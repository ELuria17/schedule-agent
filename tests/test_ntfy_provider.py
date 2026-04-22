"""Tests for providers/ntfy_notifier.py — mocked HTTP."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from providers import ntfy_notifier as ntfy_mod
from providers.ntfy_notifier import NtfyNotifier


class _Capture:
    """Records the last requests.post call and returns a scripted response."""
    def __init__(self, status_code=200, headers=None, body=""):
        self.status_code = status_code
        self.headers = headers or {}
        self.body = body
        self.calls: list[dict] = []

    def __call__(self, url, data=None, headers=None, timeout=None):
        self.calls.append({"url": url, "data": data,
                           "headers": dict(headers or {}), "timeout": timeout})
        return SimpleNamespace(status_code=self.status_code,
                               headers=self.headers, text=self.body)


def test_send_posts_body_to_topic_url(monkeypatch):
    cap = _Capture(headers={"X-Message-Id": "abc123"})
    monkeypatch.setattr(ntfy_mod.requests, "post", cap)
    n = NtfyNotifier(topic="my-secret-topic")
    out = n.send("hello world")
    assert out == {"ok": True, "message_id": "abc123"}
    assert len(cap.calls) == 1
    call = cap.calls[0]
    assert call["url"] == "https://ntfy.sh/my-secret-topic"
    assert call["data"] == b"hello world"


def test_title_and_priority_headers(monkeypatch):
    cap = _Capture()
    monkeypatch.setattr(ntfy_mod.requests, "post", cap)
    NtfyNotifier(topic="t").send("body", title="Heads up", priority="high")
    h = cap.calls[0]["headers"]
    assert h["Title"] == "Heads up"
    assert h["Priority"] == "5"  # 'high' → 5 per ntfy


def test_default_tags_serialized_as_csv(monkeypatch):
    cap = _Capture()
    monkeypatch.setattr(ntfy_mod.requests, "post", cap)
    NtfyNotifier(topic="t", default_tags=["calendar", "alarm_clock"]).send("x")
    assert cap.calls[0]["headers"]["Tags"] == "calendar,alarm_clock"


def test_bearer_token_when_configured(monkeypatch):
    cap = _Capture()
    monkeypatch.setattr(ntfy_mod.requests, "post", cap)
    NtfyNotifier(topic="t", bearer_token="tk_abc").send("x")
    assert cap.calls[0]["headers"]["Authorization"] == "Bearer tk_abc"


def test_custom_base_url_used_for_self_hosted(monkeypatch):
    cap = _Capture()
    monkeypatch.setattr(ntfy_mod.requests, "post", cap)
    NtfyNotifier(topic="t", base_url="https://ntfy.mydomain.com/").send("x")
    assert cap.calls[0]["url"] == "https://ntfy.mydomain.com/t"


def test_http_error_returns_ok_false(monkeypatch):
    cap = _Capture(status_code=429, body="rate limited")
    monkeypatch.setattr(ntfy_mod.requests, "post", cap)
    out = NtfyNotifier(topic="t").send("x")
    assert out["ok"] is False
    assert "429" in out["error"]
    assert "rate limited" in out["error"]


def test_connection_error_returns_ok_false(monkeypatch):
    def boom(*a, **kw):
        raise ConnectionError("no route to host")
    monkeypatch.setattr(ntfy_mod.requests, "post", boom)
    out = NtfyNotifier(topic="t").send("x")
    assert out["ok"] is False
    assert "ConnectionError" in out["error"]


def test_health_check_redacts_topic():
    """Topic is a shared secret; health_check should redact it."""
    info = NtfyNotifier(topic="super-long-topic-name").health_check()
    assert info["channel"] == "ntfy"
    assert info["authenticated"] is False
    # Long topics: first 4 chars then ellipsis — never the full thing.
    assert info["topic"] != "super-long-topic-name"
    assert info["topic"].startswith("supe")


def test_health_check_short_topic_shown_whole():
    info = NtfyNotifier(topic="abc").health_check()
    assert info["topic"] == "abc"


def test_unknown_priority_falls_back_to_normal(monkeypatch):
    cap = _Capture()
    monkeypatch.setattr(ntfy_mod.requests, "post", cap)
    NtfyNotifier(topic="t").send("x", priority="urgent")
    assert cap.calls[0]["headers"]["Priority"] == "3"  # normal default
