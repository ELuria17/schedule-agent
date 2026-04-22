"""Tests for providers/email_task_extractor.py — mocked at the Anthropic
client layer. We never hit the network.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from providers.email_task_extractor import extract_tasks_from_email


class _FakeClient:
    def __init__(self, text: str):
        self._text = text
        self.messages = self
        self.last_kwargs: dict | None = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return SimpleNamespace(
            content=[SimpleNamespace(text=self._text)],
        )


def _call(text: str, **kwargs) -> list[dict]:
    c = _FakeClient(text)
    return extract_tasks_from_email(
        subject="Q3 proposal — next steps",
        sender="pm@acme.com",
        date_iso="2026-04-22T09:00:00Z",
        body="Please finalize the draft by EOD Friday.",
        anthropic_client=c,
        now_iso="2026-04-22T14:00:00Z",
        **kwargs,
    )


def test_parses_simple_json_array():
    out = _call('[{"title":"Finalize Q3 proposal","duration_min":90,'
               '"deadline_ts":"2026-04-24T22:00:00Z","priority":"high"}]')
    assert len(out) == 1
    t = out[0]
    assert t["title"] == "Finalize Q3 proposal"
    assert t["duration_min"] == 90
    assert t["deadline_ts"] == "2026-04-24T22:00:00Z"
    assert t["priority"] == "high"


def test_empty_array_returns_empty():
    assert _call("[]") == []


def test_markdown_fences_get_stripped():
    out = _call('```json\n[{"title":"Book the venue","duration_min":30}]\n```')
    assert len(out) == 1
    assert out[0]["title"] == "Book the venue"


def test_plain_fence_without_language_tag():
    out = _call('```\n[{"title":"Send invoice","duration_min":15}]\n```')
    assert len(out) == 1
    assert out[0]["title"] == "Send invoice"


def test_invalid_json_returns_empty():
    assert _call("this is not json at all") == []


def test_non_array_returns_empty():
    assert _call('{"title":"x"}') == []


def test_drops_items_without_title():
    out = _call('[{"duration_min":30},{"title":"Ok","duration_min":30}]')
    assert len(out) == 1
    assert out[0]["title"] == "Ok"


def test_clamps_duration_to_reasonable_range():
    out = _call('[{"title":"Tiny","duration_min":0},'
                '{"title":"Huge","duration_min":99999}]')
    assert [x["duration_min"] for x in out] == [5, 480]


def test_defaults_bad_priority_to_medium():
    out = _call('[{"title":"X","duration_min":30,"priority":"MAYBE"}]')
    assert out[0]["priority"] == "medium"


def test_missing_duration_defaults_to_45():
    out = _call('[{"title":"X"}]')
    assert out[0]["duration_min"] == 45


def test_body_gets_capped():
    long_body = "A" * 20000
    c = _FakeClient("[]")
    extract_tasks_from_email(
        subject="s", sender="x@y.com", date_iso="2026-04-22T00:00:00Z",
        body=long_body, anthropic_client=c, body_char_cap=100,
    )
    sent = c.last_kwargs["messages"][0]["content"]
    # The A-block in the prompt should be exactly 100 chars long.
    assert "A" * 100 in sent
    assert "A" * 101 not in sent


def test_api_exception_returns_empty():
    class Boom:
        messages = SimpleNamespace(
            create=lambda **kw: (_ for _ in ()).throw(RuntimeError("network")),
        )
    out = extract_tasks_from_email(
        subject="s", sender="x@y.com", date_iso="2026-04-22T00:00:00Z",
        body="b", anthropic_client=Boom(),
    )
    assert out == []
