"""Google Calendar implementation of CalendarProvider.

Uses the official googleapiclient library with an installed-app OAuth loopback
flow. Each user creates their own Google Cloud project + OAuth client (see
docs/google-calendar-setup.md), drops the downloaded credentials.json in the
app's data directory, then runs `python setup.py` (or calls
`provider.authorize_interactive()`) once to produce a refresh token. From then
on the provider runs headless.

AUTO marker serialization on Google:
- extendedProperties.private["autoplan_auto"] = "1"
- extendedProperties.private["autoplan_task_id"] = "<task_id>"  (optional)

Google's `extendedProperties.private` is calendar-local metadata only the
authenticating user can see/read; it survives round-trips and doesn't pollute
the visible event. `extended_properties=private_...` search syntax lets us
filter server-side if that ever matters, though today we just filter in Python
on read.

Write calendar lookup: by the user-facing "summary" (name) field of the
calendar, not by ID. The orchestrator never deals with Google's opaque
"abc123@group.calendar.google.com" IDs — internally we resolve the name to an
ID once per call. Users must create the target calendar in Google Calendar
first; we do not auto-create it (so we don't clutter their account on a typo).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .base import CalendarEvent, CalendarProvider


AUTO_KEY = "autoplan_auto"
TASK_ID_KEY = "autoplan_task_id"
# Default OAuth scopes requested by the installed-app flow. We widen past
# calendar because the same credentials.json / google_token.json pair is
# reused by GmailScanner (providers/gmail_scanner.py) and
# GoogleTasksProvider (providers/google_tasks.py). Authorizing all three
# up front means users who enable Gmail or Tasks later don't have to do a
# second OAuth dance — one consent screen covers every Google provider
# this app ships.
#
# Migration note: users who authorized before this scope widening will
# have a token missing gmail.readonly / tasks.readonly. Delete
# google_token.json and re-run `python authorize_google.py` once to
# re-consent; Google refuses to silently upgrade scopes.
DEFAULT_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/tasks.readonly",
]


class GoogleCalendarProvider(CalendarProvider):
    """Google Calendar provider.

    Parameters
    ----------
    credentials_path: str | Path
        Path to the `credentials.json` file downloaded from Google Cloud
        Console (APIs & Services → Credentials → OAuth 2.0 Client IDs →
        Download JSON). Must be a "Desktop" type client.
    token_path: str | Path
        Where the refresh token gets cached after the first successful
        OAuth consent. If the file doesn't exist, `authorize_interactive()`
        creates it. If it exists, the provider reuses it forever (with
        silent refresh).
    write_calendar_name: str
        Name of the Google calendar the solver writes AUTO events to. The
        user must have created this calendar in Google Calendar first
        (calendar.google.com → + next to "Other calendars" → Create new
        calendar). Default: "Study Blocks".
    read_calendar_allowlist: optional list[str]
        If provided, only include these named calendars when reading
        busy-time context. If None (default), read every non-write
        calendar the account has access to.
    scopes: list[str]
        OAuth scopes. Default is full read/write on the user's calendars.
        Do not change unless you know what you're doing — the scope list
        is baked into the refresh token, so narrowing it requires re-auth.
    """

    def __init__(
        self,
        *,
        credentials_path: str | Path,
        token_path: str | Path,
        write_calendar_name: str = "Study Blocks",
        read_calendar_allowlist: Optional[list[str]] = None,
        scopes: Optional[list[str]] = None,
    ):
        self.credentials_path = Path(credentials_path).expanduser()
        self.token_path = Path(token_path).expanduser()
        self.write_calendar_name = write_calendar_name
        self.read_allowlist = (
            {n.lower() for n in read_calendar_allowlist}
            if read_calendar_allowlist is not None else None
        )
        self.scopes = list(scopes) if scopes is not None else list(DEFAULT_SCOPES)
        self._service = None  # lazy; built on first use
        self._cal_id_cache: dict[str, str] = {}  # name.lower() -> calendarId

    # ---- CalendarProvider interface ----

    def list_events(
        self, start_utc: datetime, end_utc: datetime, *,
        include_write_calendar: bool = True,
        include_read_calendars: bool = True,
    ) -> list[CalendarEvent]:
        svc = self._svc()
        write_lower = self.write_calendar_name.lower()
        time_min = _rfc3339(_ensure_utc(start_utc))
        time_max = _rfc3339(_ensure_utc(end_utc))

        out: list[CalendarEvent] = []
        for cal in self._calendars():
            cal_name = cal.get("summary", "") or ""
            is_write = cal_name.lower() == write_lower
            if is_write and not include_write_calendar:
                continue
            if not is_write and not include_read_calendars:
                continue
            if (not is_write and self.read_allowlist is not None
                    and cal_name.lower() not in self.read_allowlist):
                continue

            page_token = None
            while True:
                try:
                    resp = svc.events().list(
                        calendarId=cal["id"],
                        timeMin=time_min,
                        timeMax=time_max,
                        singleEvents=True,   # expand recurrences
                        showDeleted=False,
                        pageToken=page_token,
                        maxResults=2500,
                    ).execute()
                except Exception:
                    # Skip calendars we can't read (e.g. broken shared cals).
                    break
                for ev in resp.get("items", []):
                    parsed = self._parse_event(ev, cal["id"], cal_name)
                    if parsed is not None:
                        out.append(parsed)
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
        return out

    def create_auto_event(
        self, *, start_utc: datetime, end_utc: datetime,
        title: str, notes: str = "",
        task_id: Optional[int] = None,
    ) -> CalendarEvent:
        cal_id = self._write_calendar_id()
        if cal_id is None:
            raise RuntimeError(
                f"Write calendar '{self.write_calendar_name}' not found on Google. "
                f"Create it at calendar.google.com first (+ next to \"Other "
                f"calendars\" → Create new calendar)."
            )
        private = {AUTO_KEY: "1"}
        if task_id is not None:
            private[TASK_ID_KEY] = str(task_id)
        body = {
            "summary": title,
            "description": notes,
            "start": {"dateTime": _rfc3339(_ensure_utc(start_utc))},
            "end":   {"dateTime": _rfc3339(_ensure_utc(end_utc))},
            "extendedProperties": {"private": private},
        }
        resp = self._svc().events().insert(calendarId=cal_id, body=body).execute()
        return CalendarEvent(
            id=resp["id"],
            source_handle=f"{cal_id}/{resp['id']}",
            title=title,
            start=_ensure_utc(start_utc),
            end=_ensure_utc(end_utc),
            notes=notes,
            is_auto=True,
            auto_task_id=task_id,
            calendar_name=self.write_calendar_name,
        )

    def delete_event(self, event: CalendarEvent) -> None:
        cal_id, event_id = self._resolve_delete_target(event)
        self._svc().events().delete(
            calendarId=cal_id, eventId=event_id,
        ).execute()

    def reset(self) -> None:
        self._service = None
        self._cal_id_cache.clear()

    def health_check(self) -> dict:
        info = {
            "provider": "GoogleCalendarProvider",
            "credentials_path": str(self.credentials_path),
            "token_present": self.token_path.exists(),
            "write_calendar": self.write_calendar_name,
            "read_allowlist": sorted(self.read_allowlist) if self.read_allowlist else "all",
        }
        if not self.credentials_path.exists():
            info["error"] = (
                f"credentials.json not found at {self.credentials_path}. "
                f"See docs/google-calendar-setup.md."
            )
            return info
        if not self.token_path.exists():
            info["error"] = (
                "No refresh token yet. Run `python setup.py` (or "
                "provider.authorize_interactive()) once to complete OAuth."
            )
            return info
        try:
            cals = [c.get("summary", "") for c in self._calendars()]
            info["write_calendar_exists"] = any(
                (n or "").lower() == self.write_calendar_name.lower() for n in cals
            )
            info["total_calendars_visible"] = len(cals)
        except Exception as ex:
            info["error"] = f"{type(ex).__name__}: {ex}"
        return info

    # ---- Interactive OAuth (called once by setup.py) ----

    def authorize_interactive(self, port: int = 8765) -> None:
        """Open the browser, run the OAuth consent flow, persist the token.

        Must be run on a machine with a browser — this is the only part of
        the lifecycle that isn't headless. Safe to call repeatedly; it just
        rewrites the token file. Uses Google's loopback redirect
        (`http://localhost:<port>/`), which is the canonical installed-app
        flow. No external hosting needed.
        """
        from google_auth_oauthlib.flow import InstalledAppFlow
        if not self.credentials_path.exists():
            raise FileNotFoundError(
                f"credentials.json not found at {self.credentials_path}. "
                f"Download it from Google Cloud Console → APIs & Services "
                f"→ Credentials → OAuth 2.0 Client IDs → Download JSON, "
                f"then save it to the path above."
            )
        flow = InstalledAppFlow.from_client_secrets_file(
            str(self.credentials_path), scopes=self.scopes,
        )
        creds = flow.run_local_server(port=port, open_browser=True)
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(creds.to_json())
        # Drop any cached service so the next call picks up the new token.
        self._service = None

    # ---- Internals ----

    def _svc(self):
        if self._service is not None:
            return self._service
        creds = self._load_credentials()
        from googleapiclient.discovery import build
        self._service = build("calendar", "v3", credentials=creds,
                              cache_discovery=False)
        return self._service

    def _load_credentials(self):
        if not self.token_path.exists():
            raise RuntimeError(
                f"No Google OAuth token at {self.token_path}. "
                f"Run `python setup.py` once to complete authorization, "
                f"or call provider.authorize_interactive() from Python."
            )
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        creds = Credentials.from_authorized_user_file(
            str(self.token_path), self.scopes,
        )
        # Silent refresh if we still have a refresh token.
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                self.token_path.write_text(creds.to_json())
            else:
                raise RuntimeError(
                    "Google token is invalid and can't be refreshed. "
                    "Delete the token file and re-run `python setup.py`."
                )
        return creds

    def _calendars(self) -> list[dict]:
        resp = self._svc().calendarList().list().execute()
        return resp.get("items", [])

    def _write_calendar_id(self) -> Optional[str]:
        target = self.write_calendar_name.lower()
        if target in self._cal_id_cache:
            return self._cal_id_cache[target]
        for cal in self._calendars():
            name = (cal.get("summary", "") or "").lower()
            if name == target:
                self._cal_id_cache[target] = cal["id"]
                return cal["id"]
        return None

    def _resolve_delete_target(self, event: CalendarEvent) -> tuple[str, str]:
        # source_handle we created ourselves is "<calId>/<eventId>".
        if event.source_handle and "/" in event.source_handle:
            cal_id, event_id = event.source_handle.rsplit("/", 1)
            return cal_id, event_id
        # Fallback — assume write calendar.
        cal_id = self._write_calendar_id()
        if cal_id is None:
            raise RuntimeError("Write calendar not found for delete")
        return cal_id, event.id

    def _parse_event(self, ev: dict, cal_id: str, cal_name: str) -> Optional[CalendarEvent]:
        # All-day events use "date" instead of "dateTime" — skip those, the
        # solver doesn't reason about them and they're usually multi-day
        # informational markers (holidays, birthdays).
        start_raw = ev.get("start", {})
        end_raw = ev.get("end", {})
        if "dateTime" not in start_raw or "dateTime" not in end_raw:
            return None
        try:
            start = _parse_rfc3339(start_raw["dateTime"])
            end = _parse_rfc3339(end_raw["dateTime"])
        except Exception:
            return None
        private = (ev.get("extendedProperties") or {}).get("private") or {}
        is_auto = private.get(AUTO_KEY) == "1"
        task_id: Optional[int] = None
        raw_tid = private.get(TASK_ID_KEY)
        if raw_tid is not None:
            try:
                task_id = int(raw_tid)
            except (TypeError, ValueError):
                task_id = None
        return CalendarEvent(
            id=ev["id"],
            source_handle=f"{cal_id}/{ev['id']}",
            title=ev.get("summary", "") or "",
            start=start,
            end=end,
            notes=ev.get("description", "") or "",
            is_auto=is_auto,
            auto_task_id=task_id,
            calendar_name=cal_name,
        )


# ---- Module-private helpers ----

def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _rfc3339(dt: datetime) -> str:
    # Google APIs want "2026-04-22T10:00:00Z" style.
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_rfc3339(s: str) -> datetime:
    # Google returns strings like "2026-04-22T10:00:00-04:00" or
    # "2026-04-22T10:00:00Z". fromisoformat handles the first directly;
    # "Z" needs a swap.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s).astimezone(timezone.utc)
