"""Tests for providers/google_calendar.py — mocked at the googleapiclient layer.

We fake the chain `discovery.build() → service.events().list().execute()` and
`service.calendarList().list().execute()` with scripted response dicts. The
provider's OAuth/token loading path is bypassed by monkey-patching
`GoogleCalendarProvider._load_credentials` to return a sentinel; we never
import google.oauth2 here.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from providers import google_calendar as mod
from providers.google_calendar import (
    AUTO_KEY,
    GoogleCalendarProvider,
    TASK_ID_KEY,
    _parse_rfc3339,
    _rfc3339,
)
from providers.base import CalendarEvent


# ---------- Fake googleapiclient service ----------

class _FakeExec:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _FakeEvents:
    def __init__(self, list_pages=None, insert_response=None):
        self.list_pages = list_pages or [{"items": []}]
        self.list_calls: list[dict] = []
        self.insert_calls: list[dict] = []
        self.delete_calls: list[dict] = []
        self.insert_response = insert_response or {"id": "inserted-id"}

    def list(self, **kw):
        self.list_calls.append(kw)
        page_token = kw.get("pageToken")
        idx = 0 if page_token is None else int(page_token)
        page = self.list_pages[idx] if idx < len(self.list_pages) else {"items": []}
        return _FakeExec(page)

    def insert(self, *, calendarId, body):
        self.insert_calls.append({"calendarId": calendarId, "body": body})
        return _FakeExec(self.insert_response)

    def delete(self, *, calendarId, eventId):
        self.delete_calls.append({"calendarId": calendarId, "eventId": eventId})
        return _FakeExec({})


class _FakeCalendarList:
    def __init__(self, items):
        self._items = items

    def list(self):
        return _FakeExec({"items": self._items})


class _FakeService:
    def __init__(self, calendars, events):
        self._calendars = calendars
        self._events = events

    def calendarList(self):
        return _FakeCalendarList(self._calendars)

    def events(self):
        return self._events


@pytest.fixture
def patch_build(monkeypatch):
    """Returns a setter that installs a fake service for the next _svc() call."""
    state = {"service": None}

    def _fake_build(api, version, credentials=None, cache_discovery=True):
        assert api == "calendar"
        assert version == "v3"
        return state["service"]

    # Inject a fake `googleapiclient.discovery.build` that the provider
    # imports inside `_svc()`. We install a tiny shim module so the inline
    # `from googleapiclient.discovery import build` succeeds.
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
    """Bypass the real OAuth credential loader."""
    monkeypatch.setattr(
        GoogleCalendarProvider, "_load_credentials",
        lambda self: SimpleNamespace(token="fake"),
    )


def _provider(tmp_path, **overrides):
    defaults = dict(
        credentials_path=tmp_path / "credentials.json",
        token_path=tmp_path / "google_token.json",
        write_calendar_name="Study Blocks",
    )
    defaults.update(overrides)
    return GoogleCalendarProvider(**defaults)


def _event(evid, start_iso, end_iso, *, summary="", description="",
           auto=False, task_id=None, all_day=False):
    e = {
        "id": evid,
        "summary": summary,
        "description": description,
    }
    if all_day:
        e["start"] = {"date": "2026-04-22"}
        e["end"] = {"date": "2026-04-23"}
    else:
        e["start"] = {"dateTime": start_iso}
        e["end"] = {"dateTime": end_iso}
    if auto:
        private = {AUTO_KEY: "1"}
        if task_id is not None:
            private[TASK_ID_KEY] = str(task_id)
        e["extendedProperties"] = {"private": private}
    return e


# ---------- Helper round-trip ----------

def test_rfc3339_roundtrip():
    dt = datetime(2026, 4, 22, 10, 30, tzinfo=timezone.utc)
    assert _rfc3339(dt) == "2026-04-22T10:30:00Z"
    assert _parse_rfc3339("2026-04-22T10:30:00Z") == dt


def test_rfc3339_parses_offset():
    dt = _parse_rfc3339("2026-04-22T10:30:00-04:00")
    assert dt.tzinfo is not None
    assert dt.astimezone(timezone.utc).hour == 14


# ---------- list_events ----------

def test_list_events_includes_both_write_and_read(tmp_path, patch_build, stub_creds):
    events_fake = _FakeEvents(list_pages=[{"items": [
        _event("a", "2026-04-22T10:00:00Z", "2026-04-22T11:00:00Z", summary="Class"),
    ]}])
    calendars = [
        {"id": "write-cal", "summary": "Study Blocks"},
        {"id": "school-cal", "summary": "School"},
    ]
    patch_build(_FakeService(calendars, events_fake))

    p = _provider(tmp_path)
    start = datetime(2026, 4, 22, tzinfo=timezone.utc)
    end = datetime(2026, 4, 23, tzinfo=timezone.utc)
    out = p.list_events(start, end)

    # One event per calendar (the fake returns the same page for every call).
    assert len(out) == 2
    # Both time_min/max passed to Google should be RFC3339 UTC.
    for call in events_fake.list_calls:
        assert call["timeMin"] == "2026-04-22T00:00:00Z"
        assert call["timeMax"] == "2026-04-23T00:00:00Z"
        assert call["singleEvents"] is True


def test_list_events_excludes_write_calendar_when_requested(tmp_path, patch_build, stub_creds):
    events_fake = _FakeEvents(list_pages=[{"items": [
        _event("x", "2026-04-22T10:00:00Z", "2026-04-22T11:00:00Z"),
    ]}])
    calendars = [
        {"id": "write-cal", "summary": "Study Blocks"},
        {"id": "school-cal", "summary": "School"},
    ]
    patch_build(_FakeService(calendars, events_fake))

    p = _provider(tmp_path)
    p.list_events(
        datetime(2026, 4, 22, tzinfo=timezone.utc),
        datetime(2026, 4, 23, tzinfo=timezone.utc),
        include_write_calendar=False,
    )
    # Only the non-write calendar should be hit.
    assert len(events_fake.list_calls) == 1
    assert events_fake.list_calls[0]["calendarId"] == "school-cal"


def test_list_events_respects_read_allowlist(tmp_path, patch_build, stub_creds):
    events_fake = _FakeEvents()
    calendars = [
        {"id": "write-cal", "summary": "Study Blocks"},
        {"id": "school-cal", "summary": "School"},
        {"id": "family-cal", "summary": "Family"},
    ]
    patch_build(_FakeService(calendars, events_fake))

    p = _provider(tmp_path, read_calendar_allowlist=["School"])
    p.list_events(
        datetime(2026, 4, 22, tzinfo=timezone.utc),
        datetime(2026, 4, 23, tzinfo=timezone.utc),
    )
    hit = {c["calendarId"] for c in events_fake.list_calls}
    assert hit == {"write-cal", "school-cal"}  # family excluded


def test_list_events_sets_auto_flag(tmp_path, patch_build, stub_creds):
    events_fake = _FakeEvents(list_pages=[{"items": [
        _event("a", "2026-04-22T10:00:00Z", "2026-04-22T11:00:00Z",
               summary="AutoPlan: Chapter 8", auto=True, task_id=42),
        _event("b", "2026-04-22T12:00:00Z", "2026-04-22T13:00:00Z",
               summary="Lunch with friend", auto=False),
    ]}])
    patch_build(_FakeService(
        [{"id": "write-cal", "summary": "Study Blocks"}], events_fake,
    ))

    p = _provider(tmp_path)
    out = p.list_events(
        datetime(2026, 4, 22, tzinfo=timezone.utc),
        datetime(2026, 4, 23, tzinfo=timezone.utc),
    )
    by_title = {e.title: e for e in out}
    assert by_title["AutoPlan: Chapter 8"].is_auto is True
    assert by_title["AutoPlan: Chapter 8"].auto_task_id == 42
    assert by_title["Lunch with friend"].is_auto is False


def test_list_events_skips_allday(tmp_path, patch_build, stub_creds):
    events_fake = _FakeEvents(list_pages=[{"items": [
        _event("h", "", "", summary="Holiday", all_day=True),
        _event("m", "2026-04-22T10:00:00Z", "2026-04-22T11:00:00Z", summary="Meeting"),
    ]}])
    patch_build(_FakeService(
        [{"id": "write-cal", "summary": "Study Blocks"}], events_fake,
    ))

    out = _provider(tmp_path).list_events(
        datetime(2026, 4, 22, tzinfo=timezone.utc),
        datetime(2026, 4, 23, tzinfo=timezone.utc),
    )
    assert [e.title for e in out] == ["Meeting"]


def test_list_events_paginates(tmp_path, patch_build, stub_creds):
    page0 = {
        "items": [_event("a", "2026-04-22T09:00:00Z", "2026-04-22T10:00:00Z")],
        "nextPageToken": "1",
    }
    page1 = {
        "items": [_event("b", "2026-04-22T11:00:00Z", "2026-04-22T12:00:00Z")],
    }
    events_fake = _FakeEvents(list_pages=[page0, page1])
    patch_build(_FakeService(
        [{"id": "write-cal", "summary": "Study Blocks"}], events_fake,
    ))

    out = _provider(tmp_path).list_events(
        datetime(2026, 4, 22, tzinfo=timezone.utc),
        datetime(2026, 4, 23, tzinfo=timezone.utc),
    )
    assert [e.id for e in out] == ["a", "b"]
    assert [c.get("pageToken") for c in events_fake.list_calls] == [None, "1"]


# ---------- create_auto_event ----------

def test_create_auto_event_round_trip(tmp_path, patch_build, stub_creds):
    events_fake = _FakeEvents(insert_response={"id": "new-evt-1"})
    patch_build(_FakeService(
        [{"id": "write-cal-id", "summary": "Study Blocks"}], events_fake,
    ))

    p = _provider(tmp_path)
    start = datetime(2026, 4, 22, 14, 0, tzinfo=timezone.utc)
    end = datetime(2026, 4, 22, 15, 30, tzinfo=timezone.utc)
    ev = p.create_auto_event(
        start_utc=start, end_utc=end,
        title="AutoPlan: Chapter 8", notes="focus block",
        task_id=42,
    )
    assert ev.is_auto is True
    assert ev.auto_task_id == 42
    assert ev.id == "new-evt-1"
    assert ev.source_handle == "write-cal-id/new-evt-1"
    assert len(events_fake.insert_calls) == 1
    body = events_fake.insert_calls[0]["body"]
    assert body["summary"] == "AutoPlan: Chapter 8"
    assert body["description"] == "focus block"
    assert body["start"]["dateTime"] == "2026-04-22T14:00:00Z"
    assert body["end"]["dateTime"] == "2026-04-22T15:30:00Z"
    assert body["extendedProperties"]["private"] == {
        AUTO_KEY: "1", TASK_ID_KEY: "42",
    }


def test_create_auto_event_without_task_id(tmp_path, patch_build, stub_creds):
    events_fake = _FakeEvents(insert_response={"id": "new-evt"})
    patch_build(_FakeService(
        [{"id": "write-cal", "summary": "Study Blocks"}], events_fake,
    ))

    ev = _provider(tmp_path).create_auto_event(
        start_utc=datetime(2026, 4, 22, 14, tzinfo=timezone.utc),
        end_utc=datetime(2026, 4, 22, 15, tzinfo=timezone.utc),
        title="Flex",
    )
    body = events_fake.insert_calls[0]["body"]
    assert body["extendedProperties"]["private"] == {AUTO_KEY: "1"}
    assert TASK_ID_KEY not in body["extendedProperties"]["private"]
    assert ev.auto_task_id is None


def test_create_auto_event_errors_when_write_calendar_missing(tmp_path, patch_build, stub_creds):
    events_fake = _FakeEvents()
    patch_build(_FakeService(
        [{"id": "other", "summary": "Other Cal"}], events_fake,
    ))
    with pytest.raises(RuntimeError, match="Study Blocks"):
        _provider(tmp_path).create_auto_event(
            start_utc=datetime(2026, 4, 22, 14, tzinfo=timezone.utc),
            end_utc=datetime(2026, 4, 22, 15, tzinfo=timezone.utc),
            title="x",
        )


# ---------- delete_event ----------

def test_delete_event_uses_source_handle(tmp_path, patch_build, stub_creds):
    events_fake = _FakeEvents()
    patch_build(_FakeService(
        [{"id": "write-cal", "summary": "Study Blocks"}], events_fake,
    ))

    p = _provider(tmp_path)
    ev = CalendarEvent(
        id="evt-9", source_handle="write-cal/evt-9",
        title="x",
        start=datetime(2026, 4, 22, 10, tzinfo=timezone.utc),
        end=datetime(2026, 4, 22, 11, tzinfo=timezone.utc),
        is_auto=True,
    )
    p.delete_event(ev)
    assert events_fake.delete_calls == [
        {"calendarId": "write-cal", "eventId": "evt-9"},
    ]


def test_delete_event_falls_back_to_write_calendar(tmp_path, patch_build, stub_creds):
    events_fake = _FakeEvents()
    patch_build(_FakeService(
        [{"id": "write-cal", "summary": "Study Blocks"}], events_fake,
    ))

    ev = CalendarEvent(
        id="evt-no-handle", source_handle=None,
        title="x",
        start=datetime(2026, 4, 22, 10, tzinfo=timezone.utc),
        end=datetime(2026, 4, 22, 11, tzinfo=timezone.utc),
        is_auto=True,
    )
    _provider(tmp_path).delete_event(ev)
    assert events_fake.delete_calls == [
        {"calendarId": "write-cal", "eventId": "evt-no-handle"},
    ]


# ---------- health_check ----------

def test_health_check_reports_missing_credentials(tmp_path):
    p = _provider(tmp_path)  # neither file exists
    info = p.health_check()
    assert info["provider"] == "GoogleCalendarProvider"
    assert info["token_present"] is False
    assert "credentials.json not found" in info["error"]


def test_health_check_reports_missing_token(tmp_path):
    (tmp_path / "credentials.json").write_text("{}")
    p = _provider(tmp_path)
    info = p.health_check()
    assert info["token_present"] is False
    assert "refresh token" in info["error"].lower()


def test_health_check_reports_success(tmp_path, patch_build, stub_creds):
    (tmp_path / "credentials.json").write_text("{}")
    (tmp_path / "google_token.json").write_text("{}")
    events_fake = _FakeEvents()
    patch_build(_FakeService(
        [
            {"id": "write-cal", "summary": "Study Blocks"},
            {"id": "school", "summary": "School"},
        ],
        events_fake,
    ))
    info = _provider(tmp_path).health_check()
    assert info["write_calendar_exists"] is True
    assert info["total_calendars_visible"] == 2
    assert "error" not in info
