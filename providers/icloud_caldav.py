"""iCloud CalDAV implementation of CalendarProvider.

Encapsulates every iCloud-specific quirk documented in ROADBLOCKS.md §I1-I7:
- Uses primary Apple ID + app-specific password (username=primary email)
- Bypasses the caldav library's event_by_uid() (which returns 412 on iCloud)
  by iterating cal.search() and matching UID in Python
- Writes via caldav's save_event, deletes via raw HTTP to skip If-Match (ETag
  mismatches are frequent because iCloud mutates events server-side)
- Handles both list-valued and comma-separated CATEGORIES
- Caches the principal() connection but exposes reset() so callers can invalidate
  on keepalive timeouts

AUTO marker serialization on CalDAV:
- CATEGORIES:AUTO-SCHED
- X-AGENT-TASK-ID:<task_id>    (custom X- property)

Both are preserved across read/write, so a round-trip is stable.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import caldav
import requests

from .base import CalendarEvent, CalendarProvider


AUTO_TAG = "AUTO-SCHED"
UTC = ZoneInfo("UTC")


class ICloudCalDAVProvider(CalendarProvider):
    """iCloud CalDAV provider.

    Parameters
    ----------
    username: str
        Primary Apple ID email. This is often NOT your @icloud.com alias —
        it's whatever email appears at the top of appleid.apple.com's
        "Sign-In and Security" page.
    app_password: str
        App-specific password from appleid.apple.com (NOT your Apple ID
        password, NOT a passkey). Format: xxxx-xxxx-xxxx-xxxx.
    write_calendar_name: str
        Name of the iCloud calendar the solver writes AUTO events to. The
        user must have created this calendar in Calendar.app first. Default:
        "Study Blocks".
    read_calendar_allowlist: optional list[str]
        If provided, only include these named calendars when reading "main"
        events (busy-time context). If None (default), read every non-write
        calendar. Use an allowlist to exclude shared family calendars etc.
    timeout: int
        Per-request timeout in seconds for raw HTTP (delete) calls.
    """

    def __init__(
        self,
        *,
        username: str,
        app_password: str,
        write_calendar_name: str = "Study Blocks",
        read_calendar_allowlist: Optional[list[str]] = None,
        url: str = "https://caldav.icloud.com/",
        timeout: int = 20,
    ):
        """
        url: CalDAV server URL. Defaults to iCloud. Other known endpoints:
            Fastmail     → https://caldav.fastmail.com/
            Posteo       → https://posteo.de:8443/
            Mailbox.org  → https://dav.mailbox.org/
            Nextcloud    → https://your-nextcloud/remote.php/dav/
            Radicale     → your self-hosted Radicale URL
            Apple Server → https://<host>:<port>/
        For those, `username` is whatever the service uses (email, login
        handle, …), and `app_password` is the service's equivalent — a
        "mail/CalDAV password" on Fastmail, a regular password on
        self-hosted installs, etc.
        """
        self.username = username
        self.app_password = app_password
        self.write_calendar_name = write_calendar_name
        self.read_allowlist = (
            {n.lower() for n in read_calendar_allowlist}
            if read_calendar_allowlist is not None else None
        )
        self.url = url
        self.timeout = timeout
        self._principal: Optional[caldav.Principal] = None

    # ---- CalendarProvider interface ----

    def list_events(
        self, start_utc: datetime, end_utc: datetime, *,
        include_write_calendar: bool = True,
        include_read_calendars: bool = True,
    ) -> list[CalendarEvent]:
        out: list[CalendarEvent] = []
        write_lower = self.write_calendar_name.lower()
        for cal in self._principal_conn().calendars():
            cal_name = cal.name or ""
            is_write = cal_name.lower() == write_lower
            if is_write and not include_write_calendar:
                continue
            if not is_write and not include_read_calendars:
                continue
            if (not is_write and self.read_allowlist is not None
                    and cal_name.lower() not in self.read_allowlist):
                continue
            try:
                for ev in cal.search(start=start_utc, end=end_utc,
                                     event=True, expand=True):
                    parsed = self._parse_vevent(ev, cal_name)
                    if parsed is not None:
                        out.append(parsed)
            except Exception:
                # Skip broken calendars rather than fail the whole query;
                # iCloud occasionally returns errors for shared calendars.
                continue
        return out

    def create_auto_event(
        self, *, start_utc: datetime, end_utc: datetime,
        title: str, notes: str = "",
        task_id: Optional[int] = None,
    ) -> CalendarEvent:
        cal = self._write_calendar()
        if cal is None:
            raise RuntimeError(
                f"Write calendar '{self.write_calendar_name}' not found on iCloud. "
                f"Create it in Calendar.app first (File → New Calendar → iCloud)."
            )
        uid = str(uuid.uuid4())
        ical = self._build_ical(
            uid=uid,
            start_utc=_ensure_utc(start_utc),
            end_utc=_ensure_utc(end_utc),
            title=title, notes=notes,
            extras=self._auto_properties(task_id),
        )
        cal.save_event(ical=ical)
        return CalendarEvent(
            id=uid, source_handle=None,  # deletes fall back to UID lookup
            title=title,
            start=_ensure_utc(start_utc), end=_ensure_utc(end_utc),
            notes=notes, is_auto=True, auto_task_id=task_id,
            calendar_name=cal.name or self.write_calendar_name,
        )

    def delete_event(self, event: CalendarEvent) -> None:
        # Prefer the fast path: known source_handle (URL) → raw DELETE.
        if event.source_handle:
            self._raw_delete(event.source_handle)
            return
        # Fallback: scan the write calendar, match UID, then DELETE by URL.
        # Necessary for events we created via save_event (which doesn't return
        # a URL in the form we can reuse without another round-trip).
        cal = self._write_calendar()
        if cal is None:
            raise RuntimeError("Write calendar not found")
        for ev in cal.search(event=True, expand=False):
            try:
                if str(ev.vobject_instance.vevent.uid.value) == event.id:
                    self._raw_delete(str(ev.url))
                    return
            except Exception:
                continue
        raise RuntimeError(f"Event {event.id!r} not found for delete")

    def reset(self) -> None:
        self._principal = None

    def health_check(self) -> dict:
        info = {
            "provider": "ICloudCalDAVProvider",
            "server_url": self.url,
            "write_calendar": self.write_calendar_name,
            "read_allowlist": sorted(self.read_allowlist) if self.read_allowlist else "all",
        }
        try:
            cals = [c.name for c in self._principal_conn().calendars()]
            info["write_calendar_exists"] = any(
                (n or "").lower() == self.write_calendar_name.lower() for n in cals
            )
            info["total_calendars_visible"] = len(cals)
        except Exception as ex:
            info["error"] = f"{type(ex).__name__}: {ex}"
        return info

    # ---- Internals ----

    def _principal_conn(self) -> caldav.Principal:
        if self._principal is None:
            client = caldav.DAVClient(
                url=self.url,
                username=self.username, password=self.app_password,
            )
            self._principal = client.principal()
        return self._principal

    def _write_calendar(self):
        target = self.write_calendar_name.lower()
        for cal in self._principal_conn().calendars():
            if (cal.name or "").lower() == target:
                return cal
        return None

    def _raw_delete(self, url: str) -> None:
        # Raw DELETE intentionally omits If-Match (caldav library would send
        # one, and iCloud returns 412 Precondition Failed when its ETag
        # diverges from ours — which happens routinely because it mutates
        # events server-side). Skipping the check is force-overwrite behavior,
        # safe here because we only touch our own AUTO events.
        r = requests.delete(url,
                            auth=(self.username, self.app_password),
                            timeout=self.timeout)
        if r.status_code >= 400:
            raise RuntimeError(f"DELETE {url} -> {r.status_code}: {r.text[:200]}")

    # ---- VEVENT parsing + construction ----

    def _parse_vevent(self, caldav_event, cal_name: str) -> Optional[CalendarEvent]:
        try:
            v = caldav_event.vobject_instance.vevent
        except Exception:
            return None
        try:
            uid = str(v.uid.value)
            start = _ensure_utc(v.dtstart.value) if hasattr(v, "dtstart") else None
            end = _ensure_utc(v.dtend.value) if hasattr(v, "dtend") else None
        except Exception:
            return None
        if start is None or end is None:
            return None
        cats = _extract_categories(v)
        task_id_str = _extract_x_task_id(v)
        task_id = None
        if task_id_str is not None:
            try:
                task_id = int(task_id_str)
            except ValueError:
                task_id = None
        return CalendarEvent(
            id=uid,
            source_handle=str(caldav_event.url) if caldav_event.url else None,
            title=str(v.summary.value) if hasattr(v, "summary") else "",
            start=start, end=end,
            notes=str(v.description.value) if hasattr(v, "description") else "",
            is_auto=AUTO_TAG in cats,
            auto_task_id=task_id,
            calendar_name=cal_name,
        )

    @staticmethod
    def _auto_properties(task_id: Optional[int]) -> list[str]:
        lines = [f"CATEGORIES:{AUTO_TAG}"]
        if task_id is not None:
            lines.append(f"X-AGENT-TASK-ID:{task_id}")
        return lines

    @staticmethod
    def _build_ical(*, uid: str, start_utc: datetime, end_utc: datetime,
                    title: str, notes: str, extras: list[str]) -> str:
        fmt = "%Y%m%dT%H%M%SZ"
        extra_block = ("\r\n".join(extras) + "\r\n") if extras else ""
        return (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//schedule-agent//EN\r\n"
            "BEGIN:VEVENT\r\n"
            f"UID:{uid}\r\n"
            f"DTSTAMP:{datetime.utcnow().strftime(fmt)}\r\n"
            f"DTSTART:{start_utc.strftime(fmt)}\r\n"
            f"DTEND:{end_utc.strftime(fmt)}\r\n"
            f"SUMMARY:{_escape_ical_text(title)}\r\n"
            f"DESCRIPTION:{_escape_ical_text(notes)}\r\n"
            f"{extra_block}"
            "END:VEVENT\r\nEND:VCALENDAR\r\n"
        )


# ---- Module-private helpers ----

def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _extract_categories(vevent) -> list[str]:
    if not hasattr(vevent, "categories"):
        return []
    raw = vevent.categories.value
    if isinstance(raw, list):
        return [str(x).strip() for x in raw]
    if isinstance(raw, str):
        return [p.strip() for p in raw.split(",")]
    return []


def _extract_x_task_id(vevent) -> Optional[str]:
    for child in (vevent.getChildren() if hasattr(vevent, "getChildren") else []):
        if getattr(child, "name", "").upper() == "X-AGENT-TASK-ID":
            return str(child.value)
    return None


def _escape_ical_text(s: str) -> str:
    return (
        (s or "")
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


# Convenience alias: same class, different default name. Use this when
# you're pointing at Fastmail / Posteo / Nextcloud / self-hosted Radicale.
# Behaviorally identical; the name just makes schedule_config.py clearer.
GenericCalDAVProvider = ICloudCalDAVProvider
