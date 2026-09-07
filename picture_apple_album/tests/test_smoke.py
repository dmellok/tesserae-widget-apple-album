"""picture_apple_album smoke: token-parsing + base62 partition
derivation are pure functions; the network paths are exercised via
mocked HTTP responses to confirm the manifest + asseturls flow."""

from __future__ import annotations

import importlib.util
import json
import re
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest
from flask.testing import FlaskClient


def _load_server():
    spec = importlib.util.spec_from_file_location(
        "aa_server",
        Path(__file__).resolve().parents[1] / "server.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_parse_token_handles_full_share_url() -> None:
    server = _load_server()
    assert server._parse_token("https://www.icloud.com/sharedalbum/#B0xABCDEF") == "B0xABCDEF"
    assert server._parse_token("B0xABCDEF") == "B0xABCDEF"
    assert server._parse_token("  https://www.icloud.com/sharedalbum/#B0xy ") == "B0xy"


def test_partition_derivation_matches_reference_implementation() -> None:
    """For 'B'-prefixed tokens, partition = base62(token[1:3]).
    'A'-prefixed tokens use 1 char (base62(token[1:2]))."""
    server = _load_server()
    # token[1:3] = "0A" -> 0*62 + 10 = 10
    assert server._partition_for("B0AzzzzzZ") == 10
    # token[1:3] = "00" -> 0
    assert server._partition_for("B00xxxxxx") == 0
    # A-prefix takes only one char: token[1:2] = "Z" -> 35
    assert server._partition_for("AZxxxxxx") == 35


def test_token_regex_accepts_base64url_share_tokens() -> None:
    """Newer share links carry a long base64url token containing '_'
    and '-'. The partition rule (base62 of token[1:3]) still applies:
    Apple's 330 redirect for this token reports partition 134."""
    server = _load_server()
    tok = "D2Av3xm1ZC1oiMtDjuq8n5tuATQCAEQARog_KVvBdn6NRKTCPDZgIT6Tl9M4hXmUp_zwtnAljCq4tg"
    assert server.TOKEN_RE.match(tok)
    assert server._partition_for(tok) == 134
    assert server._initial_base_url(tok).startswith("https://p134-sharedstreams.icloud.com/")


def test_partition_falls_back_when_token_chars_are_not_base62() -> None:
    """base64url tokens can put '_' or '-' in the partition slice.
    Instead of raising, use the fallback partition and rely on the 330
    redirect."""
    server = _load_server()
    tok = "D_Av3xm1ZC1oiMtDjuq8n5tuATQCAEQARog_KVvBdn6NRKTCPDZgIT6Tl9M4hXmUp_zwtnAljCq4tg"
    assert server._partition_for(tok) == server.FALLBACK_PARTITION
    assert server._initial_base_url(tok).startswith("https://p01-sharedstreams.icloud.com/")
    assert server._partition_for("A-xxxxxx") == server.FALLBACK_PARTITION


def test_cache_key_keeps_short_values_and_hashes_long_ones() -> None:
    server = _load_server()
    assert server._safe_cache_key("B0xABCDEF") == "B0xABCDEF"
    assert server._safe_cache_key("abc-123") == "abc-123"
    prefix = "D2Av3xm1ZC1oiMtDjuq8n5tuATQCAEQARog_KVvBdn6NRKTCPDZ"
    a = server._safe_cache_key(prefix + "gIT6Tl9M4hXmUp_zwtnAljCq4tg")
    b = server._safe_cache_key(prefix + "ZZZZZZZZZZZZZZZZZZZZZZZZZZZ")
    assert a != b
    assert len(a) <= 40
    assert not re.search(r"[^A-Za-z0-9_-]", a)


def test_initial_base_url_zero_pads_low_partitions() -> None:
    server = _load_server()
    assert server._initial_base_url("B00xxxxxxx").startswith(
        "https://p00-sharedstreams.icloud.com/"
    )
    # partition 10 → 'p10'
    assert server._initial_base_url("B0Axxxxxxx").startswith(
        "https://p10-sharedstreams.icloud.com/"
    )


def test_orientation_classifier_handles_edge_cases() -> None:
    server = _load_server()
    assert server._orientation_of({"width": 100, "height": 100}) == "square"
    assert server._orientation_of({"width": 200, "height": 100}) == "landscape"
    assert server._orientation_of({"width": 100, "height": 200}) == "portrait"
    assert server._orientation_of({"width": 0, "height": 100}) == "any"


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
                    "1500": {
                        "checksum": "CHKSM_BIG",
                        "fileSize": "200000",
                        "width": "1200",
                        "height": "1600",
                    },
                    "640": {
                        "checksum": "CHKSM_SMALL",
                        "fileSize": "30000",
                        "width": "480",
                        "height": "640",
                    },
                },
            },
        ],
    }
).encode()

_FAKE_ASSETURLS = json.dumps(
    {
        "items": {
            "CHKSM_BIG": {
                "url_location": "cvws.icloud-content.com",
                "url_path": "/B/abc/biglurl?signature=xyz",
            },
            "CHKSM_SMALL": {
                "url_location": "cvws.icloud-content.com",
                "url_path": "/B/abc/smalllurl?signature=xyz",
            },
        },
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
        url = req.full_url if hasattr(req, "full_url") else str(req)
        for key, body in body_by_path.items():
            if key in url:
                return _FakeResp(body)
        raise RuntimeError(f"unmocked URL: {url}")

    return _fake


def test_fetch_returns_largest_derivative_url(tmp_path) -> None:
    server = _load_server()
    fake = _urlopen_router(
        {
            "/webstream": _FAKE_WEBSTREAM,
            "/webasseturls": _FAKE_ASSETURLS,
        }
    )
    with patch("urllib.request.urlopen", side_effect=fake):
        out = server.fetch(
            {"album": "B00AAAAAAAAAA", "mode": "random"},
            {},
            ctx={"data_dir": str(tmp_path)},
        )
    assert "error" not in out
    # Largest derivative wins (CHKSM_BIG → biglurl)
    assert out["url"].endswith("biglurl?signature=xyz")
    assert out["stream"] == "Family"
    assert out["owner"] == "Jane Doe"


def test_resolve_base_url_retries_from_fallback_when_host_has_no_dns() -> None:
    """A token whose partition slice decodes to a high number (e.g. 'zz'
    -> 3843) points at a host with no DNS record. The first POST raises
    URLError; we retry from the fallback partition."""
    server = _load_server()
    seen: list[str] = []

    def _fake(req, timeout):
        seen.append(req.full_url)
        if "p3843-" in req.full_url:
            raise urllib.error.URLError("nodename nor servname provided")
        return _FakeResp(_FAKE_WEBSTREAM)

    with patch("urllib.request.urlopen", side_effect=_fake):
        base, body = server._resolve_base_url("Dzzv3xm1ZC1oiMtDjuq8n5tuATQCAEQARog_KVv")
    assert seen[0].startswith("https://p3843-sharedstreams.icloud.com/")
    assert seen[1].startswith("https://p01-sharedstreams.icloud.com/")
    assert base.startswith("https://p01-sharedstreams.icloud.com/")
    assert body["streamName"] == "Family"


def test_fetch_returns_error_when_token_missing() -> None:
    server = _load_server()
    out = server.fetch({}, {}, ctx={"data_dir": "/tmp/aa_nope"})
    assert "error" in out
    assert "share link" in out["error"].lower()


@pytest.mark.parametrize("size", ["sm", "md", "lg"])
def test_renders_empty_state_when_no_album_configured(client: FlaskClient, size: str) -> None:
    resp = client.get(f"/_test/render?plugin=picture_apple_album&size={size}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'data-plugin="picture_apple_album"' in body
