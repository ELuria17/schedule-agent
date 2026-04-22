"""Tests for providers/outlook_mail_scanner.py.

We mock microsoft_graph_auth.graph_get + get_access_token and pass
a fake anthropic client through to the shared email task extractor.
The scanner doesn't do much beyond iterating messages, stripping
HTML, and delegating — tests focus on that glue layer.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from providers import outlook_mail_scanner as mod
from providers.outlook_mail_scanner import OutlookMailScanner


# ---------- Fake Anthropic client ----------

class _FakeAnthropicClient:
    """Scripts one reply per call, FIFO. Mirrors test_email_task_extractor."""
    def __init__(self, replies):
        self._replies = list(replies)
        self.messages = self
        self.call_count = 0
        self.last_kwargs: dict | None = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        self.call_count += 1
        if self._replies:
            text = self._replies.pop(0)
        else:
            text = "[]"
        return SimpleNamespace(content=[SimpleNamespace(text=text)])


# ---------- Fake graph_get ----------

class _FakeGraph:
    def __init__(self, messages):
        self.messages = list(messages)
        self.get_calls: list[tuple[str, dict]] = []
        self.get_access_token_calls: int = 0

    def get_access_token(self, *, client_id, tenant="common",
                         token_path=None, scopes=None):
        self.get_access_token_calls += 1
        return "fake-token"

    def graph_get(self, token, path, params=None, **kw):
        self.get_calls.append((path, dict(params or {})))
        return {"value": [dict(m) for m in self.messages]}

    def token_status(self, *, token_path):
        return {"token_present": True, "accounts": ["u@example.com"]}


@pytest.fixture
def patch_graph(monkeypatch):
    def _install(messages):
        fg = _FakeGraph(messages)
        monkeypatch.setattr(mod.mg, "get_access_token", fg.get_access_token)
        monkeypatch.setattr(mod.mg, "graph_get", fg.graph_get)
        monkeypatch.setattr(mod.mg, "token_status", fg.token_status)
        return fg
    return _install


def _msg(msg_id, *, subject="Hello", sender="p@ex.com", received="2026-04-22T12:00:00Z",
         body="Please do the thing", content_type="text", categories=None):
    return {
        "id": msg_id,
        "subject": subject,
        "from": {"emailAddress": {"address": sender, "name": sender}},
        "receivedDateTime": received,
        "body": {"contentType": content_type, "content": body},
        "categories": list(categories or []),
    }


def _scanner(tmp_path, **overrides):
    defaults = dict(
        client_id="abc",
        token_path=tmp_path / "microsoft_token.json",
    )
    defaults.update(overrides)
    return OutlookMailScanner(**defaults)


# ---------- Happy paths ----------

class TestListActiveTasks:
    def test_extracts_single_task(self, tmp_path, patch_graph):
        fg = patch_graph([_msg("m1", subject="Report due Friday",
                               body="Draft the quarterly report by Fri EOD")])
        fake = _FakeAnthropicClient([
            '[{"title":"Draft quarterly report","duration_min":90,'
            '"deadline_ts":"2026-04-25T22:00:00Z","priority":"high"}]',
        ])
        s = _scanner(tmp_path, anthropic_client=fake)
        out = s.list_active_tasks()
        assert len(out) == 1
        t = out[0]
        assert t.source == "outlook_mail"
        assert t.source_id == "m1::0"
        assert t.title == "Draft quarterly report"
        assert t.duration_hint_min == 90
        assert t.priority_hint == "high"
        assert t.deadline_utc == datetime(2026, 4, 25, 22, tzinfo=timezone.utc)

    def test_multiple_tasks_per_email(self, tmp_path, patch_graph):
        fg = patch_graph([_msg("m1")])
        fake = _FakeAnthropicClient([
            '[{"title":"Task A","duration_min":30,"deadline_ts":null,"priority":"medium"},'
            '{"title":"Task B","duration_min":60,"deadline_ts":null,"priority":"low"}]',
        ])
        s = _scanner(tmp_path, anthropic_client=fake)
        out = s.list_active_tasks()
        ids = [t.source_id for t in out]
        assert ids == ["m1::0", "m1::1"]
        titles = [t.title for t in out]
        assert titles == ["Task A", "Task B"]

    def test_html_body_is_stripped(self, tmp_path, patch_graph):
        patch_graph([_msg("m1",
                          body="<p>Please <b>review</b> the&nbsp;doc.</p>",
                          content_type="html")])
        fake = _FakeAnthropicClient(['[{"title":"Review doc","duration_min":30}]'])
        s = _scanner(tmp_path, anthropic_client=fake)
        s.list_active_tasks()
        # The extractor sees the stripped body in its user message.
        user_msg = fake.last_kwargs["messages"][0]["content"]
        assert "<p>" not in user_msg
        assert "<b>" not in user_msg
        assert "Please review the doc." in user_msg

    def test_one_bad_message_does_not_crash_batch(self, tmp_path, patch_graph):
        # Two messages: the first one has a malformed body structure
        # that raises inside _extract_from_message (body is a string
        # where dict is expected, so body.get(...) blows up with
        # AttributeError). The second one is fine.
        bad = {"id": "m1", "subject": "weird", "body": "not-a-dict"}
        good = _msg("m2")
        patch_graph([bad, good])
        fake = _FakeAnthropicClient([
            '[{"title":"From good","duration_min":15}]',
        ])
        s = _scanner(tmp_path, anthropic_client=fake)
        out = s.list_active_tasks()
        assert len(out) == 1
        assert out[0].source_id.startswith("m2::")

    def test_category_filter_narrows_query(self, tmp_path, patch_graph):
        fg = patch_graph([])
        s = _scanner(tmp_path,
                     category_filter=["TODO", "Follow up"],
                     anthropic_client=_FakeAnthropicClient([]))
        s.list_active_tasks()
        # The filter should appear in the $filter param.
        assert fg.get_calls
        _, params = fg.get_calls[0]
        f = params["$filter"]
        assert "isRead eq false" in f
        assert "TODO" in f
        assert "Follow up" in f

    def test_odata_escapes_single_quotes(self, tmp_path, patch_graph):
        fg = patch_graph([])
        s = _scanner(tmp_path,
                     category_filter=["Joe's"],
                     anthropic_client=_FakeAnthropicClient([]))
        s.list_active_tasks()
        _, params = fg.get_calls[0]
        assert "Joe''s" in params["$filter"]

    def test_returns_empty_on_auth_failure(self, tmp_path, monkeypatch):
        def _raise(**kw):
            raise RuntimeError("no token")
        monkeypatch.setattr(mod.mg, "get_access_token", _raise)

        s = _scanner(tmp_path, anthropic_client=_FakeAnthropicClient([]))
        assert s.list_active_tasks() == []

    def test_returns_empty_on_graph_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mod.mg, "get_access_token",
                            lambda **kw: "tok")
        def _raise(token, path, params=None, **kw):
            raise RuntimeError("500")
        monkeypatch.setattr(mod.mg, "graph_get", _raise)

        s = _scanner(tmp_path, anthropic_client=_FakeAnthropicClient([]))
        assert s.list_active_tasks() == []

    def test_max_messages_is_sent_as_top(self, tmp_path, patch_graph):
        fg = patch_graph([])
        _scanner(tmp_path, max_messages=7,
                 anthropic_client=_FakeAnthropicClient([])).list_active_tasks()
        _, params = fg.get_calls[0]
        assert params["$top"] == "7"


# ---------- health_check ----------

class TestHealthCheck:
    def test_reports_missing_token(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mod.mg, "token_status",
                            lambda *, token_path: {"token_present": False,
                                                    "accounts": []})
        info = _scanner(tmp_path).health_check()
        assert info["token_present"] is False
        assert "authorize_microsoft" in info["error"]

    def test_reports_ready(self, tmp_path, patch_graph):
        patch_graph([])
        info = _scanner(tmp_path).health_check()
        assert info["token_present"] is True
        assert info["source"] == "outlook_mail"
        assert "error" not in info
