"""Tests for providers/outlook_calendar.py.

We mock the module's dependency on microsoft_graph_auth at the GET /
POST / DELETE + get_access_token level, not at msal. That gives us a
clean test of calendar parsing, write-calendar lookup, auto markers,
and health_check behavior without any Graph-specific plumbing leaking
in.

Mirrors the structure of tests/test_google_calendar.py so new Claude
readers can diff the two side-by-side.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from providers import outlook_calendar as mod
from providers.outlook_calendar import (
    AUTO_PROP_NAME,
    EXT_PROP_GUID,
    OutlookCalendarProvider,
    TASK_ID_PROP_NAME,
)
from providers.base import CalendarEvent


# ---------- Fake graph helpers ----------

class _FakeGraph:
    """Scripts every call through microsoft_graph_auth, recording args."""
    def __init__(self, *, calendars=None, events_by_cal=None,
                 post_response=None):
        self.calendars = calendars or []
        self.events_by_cal = events_by_cal or {}
        self.post_response = post_response or {"id": "new-evt-1"}

        self.get_calls: list[tuple[str, dict]] = []
        self.post_calls: list[tuple[str, dict]] = []
        self.delete_calls: list[str] = []

    def graph_get(self, token, path, params=None, **kw):
        self.get_calls.append((path, dict(params or {})))
        if path == "/me/calendars":
            return {"value": [dict(c) for c in self.calendars]}
        if "/calendarView" in path:
            # path is /me/calendars/{id}/calendarView
            cal_id = path.split("/me/calendars/")[1].split("/")[0]
            return {"value": list(self.events_by_cal.get(cal_id, []))}
        return {"value": []}

    def graph_post(self, token, path, body, **kw):
        self.post_calls.append((path, body))
        return dict(self.post_response)

    def graph_delete(self, token, path, **kw):
        self.delete_calls.append(path)
        return None

    def get_access_token(self, *, client_id, tenant="common",
                         token_path=None, scopes=None):
        return "fake-access-token"

    def token_status(self, *, token_path):
        return {"token_present": True, "accounts": ["u@example.com"]}


@pytest.fixture
def fake_graph(monkeypatch):
    fg = _FakeGraph()
    monkeypatch.setattr(mod.mg, "graph_get", fg.graph_get)
    monkeypatch.setattr(mod.mg, "graph_post", fg.graph_post)
    monkeypatch.setattr(mod.mg, "graph_delete", fg.graph_delete)
    monkeypatch.setattr(mod.mg, "get_access_token", fg.get_access_token)
    monkeypatch.setattr(mod.mg, "token_status", fg.token_status)
    return fg


def _provider(tmp_path, **overrides):
    defaults = dict(
        client_id="client-abc",
        token_path=tmp_path / "microsoft_token.json",
        write_calendar_name="Study Blocks",
    )
    defaults.update(overrides)
    return OutlookCalendarProvider(**defaults)


def _ev(evid, start_iso, end_iso, *, subject="", is_all_day=False,
        auto=False, task_id=None, notes=""):
    e = {
        "id": evid,
        "subject": subject,
        "body": {"contentType": "text", "content": notes},
        "isAllDay": bool(is_all_day),
        "start": {"dateTime": start_iso, "timeZone": "UTC"},
        "end": {"dateTime": end_iso, "timeZone": "UTC"},
    }
    props = []
    if auto:
        props.append({
            "id": f"String {{{EXT_PROP_GUID}}} Name {AUTO_PROP_NAME}",
            "value": "1",
        })
    if task_id is not None:
        props.append({
            "id": f"String {{{EXT_PROP_GUID}}} Name {TASK_ID_PROP_NAME}",
            "value": str(task_id),
        })
    e["singleValueExtendedProperties"] = props
    return e


# ---------- list_events ----------

class TestListEvents:
    def test_lists_events_across_calendars(self, tmp_path, fake_graph):
        fake_graph.calendars = [
            {"id": "write-id", "name": "Study Blocks"},
            {"id": "school-id", "name": "School"},
        ]
        fake_graph.events_by_cal = {
            "write-id": [_ev("a", "2026-04-22T10:00:00", "2026-04-22T11:00:00",
                             subject="AutoPlan: Chapter 8", auto=True, task_id=42)],
            "school-id": [_ev("b", "2026-04-22T12:00:00", "2026-04-22T13:00:00",
                              subject="Class")],
        }
        p = _provider(tmp_path)
        out = p.list_events(
            datetime(2026, 4, 22, tzinfo=timezone.utc),
            datetime(2026, 4, 23, tzinfo=timezone.utc),
        )
        titles = sorted(e.title for e in out)
        assert titles == ["AutoPlan: Chapter 8", "Class"]
        by_title = {e.title: e for e in out}
        assert by_title["AutoPlan: Chapter 8"].is_auto is True
        assert by_title["AutoPlan: Chapter 8"].auto_task_id == 42
        assert by_title["Class"].is_auto is False
        assert by_title["AutoPlan: Chapter 8"].source_handle == "write-id/a"

    def test_excludes_write_calendar_when_requested(self, tmp_path, fake_graph):
        fake_graph.calendars = [
            {"id": "write-id", "name": "Study Blocks"},
            {"id": "school-id", "name": "School"},
        ]
        fake_graph.events_by_cal = {
            "write-id": [_ev("a", "2026-04-22T10:00:00", "2026-04-22T11:00:00")],
            "school-id": [_ev("b", "2026-04-22T12:00:00", "2026-04-22T13:00:00")],
        }
        _provider(tmp_path).list_events(
            datetime(2026, 4, 22, tzinfo=timezone.utc),
            datetime(2026, 4, 23, tzinfo=timezone.utc),
            include_write_calendar=False,
        )
        hit_paths = [p for p, _ in fake_graph.get_calls if "/calendarView" in p]
        # Only school calendar should be hit; write shouldn't.
        assert all("school-id" in p for p in hit_paths)
        assert hit_paths  # sanity

    def test_respects_read_allowlist(self, tmp_path, fake_graph):
        fake_graph.calendars = [
            {"id": "write-id", "name": "Study Blocks"},
            {"id": "school-id", "name": "School"},
            {"id": "family-id", "name": "Family"},
        ]
        fake_graph.events_by_cal = {
            "write-id": [], "school-id": [], "family-id": [],
        }
        p = _provider(tmp_path, read_calendar_allowlist=["School"])
        p.list_events(
            datetime(2026, 4, 22, tzinfo=timezone.utc),
            datetime(2026, 4, 23, tzinfo=timezone.utc),
        )
        hit = {p for p, _ in fake_graph.get_calls if "/calendarView" in p}
        # Write + School, NOT Family.
        assert any("write-id" in p for p in hit)
        assert any("school-id" in p for p in hit)
        assert not any("family-id" in p for p in hit)

    def test_skips_all_day_events(self, tmp_path, fake_graph):
        fake_graph.calendars = [{"id": "write-id", "name": "Study Blocks"}]
        fake_graph.events_by_cal = {
            "write-id": [
                _ev("h", "2026-04-22T00:00:00", "2026-04-23T00:00:00",
                    subject="Holiday", is_all_day=True),
                _ev("m", "2026-04-22T10:00:00", "2026-04-22T11:00:00",
                    subject="Meeting"),
            ],
        }
        out = _provider(tmp_path).list_events(
            datetime(2026, 4, 22, tzinfo=timezone.utc),
            datetime(2026, 4, 23, tzinfo=timezone.utc),
        )
        assert [e.title for e in out] == ["Meeting"]

    def test_parses_fractional_second_datetime(self, tmp_path, fake_graph):
        fake_graph.calendars = [{"id": "write-id", "name": "Study Blocks"}]
        fake_graph.events_by_cal = {
            "write-id": [
                _ev("a", "2026-04-22T10:00:00.0000000", "2026-04-22T11:00:00.0000000",
                    subject="High-res"),
            ],
        }
        out = _provider(tmp_path).list_events(
            datetime(2026, 4, 22, tzinfo=timezone.utc),
            datetime(2026, 4, 23, tzinfo=timezone.utc),
        )
        assert len(out) == 1
        assert out[0].start == datetime(2026, 4, 22, 10, tzinfo=timezone.utc)
        assert out[0].end == datetime(2026, 4, 22, 11, tzinfo=timezone.utc)

    def test_unreadable_calendar_is_skipped(self, tmp_path, fake_graph, monkeypatch):
        fake_graph.calendars = [
            {"id": "ok", "name": "Study Blocks"},
            {"id": "broken", "name": "Broken"},
        ]
        fake_graph.events_by_cal = {
            "ok": [_ev("a", "2026-04-22T10:00:00", "2026-04-22T11:00:00",
                       subject="Fine")],
            "broken": [],
        }
        # Wrap graph_get so /broken/calendarView raises but everything else
        # falls through to the fake.
        real_get = fake_graph.graph_get

        def _get(token, path, params=None, **kw):
            if "broken" in path and "/calendarView" in path:
                raise RuntimeError("perm denied")
            return real_get(token, path, params=params, **kw)

        monkeypatch.setattr(mod.mg, "graph_get", _get)

        out = _provider(tmp_path).list_events(
            datetime(2026, 4, 22, tzinfo=timezone.utc),
            datetime(2026, 4, 23, tzinfo=timezone.utc),
        )
        assert [e.title for e in out] == ["Fine"]


# ---------- create_auto_event ----------

class TestCreateAutoEvent:
    def test_creates_event_with_ext_props(self, tmp_path, fake_graph):
        fake_graph.calendars = [{"id": "write-id", "name": "Study Blocks"}]
        fake_graph.post_response = {"id": "new-evt-77"}
        p = _provider(tmp_path)
        start = datetime(2026, 4, 22, 14, tzinfo=timezone.utc)
        end = datetime(2026, 4, 22, 15, 30, tzinfo=timezone.utc)
        ev = p.create_auto_event(
            start_utc=start, end_utc=end,
            title="AutoPlan: Chapter 8", notes="focus block",
            task_id=42,
        )
        assert ev.is_auto is True
        assert ev.auto_task_id == 42
        assert ev.id == "new-evt-77"
        assert ev.source_handle == "write-id/new-evt-77"

        assert len(fake_graph.post_calls) == 1
        path, body = fake_graph.post_calls[0]
        assert path == "/me/calendars/write-id/events"
        assert body["subject"] == "AutoPlan: Chapter 8"
        assert body["body"]["content"] == "focus block"
        assert body["start"]["dateTime"] == "2026-04-22T14:00:00"
        assert body["start"]["timeZone"] == "UTC"
        assert body["end"]["dateTime"] == "2026-04-22T15:30:00"
        props = body["singleValueExtendedProperties"]
        names = {p["id"]: p["value"] for p in props}
        auto_id = f"String {{{EXT_PROP_GUID}}} Name {AUTO_PROP_NAME}"
        task_id = f"String {{{EXT_PROP_GUID}}} Name {TASK_ID_PROP_NAME}"
        assert names[auto_id] == "1"
        assert names[task_id] == "42"

    def test_without_task_id_omits_task_property(self, tmp_path, fake_graph):
        fake_graph.calendars = [{"id": "write-id", "name": "Study Blocks"}]
        fake_graph.post_response = {"id": "new-evt"}
        ev = _provider(tmp_path).create_auto_event(
            start_utc=datetime(2026, 4, 22, 14, tzinfo=timezone.utc),
            end_utc=datetime(2026, 4, 22, 15, tzinfo=timezone.utc),
            title="Flex",
        )
        _, body = fake_graph.post_calls[0]
        ids = [p["id"] for p in body["singleValueExtendedProperties"]]
        assert any(AUTO_PROP_NAME in i for i in ids)
        assert not any(TASK_ID_PROP_NAME in i for i in ids)
        assert ev.auto_task_id is None

    def test_errors_when_write_calendar_missing(self, tmp_path, fake_graph):
        fake_graph.calendars = [{"id": "other", "name": "Other"}]
        with pytest.raises(RuntimeError, match="Study Blocks"):
            _provider(tmp_path).create_auto_event(
                start_utc=datetime(2026, 4, 22, 14, tzinfo=timezone.utc),
                end_utc=datetime(2026, 4, 22, 15, tzinfo=timezone.utc),
                title="x",
            )


# ---------- delete_event ----------

class TestDeleteEvent:
    def test_deletes_via_source_handle(self, tmp_path, fake_graph):
        fake_graph.calendars = [{"id": "write-id", "name": "Study Blocks"}]
        ev = CalendarEvent(
            id="evt-9", source_handle="write-id/evt-9",
            title="x",
            start=datetime(2026, 4, 22, 10, tzinfo=timezone.utc),
            end=datetime(2026, 4, 22, 11, tzinfo=timezone.utc),
            is_auto=True,
        )
        _provider(tmp_path).delete_event(ev)
        assert fake_graph.delete_calls == ["/me/calendars/write-id/events/evt-9"]

    def test_raises_without_source_handle(self, tmp_path, fake_graph):
        ev = CalendarEvent(
            id="evt-0", source_handle=None,
            title="x",
            start=datetime(2026, 4, 22, 10, tzinfo=timezone.utc),
            end=datetime(2026, 4, 22, 11, tzinfo=timezone.utc),
            is_auto=True,
        )
        with pytest.raises(RuntimeError, match="source_handle"):
            _provider(tmp_path).delete_event(ev)


# ---------- health_check ----------

class TestHealthCheck:
    def test_reports_missing_token(self, tmp_path, fake_graph, monkeypatch):
        # Override token_status to say absent.
        monkeypatch.setattr(mod.mg, "token_status",
                            lambda *, token_path: {"token_present": False,
                                                    "accounts": []})
        info = _provider(tmp_path).health_check()
        assert info["token_present"] is False
        assert "authorize_microsoft" in info["error"]

    def test_reports_success(self, tmp_path, fake_graph):
        fake_graph.calendars = [
            {"id": "write-id", "name": "Study Blocks"},
            {"id": "school-id", "name": "School"},
        ]
        info = _provider(tmp_path).health_check()
        assert info["token_present"] is True
        assert info["write_calendar_exists"] is True
        assert info["total_calendars_visible"] == 2
        assert "error" not in info

    def test_reports_missing_write_calendar(self, tmp_path, fake_graph):
        fake_graph.calendars = [{"id": "other", "name": "Other"}]
        info = _provider(tmp_path).health_check()
        assert info["token_present"] is True
        assert info["write_calendar_exists"] is False


# ---------- reset ----------

def test_reset_clears_caches(tmp_path, fake_graph):
    fake_graph.calendars = [{"id": "w", "name": "Study Blocks"}]
    p = _provider(tmp_path)
    p._access_token()  # populates _token + expiry
    p._write_calendar_id(p._access_token())  # populates _cal_id_cache
    assert p._token == "fake-access-token"
    assert p._cal_id_cache
    p.reset()
    assert p._token is None
    assert p._cal_id_cache == {}
