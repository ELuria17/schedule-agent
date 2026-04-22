"""Microsoft Outlook / Microsoft 365 implementation of CalendarProvider.

Talks to Microsoft Graph v1.0 over plain HTTPS (via the graph_*
helpers in microsoft_graph_auth). Auth is MSAL public-client flow;
each user creates their own Azure AD app registration, drops the
client_id into their .env, and runs `python authorize_microsoft.py`
once to produce a persisted token cache. After that the provider
runs headless with silent refresh.

AUTO marker serialization on Outlook
------------------------------------
Outlook events don't have a Google-style `extendedProperties.private`
dict. The equivalent is `singleValueExtendedProperties` — a MAPI-
style property bag that round-trips through Exchange. We use:

    property set GUID   : 00020329-0000-0000-C000-000000000046
                          (the public MAPI namespace Microsoft
                          documents for extended properties on
                          calendar items; safe to reuse)
    name "AutoPlanAuto"       — value "1" means solver-placed
    name "AutoPlanTaskId"     — optional local solver task id

On read we pull `singleValueExtendedProperties` (via `$expand`)
and scan its entries for these names. On write we include both
entries in the event body.

Write vs read calendars
-----------------------
Exactly one calendar on the account is the "write calendar" (the
bucket where solver-placed chunks land). Everything else is a
read-only busy-time source. The user creates the write calendar
(e.g. "Study Blocks") in outlook.office.com / Outlook on the web
first; we never auto-create, because the authorize consent doesn't
guarantee the user wants us making new calendars on their account.

All-day events are skipped for the same reason as Google: they're
almost always multi-day informational markers (holidays, birthdays,
OoO banners) the solver would mistakenly treat as all-day busy
time.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import microsoft_graph_auth as mg
from .base import CalendarEvent, CalendarProvider


# ---- Extended-property identifiers ----

# Public MAPI namespace. Any stable GUID works; this one is
# documented by Microsoft as the generic public extended-property
# namespace and is the convention Outlook add-in samples use.
EXT_PROP_GUID = "00020329-0000-0000-C000-000000000046"
AUTO_PROP_NAME = "AutoPlanAuto"
TASK_ID_PROP_NAME = "AutoPlanTaskId"

_AUTO_PROP_ID = f"String {{{EXT_PROP_GUID}}} Name {AUTO_PROP_NAME}"
_TASK_ID_PROP_ID = f"String {{{EXT_PROP_GUID}}} Name {TASK_ID_PROP_NAME}"

# Fields we ask Graph for on calendarView. Narrow selects keep
# payloads small and (importantly) force `singleValueExtendedProperties`
# to actually come back on the wire.
_EVENT_SELECT = (
    "id,subject,body,start,end,isAllDay"
)
# `$expand` syntax for pulling just our two named properties.
_EXT_EXPAND_FILTER = (
    f"singleValueExtendedProperties($filter="
    f"id eq '{_AUTO_PROP_ID}' or id eq '{_TASK_ID_PROP_ID}')"
)

# Access-token memoization — MSAL itself caches, but decoding the
# serialized cache off disk on every call still costs a few ms and
# a file read. Cache the token for a tight window, then re-ask MSAL.
_TOKEN_TTL_SEC = 50 * 60  # 50 minutes


class OutlookCalendarProvider(CalendarProvider):
    """Outlook / Microsoft 365 calendar provider.

    Parameters
    ----------
    client_id : str
        Application (client) ID from your Azure AD app registration
        (portal.azure.com → App registrations → New registration →
        "Accounts in any organizational directory and personal
        Microsoft accounts" → Register → Overview → "Application
        (client) ID"). Must be a public client; enable Mobile +
        Desktop redirect URI `http://localhost` in Authentication.
    tenant : str, default "common"
        Azure tenant to authenticate against. "common" accepts both
        personal and work/school accounts; pass a tenant GUID when
        scoping to a single Azure AD tenant.
    token_path : Path, optional
        File where the MSAL token cache is persisted. Defaults to
        `paths.microsoft_token_path()` on first access.
    write_calendar_name : str, default "Study Blocks"
        Name of the Outlook calendar the solver writes AUTO events
        to. The user must have created this calendar in Outlook on
        the web first; we do not auto-create it (a typo would
        quietly litter the account).
    read_calendar_allowlist : list[str], optional
        If provided, only include these named calendars when reading
        busy-time context. If None (default), every non-write
        calendar the account has access to is read.
    """

    def __init__(
        self,
        *,
        client_id: str,
        tenant: str = "common",
        token_path: Optional[Path] = None,
        write_calendar_name: str = "Study Blocks",
        read_calendar_allowlist: Optional[list[str]] = None,
    ):
        self.client_id = client_id
        self.tenant = tenant
        # Resolve the default here rather than as a default arg so
        # changes to paths.data_dir() (e.g. SCHEDULE_AGENT_DATA_DIR
        # being set after import) are honored.
        if token_path is None:
            from paths import microsoft_token_path  # local import to dodge cycles
            token_path = microsoft_token_path()
        self.token_path = Path(token_path).expanduser()
        self.write_calendar_name = write_calendar_name
        self.read_allowlist = (
            {n.lower() for n in read_calendar_allowlist}
            if read_calendar_allowlist is not None else None
        )
        self._cal_id_cache: dict[str, str] = {}  # name.lower() -> calendar id
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0

    # ---- CalendarProvider interface ----

    def list_events(
        self, start_utc: datetime, end_utc: datetime, *,
        include_write_calendar: bool = True,
        include_read_calendars: bool = True,
    ) -> list[CalendarEvent]:
        token = self._access_token()
        write_lower = self.write_calendar_name.lower()
        start_str = _graph_iso(_ensure_utc(start_utc))
        end_str = _graph_iso(_ensure_utc(end_utc))

        out: list[CalendarEvent] = []
        for cal in self._calendars(token):
            cal_name = cal.get("name", "") or ""
            is_write = cal_name.lower() == write_lower
            if is_write and not include_write_calendar:
                continue
            if not is_write and not include_read_calendars:
                continue
            if (not is_write and self.read_allowlist is not None
                    and cal_name.lower() not in self.read_allowlist):
                continue

            path = f"/me/calendars/{cal['id']}/calendarView"
            params = {
                "startDateTime": start_str,
                "endDateTime": end_str,
                "$top": "500",
                "$select": _EVENT_SELECT,
                "$expand": _EXT_EXPAND_FILTER,
            }
            try:
                resp = mg.graph_get(token, path, params=params)
            except Exception:
                # Skip calendars we can't read (shared cals with stale
                # perms, etc.). Same survival-mode behavior as Google.
                continue
            for ev in resp.get("value", []) or []:
                parsed = self._parse_event(ev, cal["id"], cal_name)
                if parsed is not None:
                    out.append(parsed)
            # (Graph paginates with `@odata.nextLink`; we set $top=500
            # and the solver never looks further than a few weeks, so
            # one page is enough in practice. If a user ever crosses
            # that threshold, $top=1000 is the cap — extend then.)
        return out

    def create_auto_event(
        self, *, start_utc: datetime, end_utc: datetime,
        title: str, notes: str = "",
        task_id: Optional[int] = None,
    ) -> CalendarEvent:
        token = self._access_token()
        cal_id = self._write_calendar_id(token)
        if cal_id is None:
            raise RuntimeError(
                f"Write calendar '{self.write_calendar_name}' not found on "
                f"Outlook. Create it at outlook.office.com first "
                f"(My calendars → Add calendar → Create blank calendar)."
            )
        ext_props = [{"id": _AUTO_PROP_ID, "value": "1"}]
        if task_id is not None:
            ext_props.append({"id": _TASK_ID_PROP_ID, "value": str(task_id)})
        body = {
            "subject": title,
            "body": {"contentType": "text", "content": notes},
            "start": {"dateTime": _graph_iso(_ensure_utc(start_utc)), "timeZone": "UTC"},
            "end":   {"dateTime": _graph_iso(_ensure_utc(end_utc)),   "timeZone": "UTC"},
            "singleValueExtendedProperties": ext_props,
        }
        resp = mg.graph_post(token, f"/me/calendars/{cal_id}/events", body)
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
        if not event.source_handle or "/" not in event.source_handle:
            # Fail fast — no fallback search, per spec.
            raise RuntimeError(
                "Cannot delete Outlook event: source_handle must be "
                "'<calendar_id>/<event_id>'. Got "
                f"{event.source_handle!r}."
            )
        cal_id, ev_id = event.source_handle.rsplit("/", 1)
        token = self._access_token()
        mg.graph_delete(token, f"/me/calendars/{cal_id}/events/{ev_id}")

    def reset(self) -> None:
        self._token = None
        self._token_expires_at = 0.0
        self._cal_id_cache.clear()

    def health_check(self) -> dict:
        status = mg.token_status(token_path=self.token_path)
        info = {
            "provider": "OutlookCalendarProvider",
            "client_id": self.client_id,  # not a secret for public clients
            "tenant": self.tenant,
            "token_present": status["token_present"],
            "accounts": status["accounts"],
            "write_calendar": self.write_calendar_name,
            "read_allowlist": sorted(self.read_allowlist) if self.read_allowlist else "all",
        }
        if not status["token_present"]:
            info["error"] = (
                "No Microsoft token yet. Run "
                "`python authorize_microsoft.py` once to complete sign-in."
            )
            return info
        try:
            token = self._access_token()
            names = [c.get("name", "") for c in self._calendars(token)]
            info["write_calendar_exists"] = any(
                (n or "").lower() == self.write_calendar_name.lower() for n in names
            )
            info["total_calendars_visible"] = len(names)
        except Exception as ex:
            info["error"] = f"{type(ex).__name__}: {ex}"
        return info

    # ---- Internals ----

    def _access_token(self) -> str:
        now = time.monotonic()
        if self._token is not None and now < self._token_expires_at:
            return self._token
        token = mg.get_access_token(
            client_id=self.client_id,
            tenant=self.tenant,
            token_path=self.token_path,
        )
        self._token = token
        self._token_expires_at = now + _TOKEN_TTL_SEC
        return token

    def _calendars(self, token: str) -> list[dict]:
        resp = mg.graph_get(token, "/me/calendars", params={"$select": "id,name"})
        return resp.get("value", []) or []

    def _write_calendar_id(self, token: str) -> Optional[str]:
        target = self.write_calendar_name.lower()
        if target in self._cal_id_cache:
            return self._cal_id_cache[target]
        for cal in self._calendars(token):
            name = (cal.get("name", "") or "").lower()
            if name == target:
                self._cal_id_cache[target] = cal["id"]
                return cal["id"]
        return None

    def _parse_event(self, ev: dict, cal_id: str, cal_name: str) -> Optional[CalendarEvent]:
        # Skip all-day — solver doesn't reason about them and Graph
        # sends them with `isAllDay: true` + midnight-UTC start/end.
        if ev.get("isAllDay"):
            return None
        start_raw = ev.get("start") or {}
        end_raw = ev.get("end") or {}
        start_dt_str = start_raw.get("dateTime")
        end_dt_str = end_raw.get("dateTime")
        if not start_dt_str or not end_dt_str:
            return None
        try:
            start = _parse_graph_datetime(start_dt_str, start_raw.get("timeZone") or "UTC")
            end = _parse_graph_datetime(end_dt_str, end_raw.get("timeZone") or "UTC")
        except Exception:
            return None

        is_auto = False
        task_id: Optional[int] = None
        for p in ev.get("singleValueExtendedProperties") or []:
            pid = p.get("id", "") or ""
            val = p.get("value")
            if AUTO_PROP_NAME in pid and val == "1":
                is_auto = True
            elif TASK_ID_PROP_NAME in pid and val is not None:
                try:
                    task_id = int(val)
                except (TypeError, ValueError):
                    task_id = None

        notes = ""
        body = ev.get("body") or {}
        if body.get("contentType") == "text":
            notes = body.get("content") or ""
        elif body.get("contentType") == "html":
            # The solver never looks at notes beyond logging; dropping
            # HTML rather than parsing it is fine and avoids a lxml
            # dep for a field almost nothing uses.
            notes = ""

        return CalendarEvent(
            id=ev["id"],
            source_handle=f"{cal_id}/{ev['id']}",
            title=ev.get("subject", "") or "",
            start=start,
            end=end,
            notes=notes,
            is_auto=is_auto,
            auto_task_id=task_id,
            calendar_name=cal_name,
        )


# ---- Module-private helpers ----

def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _graph_iso(dt: datetime) -> str:
    """Graph wants ISO-8601 without the trailing `Z` — the `timeZone`
    field carries the zone separately. Microseconds are dropped for
    readability; Graph accepts them too."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _parse_graph_datetime(dt_str: str, tz_name: str) -> datetime:
    """Graph returns times like '2026-04-22T14:00:00.0000000' plus a
    separate timeZone field. In practice every `calendarView` call we
    make sets the zone to UTC (Graph converts on the server), so we
    primarily handle that. If a non-UTC zone ever does come back we
    fall through to fromisoformat + assume UTC — good enough since
    the orchestrator only consumes UTC datetimes anyway."""
    # Strip trailing fractional second chunks Graph sometimes adds;
    # fromisoformat on 3.9 barfs on too-many-digits.
    cleaned = dt_str
    if "." in cleaned:
        head, frac = cleaned.split(".", 1)
        frac = frac[:6]  # microsecond precision max
        cleaned = f"{head}.{frac}"
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        # Fall back to a best-effort parse: YYYY-MM-DDTHH:MM:SS
        dt = datetime.strptime(cleaned[:19], "%Y-%m-%dT%H:%M:%S")
    if dt.tzinfo is None:
        # Graph said `tz_name`; we only trust UTC here and treat
        # anything else as already-UTC (safer than hauling in zoneinfo
        # for edge cases that don't occur in our requests).
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
