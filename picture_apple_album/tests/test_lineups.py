"""picture_apple_album, Lineups: the listed photos, their thumbnails and the
pin. Both album kinds run on recorded/mocked responses; no test touches the
network."""

from __future__ import annotations

import importlib.util
import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TOKEN = "0AbCdEfGhIjKlMnOpQrStUvWx"
LINK = f"https://photos.icloud.com/shared/album/{TOKEN}/"
LEGACY_TOKEN = "B00AAAAAAAAAA"

_FAKE_WEBSTREAM = json.dumps(
    {
        "streamName": "Family",
        "userFirstName": "Jane",
        "userLastName": "Doe",
        "photos": [
            {
                "photoGuid": "abc-123",
                "width": "1200",
                "height": "1600",
                "dateCreated": "2026-05-01T00:00:00Z",
                "derivatives": {
                    "1500": {"checksum": "CHKSM_BIG", "fileSize": "200000", "width": "1200", "height": "1600"},
                    "640": {"checksum": "CHKSM_SMALL", "fileSize": "30000", "width": "480", "height": "640"},
                },
            }
        ],
    }
).encode()
_FAKE_ASSETURLS = json.dumps(
    {
        "items": {
            "CHKSM_BIG": {"url_location": "cvws.icloud-content.com", "url_path": "/B/abc/biglurl?signature=xyz"},
            "CHKSM_SMALL": {"url_location": "cvws.icloud-content.com", "url_path": "/B/abc/smalllurl?signature=xyz"},
        }
    }
).encode()


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


def _urlopen_router(body_by_path: dict[str, bytes]):
    def _fake(req, timeout):
        for key, body in body_by_path.items():
            if key in req.full_url:
                return _FakeResp(body)
        raise RuntimeError(f"unmocked URL: {req.full_url}")

    return _fake


def _apple():
    """The recorded CloudKit album (3 photos over two pages)."""
    pages = [
        json.loads((FIXTURES / n).read_text(encoding="utf-8"))
        for n in ("ck_query_page1.json", "ck_query_page2.json")
    ]
    resolve = json.loads((FIXTURES / "ck_resolve.json").read_text(encoding="utf-8"))
    state = {"query": 0}

    def fake(req, timeout):
        if "/public/records/resolve" in req.full_url:
            return _FakeResp(json.dumps(resolve).encode())
        if "/shared/records/query" in req.full_url:
            page = pages[min(state["query"], 1)]
            state["query"] += 1
            return _FakeResp(json.dumps(page).encode())
        raise RuntimeError(f"unmocked URL: {req.full_url}")

    return fake


def _fetch(server, fake, tmp_path, **options):
    options.setdefault("album", LINK)
    with patch("urllib.request.urlopen", side_effect=fake):
        return server.fetch(options, {}, ctx={"data_dir": str(tmp_path)})


def _load_server():
    spec = importlib.util.spec_from_file_location(
        "aa_server_lineups", Path(__file__).resolve().parents[1] / "server.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _no_network(*_a, **_k):
    raise AssertionError("the listing must not use the network")


def _ctx(tmp_path) -> dict:
    return {"data_dir": str(tmp_path)}


def _warm_cloudkit(server, tmp_path):
    _fetch(server, _apple(), tmp_path)


def _warm_legacy(server, tmp_path):
    fake = _urlopen_router({"/webstream": _FAKE_WEBSTREAM, "/webasseturls": _FAKE_ASSETURLS})
    with patch("urllib.request.urlopen", side_effect=fake):
        server.fetch({"album": LEGACY_TOKEN}, {}, ctx=_ctx(tmp_path))


def test_cloudkit_items_come_from_the_cached_manifest(tmp_path) -> None:
    server = _load_server()
    _warm_cloudkit(server, tmp_path)
    with patch("urllib.request.urlopen", side_effect=_no_network):
        items = server.lineup_items({"album": LINK}, ctx=_ctx(tmp_path))
        count = server.lineup_item_count({"album": LINK}, ctx=_ctx(tmp_path))
    assert [i["key"] for i in items] == ["A002", "A007", "A012"]
    assert count == len(items) == 3
    by_key = {i["key"]: i for i in items}
    assert by_key["A002"]["label"] == "2026-10-26"
    assert "/photo.jpg?" in by_key["A002"]["thumb"]
    assert "${f}" not in by_key["A002"]["thumb"]


def test_cloudkit_thumb_is_left_off_once_its_link_has_run_out(tmp_path) -> None:
    server = _load_server()
    _warm_cloudkit(server, tmp_path)
    (path,) = tmp_path.glob("manifest_*.json")
    manifest = json.loads(path.read_text())
    manifest["photos"][0]["url_expires"] = time.time() + 30  # under the 2 minute margin
    path.write_text(json.dumps(manifest))
    with patch("urllib.request.urlopen", side_effect=_no_network):
        items = server.lineup_items({"album": LINK}, ctx=_ctx(tmp_path))
    assert "thumb" not in items[0]
    assert all("thumb" in i for i in items[1:])


def test_orientation_filter_narrows_the_list_and_count(tmp_path) -> None:
    server = _load_server()
    _warm_cloudkit(server, tmp_path)
    opts = {"album": LINK, "orientation": "landscape"}
    with patch("urllib.request.urlopen", side_effect=_no_network):
        items = server.lineup_items(opts, ctx=_ctx(tmp_path))
        count = server.lineup_item_count(opts, ctx=_ctx(tmp_path))
    assert [i["key"] for i in items] == ["A007"]
    assert count == 1


def test_legacy_items_get_the_smallest_derivative_as_thumb(tmp_path) -> None:
    server = _load_server()
    _warm_legacy(server, tmp_path)
    calls = []
    router = _urlopen_router({"/webasseturls": _FAKE_ASSETURLS})

    def fake(req, timeout):
        calls.append(json.loads(req.data))
        return router(req, timeout)

    with patch("urllib.request.urlopen", side_effect=fake):
        items = server.lineup_items({"album": LEGACY_TOKEN}, ctx=_ctx(tmp_path))
        again = server.lineup_items({"album": LEGACY_TOKEN}, ctx=_ctx(tmp_path))
    assert items == again
    assert items[0]["key"] == "abc-123"
    assert items[0]["label"] == "2026-05-01"
    assert items[0]["thumb"].endswith("smalllurl?signature=xyz")
    # One batched lookup for every photo, then served from the cache.
    assert calls == [{"photoGuids": ["abc-123"]}]


def test_listing_when_manifest_is_not_cached_loads_once_in_the_time_box(tmp_path) -> None:
    server = _load_server()
    with patch("urllib.request.urlopen", side_effect=_apple()):
        items = server.lineup_items({"album": LINK}, ctx=_ctx(tmp_path))
    assert len(items) == 3
    assert list(tmp_path.glob("manifest_*.json"))


def test_listing_returns_nothing_when_the_manifest_cannot_load(tmp_path) -> None:
    server = _load_server()
    with patch("urllib.request.urlopen", side_effect=OSError("down")):
        assert server.lineup_items({"album": LINK}, ctx=_ctx(tmp_path)) == []
        assert server.lineup_item_count({"album": LINK}, ctx=_ctx(tmp_path)) is None


def test_listing_needs_an_album_and_a_data_dir(tmp_path) -> None:
    server = _load_server()
    with patch("urllib.request.urlopen", side_effect=_no_network):
        assert server.lineup_items({"album": ""}, ctx=_ctx(tmp_path)) == []
        assert server.lineup_items({"album": LINK}) == []
        assert server.lineup_items({"album": LINK}, ctx={}) == []


def test_a_slow_load_is_abandoned_after_the_time_box(tmp_path, monkeypatch) -> None:
    server = _load_server()
    monkeypatch.setattr(server, "LINEUP_WAIT_S", 0.2)

    def slow(*_a, **_k):
        time.sleep(1.0)
        raise OSError("late")

    start = time.time()
    with patch("urllib.request.urlopen", side_effect=slow):
        assert server.lineup_items({"album": LINK}, ctx=_ctx(tmp_path)) == []
    assert time.time() - start < 0.9


def test_a_stale_manifest_still_lists_when_the_reload_does_not_finish(tmp_path) -> None:
    server = _load_server()
    _warm_cloudkit(server, tmp_path)
    (path,) = tmp_path.glob("manifest_*.json")
    old = time.time() - server.MANIFEST_TTL_S - 60
    os.utime(path, (old, old))
    with patch("urllib.request.urlopen", side_effect=OSError("down")):
        assert len(server.lineup_items({"album": LINK}, ctx=_ctx(tmp_path))) == 3


@pytest.mark.parametrize("mode", ["random", "sequential"])
def test_pinned_photo_is_shown_and_the_cursor_stays_put(tmp_path, mode) -> None:
    server = _load_server()
    out = _fetch(server, _apple(), tmp_path, mode="sequential")  # cursor now at 0
    assert out["guid"] == "A002"
    idx = next(tmp_path.glob(".seq_idx_*"))
    before = (idx.read_text(), idx.stat().st_mtime_ns)
    for _ in range(3):
        out = _fetch(server, _apple(), tmp_path, mode=mode, pinned_asset="A012")
        assert out["guid"] == "A012"
        assert "error" not in out
    assert (idx.read_text(), idx.stat().st_mtime_ns) == before
    # Unpinned, the sequence carries on from where it was.
    out = _fetch(server, _apple(), tmp_path, mode="sequential")
    assert out["guid"] == "A007"  # the next one in line, not the pinned A012
    assert idx.read_text() == "1"


def test_pin_ignores_the_orientation_filter_and_unknown_pins_fall_back(tmp_path) -> None:
    server = _load_server()
    out = _fetch(server, _apple(), tmp_path, orientation="landscape", pinned_asset="A002")
    assert out["guid"] == "A002"  # portrait, but chosen explicitly
    out = _fetch(server, _apple(), tmp_path, orientation="landscape", pinned_asset="GONE")
    assert out["guid"] == "A007"  # normal behaviour: the only landscape photo
    out = _fetch(server, _apple(), tmp_path, pinned_asset="  ")
    assert "error" not in out


def test_legacy_pinned_photo_renders_and_leaves_the_cursor(tmp_path) -> None:
    server = _load_server()
    fake = _urlopen_router({"/webstream": _FAKE_WEBSTREAM, "/webasseturls": _FAKE_ASSETURLS})
    with patch("urllib.request.urlopen", side_effect=fake):
        out = server.fetch(
            {"album": LEGACY_TOKEN, "mode": "sequential", "pinned_asset": "abc-123"},
            {},
            ctx=_ctx(tmp_path),
        )
    assert out["guid"] == "abc-123"
    assert not list(tmp_path.glob(".seq_idx_*"))
