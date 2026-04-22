"""Tests for providers/gmail_scanner.py — mocked at the googleapiclient
layer (same pattern as test_google_calendar.py) and at the Anthropic
client layer (same pattern as test_email_task_extractor.py).

We fake the chain `discovery.build() → service.users().messages()
.list()/.get().execute()` with scripted response dicts and thread a
FakeAnthropic client through GmailScanner's test seam, so no network
and no real Anthropic SDK usage are required.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from providers.gmail_scanner import GmailScanner


# ---------- Helpers (top of file — reviewable in isolation) ----------

def _b64url(text: str) -> str:
    """Encode `text` the way Gmail returns body data."""
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


def _gmail_message(
    *, mid: str, subject: str, sender: str, date_iso: str,
    body: str = "", multipart: bool = True,
) -> dict:
    """Build a fake Gmail payload. `multipart=True` nests body under
    payload.parts[0].body.data (standard shape). `multipart=False`
    puts it on payload.body.data (simple messages)."""
    headers = [
        {"name": "Subject", "value": subject},
        {"name": "From", "value": sender},
        {"name": "Date", "value": date_iso},
    ]
    if multipart:
        payload = {
            "headers": headers,
            "parts": [{
                "mimeType": "text/plain",
                "body": {"data": _b64url(body)} if body else {},
            }],
        }
    else:
        payload = {
            "headers": headers,
            "body": {"data": _b64url(body)} if body else {},
        }
    return {"id": mid, "payload": payload}


class _FakeExec:
    def __init__(self, result, *, raise_exc=None):
        self._result = result
        self._raise = raise_exc

    def execute(self):
        if self._raise is not None:
            raise self._raise
        return self._result


class _FakeMessages:
    """Captures list/get calls and returns scripted responses.

    `list_response` is the dict returned by .list().execute().
    `messages_by_id` maps id → message dict OR Exception instance
    (when an Exception, .get(id=...).execute() raises it).
    """
    def __init__(self, list_response, messages_by_id):
        self.list_response = list_response
        self.messages_by_id = messages_by_id
        self.list_calls: list[dict] = []
        self.get_calls: list[dict] = []

    def list(self, **kw):
        self.list_calls.append(kw)
        return _FakeExec(self.list_response)

    def get(self, **kw):
        self.get_calls.append(kw)
        target = self.messages_by_id.get(kw["id"])
        if isinstance(target, Exception):
            return _FakeExec(None, raise_exc=target)
        return _FakeExec(target)


class _FakeUsers:
    def __init__(self, messages):
        self._messages = messages

    def messages(self):
        return self._messages


class _FakeGmailService:
    def __init__(self, messages):
        self._users = _FakeUsers(messages)

    def users(self):
        return self._users


class _FakeAnthropicClient:
    """Scripts a single response for `extract_tasks_from_email`. Pass
    `scripts` as a list of JSON strings (one per Claude call, in order)
    or a single string used for every call."""
    def __init__(self, scripts):
        if isinstance(scripts, str):
            scripts = [scripts]
        self._scripts = list(scripts)
        self._idx = 0
        self.messages = self
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        text = (self._scripts[self._idx]
                if self._idx < len(self._scripts)
                else self._scripts[-1])
        self._idx += 1
        return SimpleNamespace(content=[SimpleNamespace(text=text)])


@pytest.fixture
def patch_build(monkeypatch):
    """Install a fake `googleapiclient.discovery.build` for the next
    `_svc()` call. Returns a setter."""
    state = {"service": None}

    def _fake_build(api, version, credentials=None, cache_discovery=True):
        assert api == "gmail"
        assert version == "v1"
        return state["service"]

    import sys, types
    fake_discovery = types.ModuleType("googleapiclient.discovery")
    fake_discovery.build = _fake_build
    fake_pkg = types.ModuleType("googleapiclient")
    fake_pkg.discovery = fake_discovery
    monkeypatch.setitem(sys.modules, "googleapiclient", fake_pkg)
    monkeypatch.setitem(sys.modules, "googleapiclient.discovery", fake_discovery)

    def install(service):
        state["service"] = service
    return install


@pytest.fixture
def stub_creds(monkeypatch):
    monkeypatch.setattr(
        GmailScanner, "_load_credentials",
        lambda self: SimpleNamespace(token="fake"),
    )


def _scanner(tmp_path, **overrides):
    defaults = dict(
        credentials_path=tmp_path / "credentials.json",
        token_path=tmp_path / "google_token.json",
    )
    defaults.update(overrides)
    return GmailScanner(**defaults)


# ---------- Tests ----------

def test_empty_inbox_returns_empty_list(tmp_path, patch_build, stub_creds):
    messages = _FakeMessages({"messages": []}, {})
    patch_build(_FakeGmailService(messages))
    s = _scanner(tmp_path, anthropic_client=_FakeAnthropicClient("[]"))
    assert s.list_active_tasks() == []
    # Default query should be used when no label filter.
    assert messages.list_calls[0]["q"] == "newer_than:3d is:unread"
    assert messages.list_calls[0]["maxResults"] == 25


def test_label_filter_shapes_query(tmp_path, patch_build, stub_creds):
    messages = _FakeMessages({"messages": []}, {})
    patch_build(_FakeGmailService(messages))
    s = _scanner(
        tmp_path,
        label_filter=["IMPORTANT", "TODO list"],
        anthropic_client=_FakeAnthropicClient("[]"),
    )
    s.list_active_tasks()
    q = messages.list_calls[0]["q"]
    assert "label:IMPORTANT" in q
    assert 'label:"TODO list"' in q
    assert " OR " in q


def test_extractor_receives_parsed_multipart_email(tmp_path, patch_build, stub_creds):
    msg = _gmail_message(
        mid="m1",
        subject="Q3 proposal — next steps",
        sender="pm@acme.com",
        date_iso="Tue, 21 Apr 2026 09:00:00 +0000",
        body="Please finalize the draft by EOD Friday.",
        multipart=True,
    )
    messages = _FakeMessages(
        {"messages": [{"id": "m1"}]}, {"m1": msg},
    )
    patch_build(_FakeGmailService(messages))

    client = _FakeAnthropicClient(
        '[{"title":"Finalize Q3 proposal","duration_min":90,'
        '"deadline_ts":"2026-04-24T22:00:00Z","priority":"high"}]'
    )
    s = _scanner(tmp_path, anthropic_client=client)
    out = s.list_active_tasks()

    assert len(out) == 1
    t = out[0]
    assert t.source == "gmail"
    assert t.source_id == "m1::0"
    assert t.title == "Finalize Q3 proposal"
    assert t.duration_hint_min == 90
    assert t.priority_hint == "high"
    assert t.deadline_utc == datetime(2026, 4, 24, 22, 0, tzinfo=timezone.utc)
    assert t.deadline_utc.tzinfo is not None
    assert t.notes is not None
    assert "Q3 proposal" in t.notes
    assert "pm@acme.com" in t.notes

    # The user prompt sent to Claude must carry subject, sender, and body.
    prompt_text = client.calls[0]["messages"][0]["content"]
    assert "Q3 proposal" in prompt_text
    assert "pm@acme.com" in prompt_text
    assert "finalize the draft" in prompt_text


def test_extractor_receives_parsed_non_multipart_email(tmp_path, patch_build, stub_creds):
    msg = _gmail_message(
        mid="m-simple",
        subject="Quick favor",
        sender="friend@example.com",
        date_iso="Tue, 21 Apr 2026 09:00:00 +0000",
        body="Can you review my PR before Thursday?",
        multipart=False,
    )
    messages = _FakeMessages(
        {"messages": [{"id": "m-simple"}]}, {"m-simple": msg},
    )
    patch_build(_FakeGmailService(messages))

    client = _FakeAnthropicClient(
        '[{"title":"Review PR","duration_min":30,"priority":"medium"}]'
    )
    s = _scanner(tmp_path, anthropic_client=client)
    out = s.list_active_tasks()

    assert len(out) == 1
    assert out[0].source_id == "m-simple::0"
    # The extractor prompt should include the non-multipart body.
    prompt_text = client.calls[0]["messages"][0]["content"]
    assert "review my PR" in prompt_text


def test_multiple_tasks_from_one_email_get_distinct_source_ids(
        tmp_path, patch_build, stub_creds):
    msg = _gmail_message(
        mid="big",
        subject="Two things",
        sender="boss@acme.com",
        date_iso="Tue, 21 Apr 2026 09:00:00 +0000",
        body="1) Ship the deck. 2) Email the client.",
    )
    messages = _FakeMessages(
        {"messages": [{"id": "big"}]}, {"big": msg},
    )
    patch_build(_FakeGmailService(messages))

    client = _FakeAnthropicClient(
        '[{"title":"Ship the deck","duration_min":90},'
        '{"title":"Email the client","duration_min":15}]'
    )
    s = _scanner(tmp_path, anthropic_client=client)
    out = s.list_active_tasks()

    assert len(out) == 2
    assert out[0].source_id == "big::0"
    assert out[1].source_id == "big::1"
    assert out[0].title == "Ship the deck"
    assert out[1].title == "Email the client"


def test_deadline_ts_flows_to_timezone_aware_utc_datetime(
        tmp_path, patch_build, stub_creds):
    msg = _gmail_message(
        mid="m1",
        subject="Review",
        sender="x@y.com",
        date_iso="Tue, 21 Apr 2026 09:00:00 +0000",
        body="Due soon.",
    )
    messages = _FakeMessages(
        {"messages": [{"id": "m1"}]}, {"m1": msg},
    )
    patch_build(_FakeGmailService(messages))

    client = _FakeAnthropicClient(
        '[{"title":"Review doc","duration_min":30,'
        '"deadline_ts":"2026-04-23T14:00:00Z","priority":"high"}]'
    )
    out = _scanner(tmp_path, anthropic_client=client).list_active_tasks()
    dl = out[0].deadline_utc
    assert dl is not None
    assert dl.tzinfo is not None
    # UTC offset is zero.
    assert dl.utcoffset().total_seconds() == 0
    assert dl == datetime(2026, 4, 23, 14, 0, tzinfo=timezone.utc)


def test_no_deadline_ts_yields_none(tmp_path, patch_build, stub_creds):
    msg = _gmail_message(
        mid="m1", subject="s", sender="x@y.com",
        date_iso="Tue, 21 Apr 2026 09:00:00 +0000",
        body="b",
    )
    messages = _FakeMessages({"messages": [{"id": "m1"}]}, {"m1": msg})
    patch_build(_FakeGmailService(messages))
    client = _FakeAnthropicClient(
        '[{"title":"Do a thing","duration_min":30}]'
    )
    out = _scanner(tmp_path, anthropic_client=client).list_active_tasks()
    assert out[0].deadline_utc is None


def test_require_approval_propagates(tmp_path, patch_build, stub_creds):
    messages = _FakeMessages({"messages": []}, {})
    patch_build(_FakeGmailService(messages))
    s = _scanner(tmp_path, require_approval=True,
                 anthropic_client=_FakeAnthropicClient("[]"))
    assert s.require_approval is True
    # And the base-class default is preserved when omitted.
    s2 = _scanner(tmp_path, anthropic_client=_FakeAnthropicClient("[]"))
    assert s2.require_approval is False


def test_one_broken_message_does_not_kill_the_batch(
        tmp_path, patch_build, stub_creds):
    good = _gmail_message(
        mid="good", subject="Ok", sender="x@y.com",
        date_iso="Tue, 21 Apr 2026 09:00:00 +0000", body="Finish the brief",
    )
    messages = _FakeMessages(
        {"messages": [{"id": "bad"}, {"id": "good"}]},
        {"bad": RuntimeError("Gmail API 500"), "good": good},
    )
    patch_build(_FakeGmailService(messages))

    client = _FakeAnthropicClient(
        '[{"title":"Finish the brief","duration_min":60}]'
    )
    s = _scanner(tmp_path, anthropic_client=client)
    out = s.list_active_tasks()
    # The broken message is silently skipped; the good one still gets through.
    assert len(out) == 1
    assert out[0].source_id == "good::0"


def test_list_api_exception_returns_empty(tmp_path, patch_build, stub_creds):
    class _BlowingUpMessages:
        def list(self, **kw):
            return _FakeExec(None, raise_exc=RuntimeError("boom"))

        def get(self, **kw):
            raise AssertionError("should not be called")

    class _Users:
        def messages(self):
            return _BlowingUpMessages()

    class _Svc:
        def users(self):
            return _Users()

    patch_build(_Svc())
    s = _scanner(tmp_path, anthropic_client=_FakeAnthropicClient("[]"))
    assert s.list_active_tasks() == []


def test_health_check_missing_credentials(tmp_path):
    s = _scanner(tmp_path)
    info = s.health_check()
    assert info["source"] == "gmail"
    assert info["token_present"] is False
    assert "credentials.json not found" in info["error"]


def test_health_check_missing_token(tmp_path):
    (tmp_path / "credentials.json").write_text("{}")
    s = _scanner(tmp_path)
    info = s.health_check()
    assert info["token_present"] is False
    assert "refresh token" in info["error"].lower()


def test_health_check_success_shape(tmp_path):
    (tmp_path / "credentials.json").write_text("{}")
    (tmp_path / "google_token.json").write_text("{}")
    s = _scanner(
        tmp_path,
        label_filter=["IMPORTANT"],
        max_messages=10,
        require_approval=True,
    )
    info = s.health_check()
    assert info["source"] == "gmail"
    assert info["token_present"] is True
    assert info["label_filter"] == ["IMPORTANT"]
    assert info["max_messages"] == 10
    assert info["require_approval"] is True
    assert "error" not in info
    assert info["credentials_path"].endswith("credentials.json")
