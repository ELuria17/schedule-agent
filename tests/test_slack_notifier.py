"""Tests for providers/slack_notifier.py — mocked at requests.post."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from providers import slack_notifier as mod
from providers.slack_notifier import SlackNotifier


class _Capture:
    def __init__(self, status=200, text="ok"):
        self.status = status
        self.text = text
        self.calls: list[dict] = []

    def __call__(self, url, json=None, timeout=None, **kw):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        return SimpleNamespace(status_code=self.status, text=self.text)


_WEBHOOK = "https://hooks.slack.com/services/T0/B0/secret"


def test_send_posts_to_webhook(monkeypatch):
    cap = _Capture()
    monkeypatch.setattr(mod.requests, "post", cap)
    r = SlackNotifier(webhook_url=_WEBHOOK).send("hello")
    assert r == {"ok": True}
    assert len(cap.calls) == 1
    assert cap.calls[0]["url"] == _WEBHOOK
    assert cap.calls[0]["json"]["text"] == "hello"


def test_title_rendered_as_bold_first_line(monkeypatch):
    cap = _Capture()
    monkeypatch.setattr(mod.requests, "post", cap)
    SlackNotifier(webhook_url=_WEBHOOK).send("body goes here", title="Today")
    text = cap.calls[0]["json"]["text"]
    assert text.startswith("*Today*\n")
    assert "body goes here" in text


def test_high_priority_adds_emoji_prefix(monkeypatch):
    cap = _Capture()
    monkeypatch.setattr(mod.requests, "post", cap)
    SlackNotifier(webhook_url=_WEBHOOK).send("x", priority="high")
    assert ":rotating_light:" in cap.calls[0]["json"]["text"]


def test_low_priority_adds_grey_prefix(monkeypatch):
    cap = _Capture()
    monkeypatch.setattr(mod.requests, "post", cap)
    SlackNotifier(webhook_url=_WEBHOOK).send("x", priority="low")
    assert ":grey_exclamation:" in cap.calls[0]["json"]["text"]


def test_username_and_icon_passed_through(monkeypatch):
    cap = _Capture()
    monkeypatch.setattr(mod.requests, "post", cap)
    SlackNotifier(
        webhook_url=_WEBHOOK, username="AutoPlan", icon_emoji=":calendar:",
    ).send("x")
    body = cap.calls[0]["json"]
    assert body["username"] == "AutoPlan"
    assert body["icon_emoji"] == ":calendar:"


def test_http_error_returns_ok_false(monkeypatch):
    cap = _Capture(status=404, text="no_service")
    monkeypatch.setattr(mod.requests, "post", cap)
    r = SlackNotifier(webhook_url=_WEBHOOK).send("x")
    assert r["ok"] is False
    assert "404" in r["error"]


def test_connection_error_returns_ok_false(monkeypatch):
    def boom(*a, **kw):
        raise ConnectionError("no route")
    monkeypatch.setattr(mod.requests, "post", boom)
    r = SlackNotifier(webhook_url=_WEBHOOK).send("x")
    assert r["ok"] is False
    assert "ConnectionError" in r["error"]


def test_health_check_redacts_webhook_secret():
    info = SlackNotifier(webhook_url=_WEBHOOK).health_check()
    assert info["channel"] == "slack"
    assert "secret" not in info["webhook"]
    assert "hooks.slack.com" in info["webhook"]
