"""picture_apple_album, public iCloud Shared Album rotation.

Two kinds of shared album exist, both reached anonymously:

* Legacy shared albums (``https://www.icloud.com/sharedalbum/#<token>``)
  use the old "sharedstreams" endpoints described below.
* Newer shared albums (``https://photos.icloud.com/shared/album/<token>/``,
  ``https://share.icloud.com/photos/<token>``) live in a CloudKit zone;
  see the CloudKit section further down.

The legacy flow uses Apple's *undocumented* but long-stable endpoints:
  POST {base}/webstream      → manifest of photos in the album
  POST {base}/webasseturls   → time-limited signed URLs for specific
                               photo GUIDs (keyed by derivative checksum)

The initial base URL is derived from the token's character set
(``p{NN}-sharedstreams.icloud.com``). Apple sometimes returns HTTP 330
with an ``X-Apple-MMe-Host`` redirect when the album lives on a
different partition; we follow it once and cache the resolved host with
the manifest.

Two caches in data_dir:
  manifest_<token>.json  , webstream payload (TTL 6h; album metadata
                            rarely changes)
  asset_<guid>.json      , signed URLs for one photo (TTL 50 min;
                            Apple's URLs expire at ~1 h)

The CloudKit flow caches the same manifest (``"system": "cloudkit"``)
plus ``ck_access_<token>.json``, the anonymous access token from
records/resolve (TTL from Apple, ~20 min).

References (reverse-engineered, MIT):
  https://github.com/ghostops/ICloud-Shared-Album
  https://github.com/bertrandom/icloud-shared-album-to-flickr
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
# Used when the token gives no usable partition (non-base62 chars in
# the partition slice, or a derived host with no DNS record). Apple
# answers with a 330 redirect to the right host from here.
FALLBACK_PARTITION = 1
HTTP_TIMEOUT_S = 15
MANIFEST_TTL_S = 6 * 3600
ASSET_TTL_S = 50 * 60
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_12_4) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/56.0.2924.87 Safari/537.36"
)
REQ_HEADERS = {
    "Origin": "https://www.icloud.com",
    "Accept-Language": "en-US,en;q=0.8",
    "User-Agent": USER_AGENT,
    "Content-Type": "text/plain",
    "Accept": "*/*",
    "Referer": "https://www.icloud.com/sharedalbum/",
    "Connection": "keep-alive",
}
# Old-style tokens are base62 (B0xxxxxxxx); newer share links carry a
# ~78-char base64url token that includes '_' and '-'.
TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-;]+$")


# ----- token parsing + URL derivation --------------------------------


LEGACY = "legacy"
CLOUDKIT = "cloudkit"
AUTO = "auto"


def _parse_album(raw: str) -> tuple[str, str]:
    """Split what the user pasted into (system, token).

    system is LEGACY or CLOUDKIT when the link says which kind of album
    it is, AUTO for a bare token or an unknown link. Accepted links:
      https://www.icloud.com/sharedalbum/#<token>      legacy
      https://photos.icloud.com/shared/album/<token>/  CloudKit
      https://share.icloud.com/photos/<token>          CloudKit
      https://www.icloud.com/photos/#<token>           CloudKit (where
                                                       share.icloud.com
                                                       redirects)"""
    s = (raw or "").strip()
    if "://" not in s and re.match(r"^[\w.-]*icloud\.com/", s):
        s = "https://" + s
    if "://" not in s:
        if "#" in s:
            s = s.split("#", 1)[1]
        return AUTO, s.strip().strip("/")
    parts = urllib.parse.urlsplit(s)
    host = (parts.hostname or "").lower()
    segments = [seg for seg in parts.path.split("/") if seg]
    fragment = parts.fragment.strip().strip("/")
    if host.endswith("icloud.com") and segments[:1] == ["sharedalbum"]:
        return LEGACY, fragment
    if host == "photos.icloud.com" and segments[:2] == ["shared", "album"] and len(segments) > 2:
        return CLOUDKIT, segments[2]
    if host == "share.icloud.com" and segments[:1] == ["photos"] and len(segments) > 1:
        return CLOUDKIT, segments[1]
    if host.endswith("icloud.com") and segments[:1] == ["photos"] and fragment:
        return CLOUDKIT, fragment.split("/")[-1]
    if fragment:
        return AUTO, fragment
    return AUTO, segments[-1] if segments else ""


def _parse_token(raw: str) -> str:
    """Extract the album token from any accepted share link or a bare
    token (see _parse_album)."""
    return _parse_album(raw)[1]


def _looks_legacy(token: str) -> bool:
    """Guess the system for a bare token. Legacy tokens start with 'A'
    or 'B' (the partition prefix) or are the ~78-char base64url form;
    CloudKit short GUIDs are 25 chars and start with '0'. A wrong guess
    only costs a fallback request (see _load_manifest)."""
    return token[:1] in ("A", "B") or len(token) >= 40


def _base62_to_int(s: str) -> int:
    n = 0
    for c in s:
        idx = BASE62.find(c)
        if idx < 0:
            raise ValueError(f"non-base62 char {c!r} in token")
        n = n * 62 + idx
    return n


def _partition_for(token: str) -> int:
    """Mirror ghostops/ICloud-Shared-Album's partition derivation:
    tokens starting with 'A' use 1 char, everything else uses 2.
    Newer base64url tokens can carry '_' or '-' in those positions,
    which base62 can't decode; fall back to a known partition and let
    Apple's 330 redirect point at the right host."""
    if not token:
        raise ValueError("empty token")
    chars = token[1:2] if token[0] == "A" else token[1:3]
    try:
        return _base62_to_int(chars)
    except ValueError:
        return FALLBACK_PARTITION


def _base_url_for_partition(n: int, token: str) -> str:
    prefix = f"0{n}" if n < 10 else str(n)
    return f"https://p{prefix}-sharedstreams.icloud.com/{token}/sharedstreams/"


def _initial_base_url(token: str) -> str:
    return _base_url_for_partition(_partition_for(token), token)


# ----- HTTP helpers --------------------------------------------------


def _post_json(url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=REQ_HEADERS,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw)
    except urllib.error.HTTPError as err:
        # Apple's 330 redirect arrives here. Read the body to find the
        # new host; the caller handles re-dispatching.
        raw = err.read().decode("utf-8", errors="replace")
        try:
            payload: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"_raw": raw}
        return err.code, payload


def _resolve_base_url(token: str) -> tuple[str, dict[str, Any]]:
    """Resolve the working base URL for ``token`` by attempting a
    webstream POST and following Apple's 330 redirect if it comes back.
    Returns (base_url, first_webstream_response) so the caller doesn't
    need a second HTTP round-trip."""
    base = _initial_base_url(token)
    try:
        status, body = _post_json(base + "webstream", {"streamCtag": None})
    except urllib.error.URLError:
        # The derived host has no DNS record (high partition numbers
        # don't exist). Retry from the fallback partition; Apple's 330
        # redirect carries the real host.
        base = _base_url_for_partition(FALLBACK_PARTITION, token)
        status, body = _post_json(base + "webstream", {"streamCtag": None})
    if status == 330:
        host = body.get("X-Apple-MMe-Host")
        if not host:
            raise RuntimeError("330 redirect with no X-Apple-MMe-Host")
        base = f"https://{host}/{token}/sharedstreams/"
        status, body = _post_json(base + "webstream", {"streamCtag": None})
    if status >= 400 or "photos" not in body:
        raise RuntimeError(f"webstream failed: HTTP {status} {body!r}"[:200])
    return base, body


# ----- manifest cache ------------------------------------------------


def _safe_cache_key(value: str) -> str:
    """Filesystem-safe cache key. Short values (base62 tokens, photo
    GUIDs) pass through unchanged so existing caches stay valid; longer
    base64url tokens are truncated with a hash suffix so two tokens
    sharing a prefix don't share a cache."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", value)
    if len(safe) <= 40:
        return safe
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]
    return f"{safe[:23]}_{digest}"


def _read_cache(path: Path, ttl: int) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if time.time() - path.stat().st_mtime >= ttl:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
    except (json.JSONDecodeError, OSError):
        return None


def _write_cache(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        path.write_text(json.dumps(payload), encoding="utf-8")


def _load_manifest(token: str, data_dir: Path, system: str = AUTO) -> dict[str, Any]:
    """Cached album manifest. For a bare token (AUTO) the likelier system
    is tried first; when that one doesn't know the album, the other."""
    cache_path = data_dir / f"manifest_{_safe_cache_key(token)}.json"
    cached = _read_cache(cache_path, MANIFEST_TTL_S)
    if cached is not None:
        return cached
    if system in (LEGACY, CLOUDKIT):
        manifest = _load_system_manifest(system, token, data_dir)
    else:
        order = [LEGACY, CLOUDKIT] if _looks_legacy(token) else [CLOUDKIT, LEGACY]
        try:
            manifest = _load_system_manifest(order[0], token, data_dir)
        except Exception as first:
            try:
                manifest = _load_system_manifest(order[1], token, data_dir)
            except Exception:
                raise first from None
    _write_cache(cache_path, manifest)
    return manifest


def _load_system_manifest(system: str, token: str, data_dir: Path) -> dict[str, Any]:
    if system == CLOUDKIT:
        return _load_cloudkit_manifest(token, data_dir)
    return _load_legacy_manifest(token)


def _load_legacy_manifest(token: str) -> dict[str, Any]:
    base, body = _resolve_base_url(token)
    photos = body.get("photos") or []
    slim_photos = []
    for p in photos:
        derivatives = p.get("derivatives") or {}
        slim_derivs = []
        for size_key, d in derivatives.items():
            if not isinstance(d, dict) or not d.get("checksum"):
                continue
            slim_derivs.append(
                {
                    "size_key": size_key,
                    "checksum": d["checksum"],
                    "width": int(d.get("width") or 0),
                    "height": int(d.get("height") or 0),
                    "fileSize": int(d.get("fileSize") or 0),
                }
            )
        if not slim_derivs:
            continue
        slim_photos.append(
            {
                "guid": p.get("photoGuid"),
                "width": int(p.get("width") or 0),
                "height": int(p.get("height") or 0),
                "dateCreated": p.get("dateCreated"),
                "derivatives": slim_derivs,
            }
        )
    manifest = {
        "base": base,
        "stream": body.get("streamName") or "",
        "owner": f"{body.get('userFirstName') or ''} {body.get('userLastName') or ''}".strip(),
        "photos": slim_photos,
        "fetched_at": int(time.time()),
    }
    return manifest


def _asset_url_for(base: str, token: str, photo: dict[str, Any], data_dir: Path) -> str | None:
    """Resolve the signed URL for a photo's largest derivative.
    Caches per-photo for ASSET_TTL_S so a render burst doesn't
    repeatedly hit /webasseturls."""
    guid = photo.get("guid")
    if not guid:
        return None
    cache_path = data_dir / f"asset_{_safe_cache_key(token)}_{_safe_cache_key(guid)}.json"
    cached = _read_cache(cache_path, ASSET_TTL_S)
    if cached and cached.get("url"):
        return str(cached["url"])

    status, body = _post_json(base + "webasseturls", {"photoGuids": [guid]})
    if status >= 400:
        return None
    items = body.get("items") or {}
    # Pick the largest derivative we know about (highest fileSize); look
    # up its checksum in the items map for the signed URL.
    derivs = sorted(
        photo.get("derivatives") or [],
        key=lambda d: int(d.get("fileSize") or 0),
        reverse=True,
    )
    for d in derivs:
        item = items.get(d["checksum"])
        if not item:
            continue
        location = item.get("url_location")
        path = item.get("url_path")
        if not location or not path:
            continue
        url = f"https://{location}{path}"
        _write_cache(cache_path, {"url": url})
        return url
    return None


# ----- CloudKit shared albums ---------------------------------------
#
# Newer shared albums are CloudKit share zones ("SharedCollection-<UUID>")
# in the com.apple.photos.cloud container. The iCloud Photos web app
# reads them anonymously in three steps, which we mirror:
#
#   POST {CK_RESOLVE_URL}  {"shortGUIDs": [{"value": <token>}]}
#        → zoneID, share title, owner name, and anonymousPublicAccess:
#          a short-lived token plus the database partition host
#   POST {partition}/…/shared/records/query
#        ?publicAccessAuthToken=<token>&sharing_url_key=<shortGUID>
#        recordType CPLAssetAndMasterByAssetDateWithoutHiddenOrDeleted
#        → CPLMaster (the file) + CPLAsset (date, edits) record pairs,
#          paged with continuationMarker; each rendition field carries a
#          signed downloadURL (~15 min) with a "${f}" filename slot
#   POST {partition}/…/shared/records/lookup
#        → the same records again, for fresh download URLs


CK_RESOLVE_URL = (
    "https://p01-ckdatabasews.icloud.com/database/1/"
    "com.apple.photos.cloud/production/public/records/resolve"
)
CK_DB_PATH = "/database/1/com.apple.photos.cloud/production/shared/"
CK_QUERY_TYPE = "CPLAssetAndMasterByAssetDateWithoutHiddenOrDeleted"
CK_PAGE_SIZE = 200
CK_MAX_PAGES = 100
# Assume a download URL lives this long when it carries no e= expiry.
CK_URL_TTL_S = 10 * 60
# Stop handing out a URL this long before it expires; the frame still
# has to download it.
CK_URL_MARGIN_S = 2 * 60
# Renditions in order of preference, (record, field prefix). The 2048 px
# JPEG covers any panel; the asset's copy includes the owner's edits.
# "Full" and "Original" are often HEIC, so only JPEG/PNG files count.
CK_RENDITIONS = (
    ("asset", "resJPEGMed"),
    ("master", "resJPEGMed"),
    ("asset", "resJPEGFull"),
    ("master", "resJPEGFull"),
    ("master", "resOriginal"),
)
CK_IMAGE_TYPES = {"public.jpeg", "public.png"}
CK_VIDEO_TYPES = {
    "com.apple.quicktime-movie",
    "public.mpeg-4",
    "public.movie",
    "public.avi",
    "public.3gpp",
    "com.apple.m4v-video",
}


class AlbumError(RuntimeError):
    """A failure with a message meant for the person setting up the
    widget (shown as-is in the cell)."""


class AlbumNotFoundError(AlbumError):
    pass


_APPLE_CHANGED = " Apple may have changed how shared albums work."


def _ck_resolve(token: str) -> dict[str, Any]:
    """records/resolve for one short GUID → the single result item."""
    status, body = _post_json(
        CK_RESOLVE_URL, {"shortGUIDs": [{"value": token, "shouldFetchRootRecord": True}]}
    )
    results = body.get("results") if isinstance(body, dict) else None
    if status >= 400 or not isinstance(results, list) or not results:
        raise AlbumError(f"iCloud couldn't look up this album (HTTP {status})." + _APPLE_CHANGED)
    item = results[0] if isinstance(results[0], dict) else {}
    if item.get("serverErrorCode") == "NOT_FOUND":
        raise AlbumNotFoundError(
            "iCloud doesn't know this album link. Check the link; the owner "
            "may have stopped sharing the album."
        )
    if item.get("serverErrorCode"):
        raise AlbumError(
            f"iCloud refused this album link ({item['serverErrorCode']})." + _APPLE_CHANGED
        )
    access = item.get("anonymousPublicAccess")
    if (
        item.get("requireAppleLogin")
        or item.get("minimallyResolved")
        or not isinstance(access, dict)
        or not access.get("token")
    ):
        raise AlbumError(
            "This album isn't public. In Photos, open the album's sharing "
            "options and turn on the public link, then paste that link."
        )
    zone = item.get("zoneID")
    if (
        item.get("databaseScope") != "SHARED"
        or not isinstance(zone, dict)
        or not zone.get("zoneName")
    ):
        raise AlbumError("iCloud described this album in an unexpected way." + _APPLE_CHANGED)
    return item


def _ck_access(token: str, data_dir: Path, *, refresh: bool = False) -> dict[str, Any]:
    """Anonymous access for the album: token, partition, zone, plus the
    title and owner, cached until shortly before Apple's TTL runs out."""
    cache_path = data_dir / f"ck_access_{_safe_cache_key(token)}.json"
    if not refresh:
        cached = _read_cache(cache_path, MANIFEST_TTL_S)
        if cached and float(cached.get("expires_at") or 0) > time.time():
            return cached
    item = _ck_resolve(token)
    access = item["anonymousPublicAccess"]
    ttl_s = int(access.get("tokenTTL") or 0) / 1000
    share_fields = (item.get("share") or {}).get("fields") or {}
    name = (item.get("ownerIdentity") or {}).get("nameComponents") or {}
    result = {
        "token": access["token"],
        "partition": str(access.get("databasePartition") or "").rstrip("/")
        or "https://p01-ckdatabasews.icloud.com:443",
        "zone": item["zoneID"],
        "title": (share_fields.get("cloudkit.title") or {}).get("value") or "",
        "owner": f"{name.get('givenName') or ''} {name.get('familyName') or ''}".strip(),
        "expires_at": time.time() + max(ttl_s - 60, 60),
    }
    _write_cache(cache_path, result)
    return result


def _ck_call(
    token: str, data_dir: Path, op: str, body: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """POST a shared-database operation for the album; re-resolves once
    when Apple rejects a stale access token. Returns (access, payload)."""
    access = _ck_access(token, data_dir)
    for attempt in range(2):
        query = urllib.parse.urlencode(
            {"publicAccessAuthToken": access["token"], "sharing_url_key": token}
        )
        url = f"{access['partition']}{CK_DB_PATH}{op}?{query}"
        status, payload = _post_json(url, {**body, "zoneID": access["zone"]})
        if status in (401, 403) and attempt == 0:
            access = _ck_access(token, data_dir, refresh=True)
            continue
        if status in (401, 403):
            raise AlbumError(
                "iCloud refused anonymous access to this album's photos "
                f"(HTTP {status}). The album may no longer be public."
            )
        if status >= 400 or not isinstance(payload, dict):
            raise AlbumError(f"iCloud {op} failed (HTTP {status})." + _APPLE_CHANGED)
        return access, payload
    raise AssertionError("unreachable")


def _ck_value(record: dict[str, Any] | None, field: str) -> Any:
    if not record:
        return None
    entry = (record.get("fields") or {}).get(field)
    return entry.get("value") if isinstance(entry, dict) else None


def _ck_url_expiry(url: str) -> float:
    """Expiry (epoch s) from a signed download URL's e= parameter."""
    values = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("e") or []
    try:
        return float(values[0])
    except (IndexError, ValueError):
        return time.time() + CK_URL_TTL_S


def _ck_rendition(asset: dict[str, Any] | None, master: dict[str, Any]) -> dict[str, Any] | None:
    """The preferred browser-friendly rendition of one photo:
    {"on", "field", "url", "url_expires", "width", "height"}."""
    records = {"asset": asset, "master": master}
    for on, prefix in CK_RENDITIONS:
        record = records[on]
        res = _ck_value(record, prefix + "Res")
        if not isinstance(res, dict) or not res.get("downloadURL"):
            continue
        file_type = _ck_value(record, prefix + "FileType")
        if file_type is None and prefix != "resJPEGMed":
            continue
        if file_type is not None and file_type not in CK_IMAGE_TYPES:
            continue
        width = int(_ck_value(record, prefix + "Width") or 0)
        height = int(_ck_value(record, prefix + "Height") or 0)
        # Derived JPEGs are stored upright; an original keeps its EXIF
        # orientation, where 5-8 mean the pixels are turned 90 degrees.
        if prefix == "resOriginal" and int(_ck_value(master, "originalOrientation") or 1) >= 5:
            width, height = height, width
        url = str(res["downloadURL"]).replace("${f}", "photo.jpg")
        return {
            "on": on,
            "field": prefix + "Res",
            "url": url,
            "url_expires": _ck_url_expiry(url),
            "width": width,
            "height": height,
        }
    return None


def _ck_is_video(master: dict[str, Any]) -> bool:
    item_type = str(_ck_value(master, "itemType") or "").lower()
    return item_type in CK_VIDEO_TYPES or "movie" in item_type or "video" in item_type


def _ck_photo(asset: dict[str, Any] | None, master: dict[str, Any]) -> dict[str, Any] | None:
    """One manifest entry for a CPLMaster (+ its CPLAsset), or None for
    videos and photos without a usable rendition."""
    if _ck_is_video(master):
        return None
    rendition = _ck_rendition(asset, master)
    if rendition is None:
        return None
    date_ms = _ck_value(asset, "assetDate") or _ck_value(master, "originalCreationDate")
    date = None
    if isinstance(date_ms, (int, float)):
        date = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(date_ms / 1000))
    return {
        "guid": (asset or master).get("recordName"),
        "master": master.get("recordName"),
        "dateCreated": date,
        **rendition,
    }


def _ck_pair_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Join CPLAsset records to their CPLMaster (in asset order) and turn
    each pair into a manifest photo."""
    masters: dict[str, dict[str, Any]] = {}
    assets: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict) or record.get("serverErrorCode"):
            continue
        if record.get("recordType") == "CPLMaster":
            masters[str(record.get("recordName"))] = record
        elif record.get("recordType") == "CPLAsset":
            assets.append(record)
    photos = []
    seen: set[str] = set()
    for asset in assets:
        ref = _ck_value(asset, "masterRef") or {}
        master = masters.get(str(ref.get("recordName")))
        if master is None or asset.get("recordName") in seen:
            continue
        seen.add(str(asset.get("recordName")))
        photo = _ck_photo(asset, master)
        if photo:
            photos.append(photo)
    return photos


def _load_cloudkit_manifest(token: str, data_dir: Path) -> dict[str, Any]:
    body: dict[str, Any] = {
        "query": {
            "recordType": CK_QUERY_TYPE,
            "filterBy": [
                {
                    "fieldName": "direction",
                    "comparator": "EQUALS",
                    "fieldValue": {"value": "ASCENDING", "type": "STRING"},
                }
            ],
        },
        "resultsLimit": CK_PAGE_SIZE,
    }
    records: list[dict[str, Any]] = []
    access: dict[str, Any] = {}
    for _ in range(CK_MAX_PAGES):
        access, payload = _ck_call(token, data_dir, "records/query", body)
        records.extend(payload.get("records") or [])
        marker = payload.get("continuationMarker")
        if not marker:
            break
        body["continuationMarker"] = marker
    return {
        "system": CLOUDKIT,
        "stream": access.get("title") or "",
        "owner": access.get("owner") or "",
        "photos": _ck_pair_records(records),
        "fetched_at": int(time.time()),
    }


def _ck_asset_url_for(token: str, photo: dict[str, Any], data_dir: Path) -> str | None:
    """A download URL for a manifest photo that is good for a couple more
    minutes: the one from the manifest while it lasts, then a fresh one
    from records/lookup (cached until shortly before it expires)."""
    guid = photo.get("guid")
    if not guid:
        return None
    deadline = time.time() + CK_URL_MARGIN_S
    if photo.get("url") and float(photo.get("url_expires") or 0) > deadline:
        return str(photo["url"])
    cache_path = data_dir / f"asset_{_safe_cache_key(token)}_{_safe_cache_key(guid)}.json"
    cached = _read_cache(cache_path, ASSET_TTL_S)
    if cached and cached.get("url") and float(cached.get("url_expires") or 0) > deadline:
        return str(cached["url"])
    names = [guid] + ([photo["master"]] if photo.get("master") and photo["master"] != guid else [])
    _, payload = _ck_call(
        token, data_dir, "records/lookup", {"records": [{"recordName": n} for n in names]}
    )
    found = {
        r.get("recordName"): r
        for r in payload.get("records") or []
        if isinstance(r, dict) and not r.get("serverErrorCode")
    }
    master = found.get(photo.get("master"))
    if master is None:
        # The photo left the album: drop the manifest so the next render
        # lists the album again.
        with contextlib.suppress(OSError):
            (data_dir / f"manifest_{_safe_cache_key(token)}.json").unlink()
        return None
    rendition = _ck_rendition(found.get(guid), master)
    if rendition is None:
        return None
    _write_cache(cache_path, {"url": rendition["url"], "url_expires": rendition["url_expires"]})
    return str(rendition["url"])


# ----- orientation filter -------------------------------------------


def _orientation_of(p: dict[str, Any]) -> str:
    w = int(p.get("width") or 0)
    h = int(p.get("height") or 0)
    if w == 0 or h == 0:
        return "any"
    ratio = w / h
    if abs(ratio - 1.0) <= 0.05:
        return "square"
    return "landscape" if ratio > 1 else "portrait"


def _filter_orientation(photos: list[dict[str, Any]], wanted: str) -> list[dict[str, Any]]:
    if wanted == "any":
        return photos
    return [p for p in photos if _orientation_of(p) == wanted]


# ----- plugin contract ----------------------------------------------


def fetch(
    options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]
) -> dict[str, Any]:
    del settings
    system, token = _parse_album(options.get("album") or "")
    if not token:
        return {
            "error": "Paste the share link from a public iCloud Shared Album "
            "(https://photos.icloud.com/shared/album/… or "
            "https://www.icloud.com/sharedalbum/#…). Turn on the public "
            "link (Public Website) for the album in Photos to get it.",
            "url": None,
        }
    if not TOKEN_RE.match(token):
        return {"error": f"Bad album token: {token[:24]}…", "url": None}

    data_dir = Path(ctx["data_dir"])
    data_dir.mkdir(parents=True, exist_ok=True)

    try:
        manifest = _load_manifest(token, data_dir, system)
    except AlbumError as err:
        return {"error": str(err)[:200], "url": None}
    except Exception as err:
        return {"error": f"{type(err).__name__}: {err}"[:200], "url": None}

    photos = manifest.get("photos") or []
    if not photos:
        return {
            "error": "Album is empty (or the token isn't a published shared album).",
            "url": None,
        }

    orientation = (options.get("orientation") or "any").lower()
    filtered = _filter_orientation(photos, orientation)
    if not filtered:
        return {"error": f"No {orientation} photos in this album.", "url": None}

    mode = options.get("mode", "random")
    if mode == "sequential":
        suffix = f"_{orientation}" if orientation != "any" else ""
        idx_file = data_dir / f".seq_idx_{_safe_cache_key(token)}{suffix}"
        try:
            current = int(idx_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            current = -1
        next_idx = (current + 1) % len(filtered)
        with contextlib.suppress(OSError):
            idx_file.write_text(str(next_idx), encoding="utf-8")
        photo = filtered[next_idx]
    else:
        photo = random.choice(filtered)

    if manifest.get("system") == CLOUDKIT:
        try:
            url = _ck_asset_url_for(token, photo, data_dir)
        except AlbumError as err:
            return {"error": str(err)[:200], "url": None}
        except Exception as err:
            return {"error": f"{type(err).__name__}: {err}"[:200], "url": None}
    else:
        url = _asset_url_for(manifest["base"], token, photo, data_dir)
    if not url:
        return {"error": "Could not resolve a signed URL for the picked photo.", "url": None}

    return {
        "url": url,
        "guid": photo.get("guid"),
        "date": photo.get("dateCreated"),
        "stream": manifest.get("stream"),
        "owner": manifest.get("owner"),
        "count": len(filtered),
    }
