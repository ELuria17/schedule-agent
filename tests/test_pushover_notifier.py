"""Tests for providers/pushover_notifier.py — mocked at requests.post."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from providers import pushover_notifier as mod
from providers.pushover_notifier import PushoverNotifier


class _Capture:
    def __init__(self, status=200, body_json=None, status_text=None):
        self.status = status
        self.body_json = body_json if body_json is not None else {"status": 1, "request": "abc"}
        self.status_text = status_text or ""
        self.calls: list[dict] = []

    def __call__(self, url, data=None, timeout=None, **kw):
        self.calls.append({"url": url, "data": data, "timeout": timeout})
        return SimpleNamespace(
            status_code=self.status,
            json=lambda: self.body_json,
            text=self.status_text,
        )


def test_send_posts_expected_fields(monkeypatch):
    cap = _Capture()
    monkeypatch.setattr(mod.requests, "post", cap)
    r = PushoverNotifier(user_key="ukey", app_token="atok").send(
        "hello", title="Morning plan", priority="high",
    )
    assert r["ok"] is True
    assert r["request_id"] == "abc"
    assert len(cap.calls) == 1
    d = cap.calls[0]["data"]
    assert d["user"] == "ukey"
    assert d["token"] == "atok"
    assert d["message"] == "hello"
    assert d["title"] == "Morning plan"
    assert d["priority"] == 1  # "high" → +1


def test_unknown_priority_falls_back_to_zero(monkeypatch):
    cap = _Capture()
    monkeypatch.setattr(mod.requests, "post", cap)
    PushoverNotifier(user_key="u", app_token="t").send("x", priority="urgent")
    assert cap.calls[0]["data"]["priority"] == 0


def test_device_and_sound_when_configured(monkeypatch):
    cap = _Capture()
    monkeypatch.setattr(mod.requests, "post", cap)
    PushoverNotifier(
        user_key="u", app_token="t", device="iphone15", sound="bike",
    ).send("x")
    d = cap.calls[0]["data"]
    assert d["device"] == "iphone15"
    assert d["sound"] == "bike"


def test_http_error_returns_ok_false(monkeypatch):
    cap = _Capture(status=429, status_text="rate limited")
    monkeypatch.setattr(mod.requests, "post", cap)
    r = PushoverNotifier(user_key="u", app_token="t").send("x")
    assert r["ok"] is False
    assert "429" in r["error"]
    assert "rate limited" in r["error"]


def test_api_status_not_one_means_not_ok(monkeypatch):
    """Pushover returns status=0 in the JSON body when something's wrong
    (e.g. invalid app token) even if the HTTP status was 200."""
    cap = _Capture(body_json={"status": 0, "errors": ["invalid token"]})
    monkeypatch.setattr(mod.requests, "post", cap)
    r = PushoverNotifier(user_key="u", app_token="bad").send("x")
    assert r["ok"] is False


def test_connection_error_returns_ok_false(monkeypatch):
    def boom(*a, **kw):
        raise ConnectionError("dns failed")
    monkeypatch.setattr(mod.requests, "post", boom)
    r = PushoverNotifier(user_key="u", app_token="t").send("x")
    assert r["ok"] is False
    assert "ConnectionError" in r["error"]


def test_health_check_redacts_user_key():
    info = PushoverNotifier(user_key="u-very-long-key", app_token="t").health_check()
    assert info["channel"] == "pushover"
    assert info["user_key"] != "u-very-long-key"
    assert info["user_key"].startswith("u-ve")
