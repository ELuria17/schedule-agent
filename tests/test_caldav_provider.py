"""Tests for providers/icloud_caldav.py — mocked at the `caldav` library layer.

The production code wraps CalDAV access in `caldav.DAVClient → principal →
calendars → events`, plus a raw `requests.delete` path that bypasses the
library's If-Match handling (ROADBLOCKS §I3). Tests monkeypatch both
surfaces and drive the provider through its full contract without hitting
any real server.

Fake classes below mimic just enough of the caldav/vobject shape:
- `caldav.DAVClient(...)` → `_FakePrincipalClient`
- `client.principal()` → `_FakePrincipal`
- `principal.calendars()` → list of `_FakeCalendar`
- `cal.search(...)` / `cal.save_event(...)` → scripted behavior
- Each event has `.vobject_instance.vevent.{uid,dtstart,dtend,...}` +
  `.url` (so _parse_vevent and delete-by-URL both work).
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from providers import icloud_caldav as caldav_mod
from providers.icloud_caldav import (
    AUTO_TAG,
    ICloudCalDAVProvider,
    _ensure_utc,
    _extract_categories,
    _escape_ical_text,
)


# ---------- Fake caldav / vobject objects ----------

def _utc(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def _fake_vevent(uid, start, end, *, summary="", description="",
                 categories=None, x_task_id=None):
    """Return an object that mimics vobject's VEVENT attribute access."""
    vevent = SimpleNamespace(
        uid=SimpleNamespace(value=uid),
        dtstart=SimpleNamespace(value=start),
        dtend=SimpleNamespace(value=end),
    )
    if summary:
        vevent.summary = SimpleNamespace(value=summary)
    if description:
        vevent.description = SimpleNamespace(value=description)
    if categories is not None:
        vevent.categories = SimpleNamespace(value=categories)

    children = []
    if x_task_id is not None:
        children.append(SimpleNamespace(name="X-AGENT-TASK-ID", value=str(x_task_id)))
    vevent.getChildren = lambda: children
    return vevent


def _fake_event(url, vevent):
    return SimpleNamespace(
        url=url,
        vobject_instance=SimpleNamespace(vevent=vevent),
    )


class _FakeCalendar:
    def __init__(self, name: str, events: list | None = None):
        self.name = name
        self._events = events or []
        self.saved_icals: list[str] = []
        self.raise_on_search: Exception | None = None

    def search(self, start=None, end=None, *, event=False, expand=False):
        if self.raise_on_search:
            raise self.raise_on_search
        return list(self._events)

    def save_event(self, ical: str):
        self.saved_icals.append(ical)
        return None


class _FakePrincipal:
    def __init__(self, calendars: list[_FakeCalendar]):
        self._cals = calendars

    def calendars(self):
        return list(self._cals)


class _FakeClient:
    def __init__(self, principal: _FakePrincipal):
        self._principal = principal
        self.constructed_with: dict = {}

    def principal(self):
        return self._principal


@pytest.fixture
def fake_caldav(monkeypatch):
    """Install a fake `caldav.DAVClient` factory on the provider module.
    Returns a mutable dict so tests can reassign the calendar list used by
    the next principal() call."""
    state = {"calendars": []}

    def factory(*, url, username, password):
        client = _FakeClient(_FakePrincipal(state["calendars"]))
        client.constructed_with = {"url": url, "username": username,
                                   "password": password}
        state["last_client"] = client
        return client

    monkeypatch.setattr(caldav_mod.caldav, "DAVClient", factory)
    return state


@pytest.fixture
def fake_requests(monkeypatch):
    """Monkeypatch requests.delete on the provider module."""
    calls: list[dict] = []

    def fake_delete(url, auth=None, timeout=None):
        calls.append({"url": url, "auth": auth, "timeout": timeout})
        return SimpleNamespace(status_code=204, text="")

    monkeypatch.setattr(caldav_mod.requests, "delete", fake_delete)
    return calls


# ---------- Pure helpers ----------

class TestPureHelpers:
    def test_ensure_utc_adds_tz_when_missing(self):
        naive = datetime(2026, 4, 21, 12, 0)
        out = _ensure_utc(naive)
        assert out.tzinfo == caldav_mod.UTC
        assert out.hour == 12

    def test_ensure_utc_converts_from_other_tz(self):
        from zoneinfo import ZoneInfo
        ny = datetime(2026, 4, 21, 12, 0, tzinfo=ZoneInfo("America/New_York"))
        out = _ensure_utc(ny)
        assert out.tzinfo == caldav_mod.UTC
        assert out.hour == 16  # EDT = UTC-4

    def test_extract_categories_from_list(self):
        v = SimpleNamespace(categories=SimpleNamespace(value=["AUTO-SCHED", "Work"]))
        assert _extract_categories(v) == ["AUTO-SCHED", "Work"]

    def test_extract_categories_from_comma_string(self):
        """iCloud sometimes serializes categories as a comma-joined string."""
        v = SimpleNamespace(categories=SimpleNamespace(value="AUTO-SCHED, Urgent"))
        assert _extract_categories(v) == ["AUTO-SCHED", "Urgent"]

    def test_extract_categories_missing(self):
        v = SimpleNamespace()
        assert _extract_categories(v) == []

    def test_escape_ical_text(self):
        assert _escape_ical_text("foo, bar; baz\nqux") == "foo\\, bar\\; baz\\nqux"
        assert _escape_ical_text(None) == ""

    def test_auto_properties_with_task_id(self):
        lines = ICloudCalDAVProvider._auto_properties(42)
        assert f"CATEGORIES:{AUTO_TAG}" in lines
        assert "X-AGENT-TASK-ID:42" in lines

    def test_auto_properties_without_task_id(self):
        lines = ICloudCalDAVProvider._auto_properties(None)
        assert lines == [f"CATEGORIES:{AUTO_TAG}"]

    def test_build_ical_contains_required_fields(self):
        ical = ICloudCalDAVProvider._build_ical(
            uid="u-1", start_utc=_utc(2026, 4, 21, 9),
            end_utc=_utc(2026, 4, 21, 10),
            title="Test Event", notes="notes here",
            extras=[f"CATEGORIES:{AUTO_TAG}", "X-AGENT-TASK-ID:7"],
        )
        assert "BEGIN:VCALENDAR" in ical
        assert "UID:u-1" in ical
        assert "DTSTART:20260421T090000Z" in ical
        assert "DTEND:20260421T100000Z" in ical
        assert "SUMMARY:Test Event" in ical
        assert f"CATEGORIES:{AUTO_TAG}" in ical
        assert "X-AGENT-TASK-ID:7" in ical
        assert "END:VEVENT" in ical


# ---------- list_events ----------

class TestListEvents:
    def _provider(self):
        return ICloudCalDAVProvider(
            username="u@example.com", app_password="xxxx-xxxx-xxxx-xxxx",
            write_calendar_name="Study Blocks",
        )

    def test_returns_events_from_all_calendars(self, fake_caldav):
        vev1 = _fake_vevent("u-1", _utc(2026, 4, 21, 9), _utc(2026, 4, 21, 10),
                             summary="Class")
        vev2 = _fake_vevent("u-2", _utc(2026, 4, 21, 14), _utc(2026, 4, 21, 15),
                             summary="Study",
                             categories=["AUTO-SCHED"], x_task_id="5")
        fake_caldav["calendars"] = [
            _FakeCalendar("School", [_fake_event("https://cal/1.ics", vev1)]),
            _FakeCalendar("Study Blocks", [_fake_event("https://cal/2.ics", vev2)]),
        ]
        events = self._provider().list_events(
            _utc(2026, 4, 21, 0), _utc(2026, 4, 22, 0),
        )
        titles = {e.title for e in events}
        assert titles == {"Class", "Study"}
        auto_event = next(e for e in events if e.is_auto)
        assert auto_event.auto_task_id == 5
        assert auto_event.calendar_name == "Study Blocks"

    def test_passes_credentials_to_davclient(self, fake_caldav):
        fake_caldav["calendars"] = []
        self._provider().list_events(_utc(2026, 4, 21), _utc(2026, 4, 22))
        client = fake_caldav["last_client"]
        assert client.constructed_with["url"] == "https://caldav.icloud.com/"
        assert client.constructed_with["username"] == "u@example.com"
        assert client.constructed_with["password"] == "xxxx-xxxx-xxxx-xxxx"

    def test_include_write_calendar_false_excludes_write(self, fake_caldav):
        vev_school = _fake_vevent("s-1", _utc(2026, 4, 21, 9), _utc(2026, 4, 21, 10),
                                   summary="Class")
        vev_study = _fake_vevent("a-1", _utc(2026, 4, 21, 14), _utc(2026, 4, 21, 15),
                                  summary="Study", categories=["AUTO-SCHED"])
        fake_caldav["calendars"] = [
            _FakeCalendar("School", [_fake_event("u1", vev_school)]),
            _FakeCalendar("Study Blocks", [_fake_event("u2", vev_study)]),
        ]
        events = self._provider().list_events(
            _utc(2026, 4, 21), _utc(2026, 4, 22),
            include_write_calendar=False,
        )
        assert [e.title for e in events] == ["Class"]

    def test_include_read_calendars_false_keeps_only_write(self, fake_caldav):
        vev_school = _fake_vevent("s-1", _utc(2026, 4, 21, 9), _utc(2026, 4, 21, 10),
                                   summary="Class")
        vev_study = _fake_vevent("a-1", _utc(2026, 4, 21, 14), _utc(2026, 4, 21, 15),
                                  summary="Study", categories=["AUTO-SCHED"])
        fake_caldav["calendars"] = [
            _FakeCalendar("School", [_fake_event("u1", vev_school)]),
            _FakeCalendar("Study Blocks", [_fake_event("u2", vev_study)]),
        ]
        events = self._provider().list_events(
            _utc(2026, 4, 21), _utc(2026, 4, 22),
            include_read_calendars=False,
        )
        assert [e.title for e in events] == ["Study"]

    def test_read_allowlist_filters_out_unlisted_calendars(self, fake_caldav):
        def evs(name):
            return [_fake_event(f"u-{name}",
                                _fake_vevent(f"uid-{name}",
                                              _utc(2026, 4, 21, 9),
                                              _utc(2026, 4, 21, 10),
                                              summary=name))]
        fake_caldav["calendars"] = [
            _FakeCalendar("Family", evs("Family")),
            _FakeCalendar("Work", evs("Work")),
            _FakeCalendar("Study Blocks", []),
        ]
        p = ICloudCalDAVProvider(
            username="u@example.com", app_password="pw",
            write_calendar_name="Study Blocks",
            read_calendar_allowlist=["Work"],
        )
        events = p.list_events(_utc(2026, 4, 21), _utc(2026, 4, 22))
        assert [e.title for e in events] == ["Work"]

    def test_broken_calendar_does_not_abort_query(self, fake_caldav):
        good = _FakeCalendar("Good", [_fake_event("u-g",
                                                  _fake_vevent("uid-g",
                                                                _utc(2026, 4, 21, 9),
                                                                _utc(2026, 4, 21, 10),
                                                                summary="ok"))])
        bad = _FakeCalendar("Bad")
        bad.raise_on_search = RuntimeError("server error")
        fake_caldav["calendars"] = [bad, good, _FakeCalendar("Study Blocks")]
        events = self._provider().list_events(_utc(2026, 4, 21), _utc(2026, 4, 22))
        assert [e.title for e in events] == ["ok"]


# ---------- create_auto_event + delete_event ----------

class TestCreateAndDelete:
    def _provider(self):
        return ICloudCalDAVProvider(
            username="u@example.com", app_password="pw",
            write_calendar_name="Study Blocks",
        )

    def test_create_auto_event_saves_ical_with_marker(self, fake_caldav):
        write_cal = _FakeCalendar("Study Blocks")
        fake_caldav["calendars"] = [_FakeCalendar("School"), write_cal]
        ev = self._provider().create_auto_event(
            start_utc=_utc(2026, 4, 21, 10), end_utc=_utc(2026, 4, 21, 11),
            title="HW 1", notes="Chapter 3", task_id=99,
        )
        assert ev.is_auto
        assert ev.auto_task_id == 99
        assert ev.title == "HW 1"
        assert len(write_cal.saved_icals) == 1
        ical = write_cal.saved_icals[0]
        assert f"CATEGORIES:{AUTO_TAG}" in ical
        assert "X-AGENT-TASK-ID:99" in ical
        assert "SUMMARY:HW 1" in ical

    def test_create_auto_event_raises_when_write_calendar_missing(self, fake_caldav):
        fake_caldav["calendars"] = [_FakeCalendar("Not the write one")]
        with pytest.raises(RuntimeError, match="Write calendar 'Study Blocks' not found"):
            self._provider().create_auto_event(
                start_utc=_utc(2026, 4, 21, 10), end_utc=_utc(2026, 4, 21, 11),
                title="X",
            )

    def test_delete_event_with_source_handle_uses_raw_delete(
        self, fake_caldav, fake_requests
    ):
        fake_caldav["calendars"] = [_FakeCalendar("Study Blocks")]
        from providers.base import CalendarEvent
        ev = CalendarEvent(
            id="u-1", source_handle="https://cal/events/abc.ics",
            title="t", start=_utc(2026, 4, 21, 10), end=_utc(2026, 4, 21, 11),
            is_auto=True,
        )
        self._provider().delete_event(ev)
        assert len(fake_requests) == 1
        assert fake_requests[0]["url"] == "https://cal/events/abc.ics"
        assert fake_requests[0]["auth"] == ("u@example.com", "pw")

    def test_delete_event_falls_back_to_uid_scan(self, fake_caldav, fake_requests):
        """Events created via save_event don't carry a URL — we have to scan
        the write calendar and match by UID."""
        target_uid = "u-target"
        target_vev = _fake_vevent(target_uid, _utc(2026, 4, 21, 10),
                                   _utc(2026, 4, 21, 11), summary="match")
        other_vev = _fake_vevent("u-other", _utc(2026, 4, 21, 12),
                                  _utc(2026, 4, 21, 13), summary="skip")
        write = _FakeCalendar("Study Blocks", [
            _fake_event("https://cal/other.ics", other_vev),
            _fake_event("https://cal/target.ics", target_vev),
        ])
        fake_caldav["calendars"] = [write]

        from providers.base import CalendarEvent
        ev = CalendarEvent(
            id=target_uid, source_handle=None,
            title="t", start=_utc(2026, 4, 21, 10), end=_utc(2026, 4, 21, 11),
            is_auto=True,
        )
        self._provider().delete_event(ev)
        assert len(fake_requests) == 1
        assert fake_requests[0]["url"] == "https://cal/target.ics"

    def test_delete_event_raises_when_uid_not_found(self, fake_caldav):
        fake_caldav["calendars"] = [_FakeCalendar("Study Blocks")]
        from providers.base import CalendarEvent
        ev = CalendarEvent(
            id="does-not-exist", source_handle=None,
            title="t", start=_utc(2026, 4, 21, 10), end=_utc(2026, 4, 21, 11),
        )
        with pytest.raises(RuntimeError, match="not found for delete"):
            self._provider().delete_event(ev)


# ---------- health_check + reset ----------

class TestHealthAndReset:
    def test_health_ok_when_write_calendar_visible(self, fake_caldav):
        fake_caldav["calendars"] = [_FakeCalendar("School"), _FakeCalendar("Study Blocks")]
        p = ICloudCalDAVProvider(username="u", app_password="pw",
                                 write_calendar_name="Study Blocks")
        info = p.health_check()
        assert info["write_calendar_exists"] is True
        assert info["total_calendars_visible"] == 2
        assert "error" not in info

    def test_health_flags_missing_write_calendar(self, fake_caldav):
        fake_caldav["calendars"] = [_FakeCalendar("School"), _FakeCalendar("Family")]
        p = ICloudCalDAVProvider(username="u", app_password="pw",
                                 write_calendar_name="Study Blocks")
        info = p.health_check()
        assert info["write_calendar_exists"] is False

    def test_health_captures_exception(self, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("unreachable")
        monkeypatch.setattr(caldav_mod.caldav, "DAVClient", boom)
        p = ICloudCalDAVProvider(username="u", app_password="pw")
        info = p.health_check()
        assert "error" in info
        assert "RuntimeError" in info["error"]

    def test_reset_reconstructs_client_on_next_call(self, fake_caldav):
        fake_caldav["calendars"] = [_FakeCalendar("Study Blocks")]
        p = ICloudCalDAVProvider(username="u", app_password="pw",
                                 write_calendar_name="Study Blocks")
        p.list_events(_utc(2026, 4, 21), _utc(2026, 4, 22))
        first = fake_caldav["last_client"]
        p.reset()
        fake_caldav["calendars"] = [_FakeCalendar("Study Blocks")]  # new set
        p.list_events(_utc(2026, 4, 21), _utc(2026, 4, 22))
        second = fake_caldav["last_client"]
        assert first is not second, "reset() should drop the cached principal"


# ---------- retry-on-reconnect (ROADBLOCKS §I5) ----------

class TestRetryOnReconnect:
    def test_keepalive_timeout_recovers_via_reset_and_retry(self, monkeypatch):
        """A stale-connection error on the first call should silently reset
        the principal and retry once. Caller sees a successful result."""
        from urllib3.exceptions import ProtocolError

        attempt = {"n": 0}
        good_cals = [_FakeCalendar("Study Blocks")]

        def factory(*, url, username, password):
            attempt["n"] += 1
            client = _FakeClient(_FakePrincipal(good_cals))
            if attempt["n"] == 1:
                # Boom on the first principal() call only — second client works.
                client.principal = lambda: (_ for _ in ()).throw(
                    ProtocolError("keepalive timeout")
                )
            return client

        monkeypatch.setattr(caldav_mod.caldav, "DAVClient", factory)
        p = ICloudCalDAVProvider(username="u", app_password="pw",
                                 write_calendar_name="Study Blocks")
        # Should not raise; the decorator catches the ProtocolError, calls
        # reset(), and re-enters list_events which builds a fresh client.
        events = p.list_events(_utc(2026, 4, 21), _utc(2026, 4, 22))
        assert events == []
        assert attempt["n"] == 2  # one bad client + one good replacement

    def test_non_connection_error_does_not_retry(self, monkeypatch):
        """Auth / 4xx style errors should propagate on the first attempt —
        retrying them is just wasted round trips and could lock an account."""
        attempt = {"n": 0}

        def factory(*, url, username, password):
            attempt["n"] += 1
            raise PermissionError("401 unauthorized")

        monkeypatch.setattr(caldav_mod.caldav, "DAVClient", factory)
        p = ICloudCalDAVProvider(username="u", app_password="pw")
        with pytest.raises(PermissionError):
            p.list_events(_utc(2026, 4, 21), _utc(2026, 4, 22))
        assert attempt["n"] == 1  # exactly one attempt — no retry

    def test_second_failure_propagates(self, monkeypatch):
        """If reset+retry also hits a connection error, the second one bubbles
        up so the caller can decide what to do (mark at-risk, alert, etc)."""
        from urllib3.exceptions import ProtocolError

        attempt = {"n": 0}

        def factory(*, url, username, password):
            attempt["n"] += 1
            client = _FakeClient(_FakePrincipal([]))
            client.principal = lambda: (_ for _ in ()).throw(
                ProtocolError("still broken")
            )
            return client

        monkeypatch.setattr(caldav_mod.caldav, "DAVClient", factory)
        p = ICloudCalDAVProvider(username="u", app_password="pw")
        with pytest.raises(ProtocolError):
            p.list_events(_utc(2026, 4, 21), _utc(2026, 4, 22))
        assert attempt["n"] == 2  # one initial + one retry, then bubble
