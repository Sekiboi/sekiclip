"""Update check helpers (no network)."""

from __future__ import annotations

from sekiclip.core.updates import is_newer, parse_remote_payload, check_for_update


def test_is_newer() -> None:
    assert is_newer("0.2.0", "0.1.0-beta.1")
    assert not is_newer("0.1.0", "0.1.0")
    assert not is_newer("0.0.9", "0.1.0")


def test_parse_plain() -> None:
    ver, url = parse_remote_payload("0.2.0\n")
    assert ver == "0.2.0"
    assert url is None


def test_parse_github_json() -> None:
    body = '{"tag_name": "v0.3.0", "html_url": "https://example.com/r"}'
    ver, url = parse_remote_payload(body, "application/json")
    assert ver == "v0.3.0"
    assert url and "example.com" in url


def test_check_no_url() -> None:
    r = check_for_update(url="")
    assert not r.ok
    assert "URL" in r.message or "configured" in r.message.lower()
