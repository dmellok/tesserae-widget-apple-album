"""picture_apple_album, CloudKit shared albums (photos.icloud.com links).

The fixtures in tests/fixtures are recorded resolve + query responses,
trimmed and sanitised (fake names, zone, tokens and URLs). No test here
touches the network except the optional live one at the bottom, which
runs only with APPLE_ALBUM_LIVE_TOKEN set to a public album link/token."""

from __future__ import annotations

import copy
import importlib.util
import io
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TOKEN = "0AbCdEfGhIjKlMnOpQrStUvWx"
LINK = f"https://photos.icloud.com/shared/album/{TOKEN}/"


def _load_server():
    spec = importlib.util.spec_from_file_location(
        "aa_server_ck",
        Path(__file__).resolve().parents[1] / "server.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class _FakeResp:
    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a) -> bool:
        return False


class _FakeApple:
    """urlopen stand-in. ``routes`` maps a URL substring to a list of
    responses served in order (the last one repeats): a dict (JSON body,
    HTTP 200) or an (status, dict) tuple. Every request is recorded."""

    def __init__(self, routes: dict[str, list]):
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, req, timeout):
        url = req.full_url
        body = json.loads(req.data) if req.data else {}
        self.calls.append((url, body))
        for key, responses in self.routes.items():
            if key in url:
                resp = responses.pop(0) if len(responses) > 1 else responses[0]
                status, payload = resp if isinstance(resp, tuple) else (200, resp)
                raw = json.dumps(payload).encode()
                if status >= 400:
                    raise urllib.error.HTTPError(url, status, "err", {}, io.BytesIO(raw))
                return _FakeResp(raw, status)
        raise RuntimeError(f"unmocked URL: {url}")

    def urls(self, part: str) -> list[str]:
        return [u for u, _ in self.calls if part in u]


def _apple(**extra) -> _FakeApple:
    routes = {
        "/public/records/resolve": [_fixture("ck_resolve.json")],
        "/shared/records/query": [_fixture("ck_query_page1.json"), _fixture("ck_query_page2.json")],
    }
    routes.update(extra)
    return _FakeApple(routes)


def _fetch(server, fake, tmp_path, **options):
    options.setdefault("album", LINK)
    with patch("urllib.request.urlopen", side_effect=fake):
        return server.fetch(options, {}, ctx={"data_dir": str(tmp_path)})


def _manifest(tmp_path) -> dict:
    (path,) = tmp_path.glob("manifest_*.json")
    return json.loads(path.read_text(encoding="utf-8"))


def test_parse_album_accepts_every_link_format() -> None:
    server = _load_server()
    ck, legacy, auto = server.CLOUDKIT, server.LEGACY, server.AUTO
    cases = {
        f"https://photos.icloud.com/shared/album/{TOKEN}/": (ck, TOKEN),
        f"https://photos.icloud.com/shared/album/{TOKEN}": (ck, TOKEN),
        f"photos.icloud.com/shared/album/{TOKEN}/": (ck, TOKEN),
        f"  https://share.icloud.com/photos/{TOKEN} ": (ck, TOKEN),
        f"https://www.icloud.com/photos/#{TOKEN}": (ck, TOKEN),
        "https://www.icloud.com/sharedalbum/#B0xABCDEF": (legacy, "B0xABCDEF"),
        "https://www.icloud.com/sharedalbum/#B0xABCDEF/": (legacy, "B0xABCDEF"),
        "www.icloud.com/sharedalbum/#B0xABCDEF": (legacy, "B0xABCDEF"),
        "B0xABCDEF": (auto, "B0xABCDEF"),
        TOKEN: (auto, TOKEN),
        "": (auto, ""),
    }
    for raw, expected in cases.items():
        assert server._parse_album(raw) == expected, raw
        assert server._parse_token(raw) == expected[1], raw


def test_bare_tokens_guess_the_likelier_system() -> None:
    server = _load_server()
    assert server._looks_legacy("B0xABCDEF")
    assert server._looks_legacy("A1xABCDEF")
    assert server._looks_legacy(
        "D2Av3xm1ZC1oiMtDjuq8n5tuATQCAEQARog_KVvBdn6NRKTCPDZgIT6Tl9M4hXmUp_zwtnAljCq4tg"
    )
    assert not server._looks_legacy(TOKEN)
    assert server.TOKEN_RE.match("0Ab-CdEfGhIjKlMn-OpQrStUv")


def test_fetch_lists_album_across_pages(tmp_path) -> None:
    server = _load_server()
    fake = _apple()
    out = _fetch(server, fake, tmp_path, mode="sequential")
    assert "error" not in out, out
    assert out["count"] == 3
    assert out["stream"] == "Family trip"
    assert out["owner"] == "Jane Doe"
    assert out["guid"] == "A002"
    assert out["date"] == "2026-10-26T18:00:30Z"
    # The ${f} filename slot is filled in, and the URL is the 2048 px JPEG.
    assert "${f}" not in out["url"]
    assert "/photo.jpg?" in out["url"]
    assert "resJPEGMedRes" in out["url"]

    # Calls go to the partition from resolve with the anonymous token,
    # and the second page carries the continuation marker.
    (resolve,) = fake.urls("/records/resolve")
    assert resolve.startswith("https://p01-ckdatabasews.icloud.com/")
    queries = [(u, b) for u, b in fake.calls if "/records/query" in u]
    assert len(queries) == 2
    url, body = queries[0]
    assert url.startswith("https://p172-ckdatabasews.icloud.com:443/database/1/")
    params = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert params["publicAccessAuthToken"] == ["FAKE_ANONYMOUS_ACCESS_TOKEN"]
    assert params["sharing_url_key"] == [TOKEN]
    assert body["zoneID"]["zoneName"].startswith("SharedCollection-")
    assert body["query"]["recordType"] == "CPLAssetAndMasterByAssetDateWithoutHiddenOrDeleted"
    assert "continuationMarker" not in body
    assert queries[1][1]["continuationMarker"] == "FAKE_CONTINUATION_MARKER"


def test_manifest_prefers_edited_jpeg_and_keeps_dimensions(tmp_path) -> None:
    server = _load_server()
    _fetch(server, _apple(), tmp_path)
    manifest = _manifest(tmp_path)
    assert manifest["system"] == "cloudkit"
    photos = {p["guid"]: p for p in manifest["photos"]}
    # Unedited photo: only the master has the JPEG. Upright portrait.
    assert photos["A002"]["on"] == "master"
    assert (photos["A002"]["width"], photos["A002"]["height"]) == (1536, 2048)
    # Edited photo: the asset's JPEG (with the edits) wins over the
    # master's; its resJPEGFull is HEIC, so it is not picked.
    assert photos["A007"]["on"] == "asset"
    assert photos["A007"]["field"] == "resJPEGMedRes"
    assert (photos["A007"]["width"], photos["A007"]["height"]) == (2048, 1536)
    assert photos["A007"]["master"] == "M004"


def test_orientation_filter_works_on_cloudkit_albums(tmp_path) -> None:
    server = _load_server()
    out = _fetch(server, _apple(), tmp_path, orientation="landscape")
    assert out["count"] == 1
    assert out["guid"] == "A007"


def _pair(master_name: str, asset_name: str, master_fields: dict, asset_fields: dict | None = None):
    master = {"recordName": master_name, "recordType": "CPLMaster", "fields": master_fields}
    asset = {
        "recordName": asset_name,
        "recordType": "CPLAsset",
        "fields": {
            "masterRef": {"value": {"recordName": master_name}, "type": "REFERENCE"},
            "assetDate": {"value": 1700000000000, "type": "TIMESTAMP"},
            **(asset_fields or {}),
        },
    }
    return [master, asset]


def _res(prefix: str, file_type: str, width: int, height: int) -> dict:
    return {
        f"{prefix}Res": {
            "value": {
                "downloadURL": f"https://cvws.icloud-content.com/B/{prefix}/${{f}}?e=4102444800"
            },
            "type": "ASSETID",
        },
        f"{prefix}FileType": {"value": file_type, "type": "STRING"},
        f"{prefix}Width": {"value": width, "type": "NUMBER_INT64"},
        f"{prefix}Height": {"value": height, "type": "NUMBER_INT64"},
    }


def test_videos_and_heic_only_items_are_skipped() -> None:
    server = _load_server()
    s = lambda v: {"value": v, "type": "STRING"}  # noqa: E731
    records = (
        # A video: has a JPEG poster frame, still skipped.
        _pair(
            "MV",
            "AV",
            {
                "itemType": s("com.apple.quicktime-movie"),
                **_res("resJPEGMed", "public.jpeg", 1920, 1080),
            },
        )
        # Only HEIC renditions: nothing a browser can show.
        + _pair(
            "MH",
            "AH",
            {"itemType": s("public.heic"), **_res("resOriginal", "public.heic", 4032, 3024)},
        )
        # JPEG original only, EXIF orientation 6: dimensions turn upright.
        + _pair(
            "MJ",
            "AJ",
            {
                "itemType": s("public.jpeg"),
                "originalOrientation": {"value": 6, "type": "NUMBER_INT64"},
                **_res("resOriginal", "public.jpeg", 4032, 3024),
            },
        )
        # A master whose asset never arrived is ignored.
        + [{"recordName": "MX", "recordType": "CPLMaster", "fields": {}}]
    )
    photos = server._ck_pair_records(records)
    assert [p["guid"] for p in photos] == ["AJ"]
    assert (photos[0]["width"], photos[0]["height"]) == (3024, 4032)
    assert photos[0]["field"] == "resOriginalRes"
    assert photos[0]["dateCreated"] == "2023-11-14T22:13:20Z"


def test_expired_url_is_refreshed_with_lookup_and_cached(tmp_path) -> None:
    server = _load_server()
    expired = copy.deepcopy(_fixture("ck_query_page1.json"))
    for record in expired["records"]:
        for field in record["fields"].values():
            if field["type"] == "ASSETID":
                field["value"]["downloadURL"] = field["value"]["downloadURL"].replace(
                    "e=4102444800", "e=1000000000"
                )
    expired.pop("continuationMarker")
    expired["records"] = [r for r in expired["records"] if r["recordName"] in ("M001", "A002")]
    master = next(r for r in expired["records"] if r["recordName"] == "M001")
    fresh = copy.deepcopy(master)
    fresh["fields"]["resJPEGMedRes"]["value"]["downloadURL"] = (
        "https://cvws.icloud-content.com/B/FRESH/${f}?e=4102444800"
    )
    fake = _apple(
        **{
            "/shared/records/query": [expired],
            "/shared/records/lookup": [
                {
                    "records": [
                        {"recordName": "A002", "recordType": "CPLAsset", "fields": {}},
                        fresh,
                    ]
                }
            ],
        }
    )
    out = _fetch(server, fake, tmp_path, mode="sequential")
    assert out["url"] == "https://cvws.icloud-content.com/B/FRESH/photo.jpg?e=4102444800"
    _, body = next(c for c in fake.calls if "/records/lookup" in c[0])
    assert {r["recordName"] for r in body["records"]} == {"A002", "M001"}

    # Next render: served from the asset cache, no second lookup and no
    # second resolve.
    out = _fetch(server, fake, tmp_path, mode="sequential")
    assert out["url"].startswith("https://cvws.icloud-content.com/B/FRESH/")
    assert len(fake.urls("/records/lookup")) == 1
    assert len(fake.urls("/records/resolve")) == 1


def test_photo_removed_from_album_drops_the_manifest(tmp_path) -> None:
    server = _load_server()
    page = _fixture("ck_query_page2.json")
    for record in page["records"]:
        for field in record["fields"].values():
            if field["type"] == "ASSETID":
                field["value"]["downloadURL"] = "https://x.example/${f}?e=1000000000"
    fake = _apple(
        **{
            "/shared/records/query": [page],
            "/shared/records/lookup": [
                {
                    "records": [
                        {"recordName": "A009", "serverErrorCode": "NOT_FOUND"},
                        {"recordName": "M008", "serverErrorCode": "NOT_FOUND"},
                    ]
                }
            ],
        }
    )
    out = _fetch(server, fake, tmp_path)
    assert out["url"] is None
    assert "signed URL" in out["error"]
    assert not list(tmp_path.glob("manifest_*.json"))


def test_stale_access_token_is_renewed_once(tmp_path) -> None:
    server = _load_server()
    denied = (401, {"serverErrorCode": "AUTHENTICATION_FAILED"})
    fake = _apple(**{"/shared/records/query": [denied, _fixture("ck_query_page2.json")]})
    out = _fetch(server, fake, tmp_path)
    assert "error" not in out, out
    assert len(fake.urls("/records/resolve")) == 2
    assert len(fake.urls("/records/query")) == 2


def test_access_refused_after_renewal_reports_it(tmp_path) -> None:
    server = _load_server()
    fake = _apple(
        **{"/shared/records/query": [(401, {"serverErrorCode": "AUTHENTICATION_FAILED"})]}
    )
    out = _fetch(server, fake, tmp_path)
    assert out["url"] is None
    assert "refused anonymous access" in out["error"]


def test_private_album_says_to_turn_on_the_public_link(tmp_path) -> None:
    server = _load_server()
    private = _fixture("ck_resolve.json")
    item = private["results"][0]
    del item["anonymousPublicAccess"]
    item["requireAppleLogin"] = True
    out = _fetch(server, _apple(**{"/public/records/resolve": [private]}), tmp_path)
    assert out["url"] is None
    assert "isn't public" in out["error"]


def test_unknown_album_link_is_reported(tmp_path) -> None:
    server = _load_server()
    not_found = {
        "results": [
            {
                "shortGUID": {"value": TOKEN},
                "reason": "Cannot resolve shortGUID",
                "serverErrorCode": "NOT_FOUND",
            }
        ]
    }
    fake = _apple(**{"/public/records/resolve": [not_found]})
    out = _fetch(server, fake, tmp_path)
    assert "doesn't know this album" in out["error"]
    # An explicit photos.icloud.com link never falls back to the legacy hosts.
    assert not fake.urls("sharedstreams")


def test_unexpected_resolve_answer_mentions_apple_changes(tmp_path) -> None:
    server = _load_server()
    out = _fetch(server, _apple(**{"/public/records/resolve": [(500, {"oops": True})]}), tmp_path)
    assert "Apple may have changed" in out["error"]


def test_bare_cloudkit_token_falls_back_to_legacy_when_unknown(tmp_path) -> None:
    server = _load_server()
    not_found = {"results": [{"serverErrorCode": "NOT_FOUND"}]}
    webstream = {
        "streamName": "Old album",
        "photos": [
            {
                "photoGuid": "g1",
                "width": "10",
                "height": "10",
                "derivatives": {"1": {"checksum": "C1", "fileSize": "5"}},
            }
        ],
    }
    fake = _apple(
        **{
            "/public/records/resolve": [not_found],
            "/webstream": [webstream],
            "/webasseturls": [{"items": {"C1": {"url_location": "h.example", "url_path": "/p"}}}],
        }
    )
    out = _fetch(server, fake, tmp_path, album=TOKEN)
    assert out["url"] == "https://h.example/p"
    assert out["stream"] == "Old album"
    assert fake.urls("/records/resolve")


def test_bare_legacy_token_does_not_touch_cloudkit(tmp_path) -> None:
    server = _load_server()
    webstream = {"streamName": "Old", "photos": []}
    fake = _FakeApple({"/webstream": [webstream]})
    out = _fetch(server, fake, tmp_path, album="B00AAAAAAAAAA")
    assert "empty" in out["error"].lower()
    assert not fake.urls("ckdatabasews")


@pytest.mark.skipif(
    not os.environ.get("APPLE_ALBUM_LIVE_TOKEN"),
    reason="set APPLE_ALBUM_LIVE_TOKEN to a public CloudKit shared-album link or token",
)
def test_live_album_lists_and_downloads_a_jpeg(tmp_path) -> None:
    server = _load_server()
    out = server.fetch(
        {"album": os.environ["APPLE_ALBUM_LIVE_TOKEN"], "mode": "random"},
        {},
        ctx={"data_dir": str(tmp_path)},
    )
    assert "error" not in out, out
    assert out["count"] > 0
    manifest = _manifest(tmp_path)
    assert manifest["system"] == "cloudkit"
    assert all(p["width"] and p["height"] for p in manifest["photos"])
    started = time.time()
    with urllib.request.urlopen(out["url"], timeout=60) as resp:
        data = resp.read()
    assert data[:3] == b"\xff\xd8\xff", "not a JPEG"
    try:
        from PIL import Image
    except ImportError:
        assert data.rstrip(b"\0")[-2:] == b"\xff\xd9"
    else:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            # iPhone JPEGs carry an HDR gain map as a second image, which
            # Pillow reports as MPO; the first image is a plain JPEG.
            assert img.format in ("JPEG", "MPO")
            assert max(img.size) >= 1024
    print(
        f"\nlive album: {out['count']} photos, '{out['stream']}', "
        f"{len(data)} byte JPEG in {time.time() - started:.1f}s"
    )
