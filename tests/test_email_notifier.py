"""Tests for providers/email_notifier.py — mocked at smtplib.SMTP."""
from __future__ import annotations

from typing import Optional

import pytest

from providers import email_notifier as mod
from providers.email_notifier import EmailNotifier


class _FakeSMTP:
    """Stand-in for smtplib.SMTP / SMTP_SSL. Records all interactions."""
    instances: list["_FakeSMTP"] = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.starttls_called = False
        self.login_calls: list[tuple[str, str]] = []
        self.sent: list = []
        self.closed = False
        _FakeSMTP.instances.append(self)

    def __enter__(self): return self
    def __exit__(self, *a):
        self.closed = True
        return False

    def starttls(self):
        self.starttls_called = True

    def login(self, username, password):
        self.login_calls.append((username, password))

    def send_message(self, msg):
        self.sent.append(msg)


@pytest.fixture(autouse=True)
def reset_instances():
    _FakeSMTP.instances.clear()
    yield
    _FakeSMTP.instances.clear()


def _notifier(**overrides):
    defaults = dict(
        smtp_host="smtp.example.com",
        smtp_port=587,
        username="u@example.com",
        password="pw",
        from_addr="u@example.com",
        to_addr="me@example.com",
    )
    defaults.update(overrides)
    return EmailNotifier(**defaults)


def test_send_connects_auths_sends(monkeypatch):
    monkeypatch.setattr(mod.smtplib, "SMTP", _FakeSMTP)
    r = _notifier().send("Here's today's plan", title="Morning")
    assert r == {"ok": True}
    assert len(_FakeSMTP.instances) == 1
    s = _FakeSMTP.instances[0]
    assert s.host == "smtp.example.com"
    assert s.port == 587
    assert s.starttls_called is True
    assert s.login_calls == [("u@example.com", "pw")]
    assert len(s.sent) == 1
    msg = s.sent[0]
    assert msg["Subject"] == "AutoPlan: Morning"
    assert msg["From"] == "u@example.com"
    assert msg["To"] == "me@example.com"
    # MIMEText base64-encodes the body by default; decode=True undoes that.
    assert b"Here's today's plan" in msg.get_payload(decode=True)


def test_starttls_skipped_when_use_ssl_is_true(monkeypatch):
    monkeypatch.setattr(mod.smtplib, "SMTP_SSL", _FakeSMTP)
    _notifier(smtp_port=465, use_tls=False, use_ssl=True).send("x")
    s = _FakeSMTP.instances[0]
    assert s.starttls_called is False
    assert s.port == 465


def test_subject_without_title_uses_prefix(monkeypatch):
    monkeypatch.setattr(mod.smtplib, "SMTP", _FakeSMTP)
    _notifier(subject_prefix="[AP]").send("body")
    assert _FakeSMTP.instances[0].sent[0]["Subject"] == "[AP]"


def test_high_priority_sets_priority_headers(monkeypatch):
    monkeypatch.setattr(mod.smtplib, "SMTP", _FakeSMTP)
    _notifier().send("x", priority="high")
    msg = _FakeSMTP.instances[0].sent[0]
    assert msg["X-Priority"] == "1"
    assert msg["Importance"] == "High"


def test_low_priority_sets_priority_headers(monkeypatch):
    monkeypatch.setattr(mod.smtplib, "SMTP", _FakeSMTP)
    _notifier().send("x", priority="low")
    msg = _FakeSMTP.instances[0].sent[0]
    assert msg["X-Priority"] == "5"
    assert msg["Importance"] == "Low"


def test_smtp_exception_returns_ok_false(monkeypatch):
    class _Boom:
        def __init__(self, *a, **kw):
            raise ConnectionRefusedError("server down")
    monkeypatch.setattr(mod.smtplib, "SMTP", _Boom)
    r = _notifier().send("x")
    assert r["ok"] is False
    assert "ConnectionRefusedError" in r["error"]


def test_health_check_reports_config():
    info = _notifier().health_check()
    assert info["channel"] == "email"
    assert info["smtp_host"] == "smtp.example.com"
    assert info["from"] == "u@example.com"
    assert info["to"] == "me@example.com"
    assert info["tls"] is True
