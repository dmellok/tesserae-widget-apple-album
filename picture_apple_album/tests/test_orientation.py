"""picture_apple_album, Auto and Match the frame orientation. Runs on a seeded
CloudKit manifest whose download links are still good; no network."""

from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path
from unittest.mock import patch

TOKEN = "0AbCdEfGhIjKlMnOpQrStUvWx"
LINK = f"https://photos.icloud.com/shared/album/{TOKEN}/"
BOTH = ["landscape", "portrait"]
WIDE = {"cell_w": 800, "cell_h": 480}
TALL = {"cell_w": 480, "cell_h": 800}


def _load_server():
    spec = importlib.util.spec_from_file_location(
        "aa_server_orientation", Path(__file__).resolve().parents[1] / "server.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _no_network(*_a, **_k):
    raise AssertionError("must not use the network")


def _photo(guid, w, h):
    return {
        "guid": guid,
        "master": f"M-{guid}",
        "dateCreated": None,
        "on": "asset",
        "field": "resJPEGFullRes",
        "url": f"https://cvws.icloud-content.com/{guid}.jpg",
        "url_expires": time.time() + 3600,
        "width": w,
        "height": h,
    }


MIXED = [_photo("L1", 4000, 2250), _photo("P1", 2250, 4000), _photo("S1", 3000, 3000)]


def _seed(server, tmp_path, photos=MIXED):
    manifest = {
        "system": server.CLOUDKIT,
        "stream": "Trip",
        "owner": "",
        "photos": photos,
    }
    path = tmp_path / f"manifest_{server._safe_cache_key(TOKEN)}.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")


def _fetch(server, tmp_path, options, **ctx):
    with patch("urllib.request.urlopen", side_effect=_no_network):
        return server.fetch(
            {"album": LINK, **options}, {}, ctx={"data_dir": str(tmp_path), **ctx}
        )


def _drawn(server, tmp_path, options, **ctx):
    """The photos fetch draws over many renders."""
    return {_fetch(server, tmp_path, options, **ctx)["guid"] for _ in range(60)}


def test_auto_on_mixed_frames_matches_the_cell(tmp_path) -> None:
    server = _load_server()
    _seed(server, tmp_path)
    opts = {"orientation": "auto"}
    assert _drawn(server, tmp_path, opts, frame_shapes=BOTH, **WIDE) == {"L1", "S1"}
    assert _drawn(server, tmp_path, opts, frame_shapes=BOTH, **TALL) == {"P1", "S1"}
    # Unknown cell size: the panel's decides.
    panel = {"cell_w": 0, "cell_h": 0, "panel_w": 480, "panel_h": 800}
    assert _drawn(server, tmp_path, opts, frame_shapes=BOTH, **panel) == {"P1", "S1"}
    # Square cell: no shape to match.
    square = {"cell_w": 600, "cell_h": 600}
    assert _drawn(server, tmp_path, opts, frame_shapes=BOTH, **square) == {
        "L1",
        "P1",
        "S1",
    }


def test_auto_is_any_on_one_frame_shape_or_none(tmp_path) -> None:
    server = _load_server()
    _seed(server, tmp_path)
    everything = {"L1", "P1", "S1"}
    for opts in ({"orientation": "auto"},):
        assert (
            _drawn(server, tmp_path, opts, frame_shapes=["landscape"], **WIDE)
            == everything
        )
        assert _drawn(server, tmp_path, opts, **WIDE) == everything


def test_any_is_the_default_and_auto_is_offered() -> None:
    manifest = json.loads(
        (Path(__file__).resolve().parents[1] / "plugin.json").read_text(
            encoding="utf-8"
        )
    )
    o = next(o for o in manifest["cell_options"] if o["name"] == "orientation")
    assert o["default"] == "any"
    assert [c["value"] for c in o["choices"]] == [
        "auto",
        "frame",
        "any",
        "portrait",
        "landscape",
        "square",
    ]


def test_match_the_frame_filters_on_any_frame(tmp_path) -> None:
    server = _load_server()
    _seed(server, tmp_path)
    opts = {"orientation": "frame"}
    assert _drawn(server, tmp_path, opts, **WIDE) == {"L1", "S1"}
    assert _drawn(server, tmp_path, opts, frame_shapes=["portrait"], **TALL) == {
        "P1",
        "S1",
    }
    assert _fetch(server, tmp_path, opts, **WIDE)["count"] == 2


def test_match_the_frame_falls_back_to_the_whole_album(tmp_path) -> None:
    server = _load_server()
    _seed(server, tmp_path, [_photo("P1", 2250, 4000), _photo("P2", 2250, 4000)])
    for orientation in ("frame", "auto"):
        out = _fetch(
            server, tmp_path, {"orientation": orientation}, frame_shapes=BOTH, **WIDE
        )
        assert "error" not in out and out["count"] == 2


def test_match_the_frame_keeps_a_sequence_per_frame_shape(tmp_path) -> None:
    server = _load_server()
    _seed(
        server,
        tmp_path,
        [_photo("L1", 4000, 2250), _photo("P1", 2250, 4000), _photo("L2", 4000, 2250)],
    )
    opts = {"orientation": "frame", "mode": "sequential"}
    ids = [
        _fetch(server, tmp_path, opts, **c)["guid"] for c in (WIDE, TALL, WIDE, TALL)
    ]
    assert ids == ["L1", "P1", "L2", "P1"]


def test_lineups_list_only_what_each_frame_is_shown(tmp_path) -> None:
    server = _load_server()
    _seed(server, tmp_path)
    wide = {"data_dir": str(tmp_path), "frame_shapes": BOTH, **WIDE}
    tall = {"data_dir": str(tmp_path), "frame_shapes": BOTH, **TALL}
    with patch("urllib.request.urlopen", side_effect=_no_network):
        for orientation in ("auto", "frame"):
            opts = {"album": LINK, "orientation": orientation}
            assert [i["key"] for i in server.lineup_items(opts, ctx=wide)] == [
                "L1",
                "S1",
            ]
            assert [i["key"] for i in server.lineup_items(opts, ctx=tall)] == [
                "P1",
                "S1",
            ]
            assert server.lineup_item_count(opts, ctx=wide) == 2
        # Any, and nothing saved (which reads as the default, Any): everything.
        for orientation in ("any", None):
            any_opts = {"album": LINK, "orientation": orientation}
            assert server.lineup_item_count(any_opts, ctx=wide) == 3
            assert server.lineup_item_count(any_opts, ctx=tall) == 3
        one_shape = {**wide, "frame_shapes": ["landscape"]}
        auto = {"album": LINK, "orientation": "auto"}
        assert server.lineup_item_count(auto, ctx=one_shape) == 3


def test_pinned_photo_ignores_match_the_frame(tmp_path) -> None:
    server = _load_server()
    _seed(server, tmp_path)
    out = _fetch(
        server, tmp_path, {"orientation": "frame", "pinned_asset": "P1"}, **WIDE
    )
    assert out["guid"] == "P1"


def test_an_unset_orientation_is_any_in_a_render_and_in_lineups(tmp_path) -> None:
    """A cell saved before the option had Auto keeps showing every photo on a
    dashboard that is on both frame shapes, and Lineups lists the same set."""
    server = _load_server()
    _seed(server, tmp_path)
    everything = {"L1", "P1", "S1"}
    for ctx in (WIDE, TALL):
        assert _drawn(server, tmp_path, {}, frame_shapes=BOTH, **ctx) == everything
        listing = {"data_dir": str(tmp_path), "frame_shapes": BOTH, **ctx}
        with patch("urllib.request.urlopen", side_effect=_no_network):
            keys = {i["key"] for i in server.lineup_items({"album": LINK}, ctx=listing)}
        assert keys == everything
