"""Tests for the live-model discovery helpers (the network is stubbed)."""

from __future__ import annotations

import pytest

from tests.e2e import discovery


class _HtmlResponse:
    status_code = 200
    headers = {"content-type": "text/html; charset=utf-8"}
    text = "<html><head><title>Just a moment...</title></head><body>blocked</body></html>"

    def json(self):
        raise ValueError("Expecting value: line 1 column 1 (char 0)")


class _JsonResponse(_HtmlResponse):
    headers = {"content-type": "application/json"}

    def json(self):
        return {"rooms": []}


def test_html_discovery_response_raises_a_skip_reason(monkeypatch):
    seen: dict = {}

    def fake_get(session, url, **kwargs):
        seen.update(kwargs)
        return _HtmlResponse()

    monkeypatch.setattr(discovery, "_get", fake_get)

    with pytest.raises(discovery.DiscoveryError, match="Just a moment"):
        discovery._json(None, "https://example.test/api/rooms")

    assert seen["headers"]["Accept"] == "application/json"


def test_json_discovery_response_is_returned(monkeypatch):
    monkeypatch.setattr(discovery, "_get", lambda *args, **kwargs: _JsonResponse())

    assert discovery._json(None, "https://example.test/api/rooms") == {"rooms": []}
