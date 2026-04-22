# Writing a new provider

The orchestrator, solver, tasks layer, and hub never touch a concrete backend.
Every integration flows through one of four abstract base classes in
`providers/`. To add support for a new backend — Google Calendar, Todoist,
Pushover, Brightspace, whatever — you implement one class and wire it in
`schedule_config.py`. Nothing else in the repo needs to change.

This doc walks through each ABC, points at the reference implementations,
and shows how the test suite mocks them.

---

## The four ABCs

| Category | ABC | Reference impl | Job |
|---|---|---|---|
| Calendar | `providers/base.py::CalendarProvider` | `icloud_caldav.ICloudCalDAVProvider` | Reads busy-time events; writes AUTO-tagged Study Blocks. |
| Task source | `providers/task_source.py::TaskSource` | `canvas_task_source.CanvasTaskSource` | Lists upstream work items (assignments, tickets). |
| Todo source | `providers/todo_source.py::TodoSource` | `apple_reminders.AppleRemindersTodoSource` | Returns recently-completed todos so their matching tasks can auto-close. |
| Notifier | `providers/notifier.py::Notifier` | `ntfy_notifier.NtfyNotifier`, `imessage_notifier.IMessageNotifier` | One-shot push of a text summary to the user. |

Each ABC is tiny by design — a handful of methods, one or two value types.
Look at the existing impls: they're the reference for style, error handling,
and docstring depth.

---

## CalendarProvider

**Five methods, one value type.** See `providers/base.py` for the full
docstring; the contract is:

```python
class CalendarProvider(ABC):
    def list_events(self, start_utc, end_utc, *,
                    include_write_calendar=True,
                    include_read_calendars=True) -> list[CalendarEvent]: ...

    def create_auto_event(self, *, start_utc, end_utc, title,
                          notes="", task_id=None) -> CalendarEvent: ...

    def delete_event(self, event: CalendarEvent) -> None: ...

    def reset(self) -> None: ...            # default no-op
    def health_check(self) -> dict: ...     # default returns {"provider": type_name}
```

`CalendarEvent` is the provider-neutral shape the solver sees:

```python
@dataclass
class CalendarEvent:
    id: str                     # UID / eventId / iCalUId
    source_handle: Optional[str]  # opaque; used by delete_event
    title: str
    start: datetime             # timezone-aware UTC
    end: datetime
    notes: str = ""
    is_auto: bool = False       # written by our solver
    auto_task_id: Optional[int] = None
    calendar_name: str = ""
    extra: dict = field(default_factory=dict)
```

### What "AUTO" means

The solver owns one write calendar. Every time it runs, it deletes every
AUTO-tagged event in a sliding window and replaces them with fresh chunks.
Non-AUTO events (anything the user placed manually) are left alone.

Your provider must serialize the AUTO flag somewhere the backend lets you
read back. Examples:
- CalDAV: `CATEGORIES:AUTO-SCHED` in the VEVENT.
- Google Calendar: `extendedProperties.private.auto_sched = "1"`.
- Outlook / Graph: `singleValueExtendedProperties` with a custom PropertyId.

Whatever you pick, `list_events` sets `is_auto=True` on the returned
`CalendarEvent` when it sees your marker. `create_auto_event` writes the
marker. Done.

### Reference: what iCloud CalDAV does

`providers/icloud_caldav.py` is 300 lines of carefully-earned knowledge —
worth skimming even if you're not touching CalDAV:

- Uses `caldav.DAVClient` for the read path but **raw** `requests.put` /
  `requests.delete` for writes (iCloud's ETag behavior breaks the library's
  If-Match logic — see `ROADBLOCKS.md §I3`).
- Replaces `cal.event_by_uid()` with `cal.search()` + Python-side UID
  matching (iCloud returns 412 on the REPORT query — `§I2`).
- Filters completion state client-side instead of via the server
  predicate — iCloud's doesn't work (`§I4`).
- `reset()` drops the DAVClient cache after a keepalive timeout (`§I5`).

For a fresh backend these quirks likely don't apply; you should start with
the simplest possible impl and add workarounds only when you observe
failures.

### Sketch: Google Calendar provider

```python
# providers/google_calendar.py
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from .base import CalendarEvent, CalendarProvider

_AUTO_PROP = "auto_sched"   # private extendedProperty key

class GoogleCalendarProvider(CalendarProvider):
    def __init__(self, *, credentials: Credentials, write_calendar_id: str,
                 read_calendar_ids: list[str] | None = None):
        self._service = build("calendar", "v3", credentials=credentials)
        self.write_id = write_calendar_id
        self.read_ids = read_calendar_ids or []

    def list_events(self, start_utc, end_utc, *,
                    include_write_calendar=True, include_read_calendars=True):
        cal_ids = []
        if include_write_calendar: cal_ids.append(self.write_id)
        if include_read_calendars: cal_ids.extend(self.read_ids)
        out = []
        for cid in cal_ids:
            items = self._service.events().list(
                calendarId=cid,
                timeMin=start_utc.isoformat(), timeMax=end_utc.isoformat(),
                singleEvents=True,
            ).execute().get("items", [])
            for ev in items:
                is_auto = (ev.get("extendedProperties", {})
                           .get("private", {})
                           .get(_AUTO_PROP) == "1")
                out.append(CalendarEvent(
                    id=ev["id"],
                    source_handle=f"{cid}/{ev['id']}",
                    title=ev.get("summary", ""),
                    start=_parse_google_ts(ev["start"]),
                    end=_parse_google_ts(ev["end"]),
                    notes=ev.get("description", ""),
                    is_auto=is_auto,
                    calendar_name=cid,
                    extra={"raw": ev},
                ))
        return out

    def create_auto_event(self, *, start_utc, end_utc, title, notes="",
                          task_id=None):
        body = {
            "summary": title, "description": notes,
            "start": {"dateTime": start_utc.isoformat()},
            "end":   {"dateTime": end_utc.isoformat()},
            "extendedProperties": {"private": {
                _AUTO_PROP: "1",
                "auto_task_id": str(task_id) if task_id else "",
            }},
        }
        ev = self._service.events().insert(
            calendarId=self.write_id, body=body,
        ).execute()
        return CalendarEvent(
            id=ev["id"], source_handle=f"{self.write_id}/{ev['id']}",
            title=title, start=start_utc, end=end_utc, notes=notes,
            is_auto=True, auto_task_id=task_id, calendar_name=self.write_id,
        )

    def delete_event(self, event):
        cid, eid = event.source_handle.split("/", 1)
        self._service.events().delete(calendarId=cid, eventId=eid).execute()

    def health_check(self):
        return {"provider": "google_calendar", "write_id": self.write_id,
                "read_count": len(self.read_ids)}
```

Wire it in `schedule_config.py`:

```python
CALENDAR: CalendarProvider = GoogleCalendarProvider(
    credentials=_load_google_creds(),
    write_calendar_id=os.environ["GOOGLE_WRITE_CAL_ID"],
)
```

---

## TaskSource

```python
class TaskSource(ABC):
    SOURCE: str = ""              # "canvas", "notion", "github", ...
    require_approval: bool = False  # route new tasks to pending_review?

    @abstractmethod
    def list_active_tasks(self) -> list[SourceTask]: ...

    def health_check(self) -> dict: ...
```

`SourceTask` is normalized to what the orchestrator upserts:

```python
@dataclass
class SourceTask:
    source: str            # same as TaskSource.SOURCE
    source_id: str         # stable id within that provider
    title: str
    course: Optional[str]          # grouping label
    deadline_utc: Optional[datetime]
    is_completed: bool             # authoritative — True closes the local task
    duration_hint_min: Optional[int]
    priority_hint: Optional[str]   # 'asap'|'high'|'medium'|'low' or None
    notes: Optional[str]
    extra: dict
```

**Upsert key.** `(source, source_id)` identifies a task uniquely across syncs.
If you wire two instances of the same source (e.g. two Canvas domains), make
sure the `source_id`s are domain-prefixed so they're globally unique.

**Authority rule.** `is_completed=True` always closes the local task. On
`is_completed=False`, `upsert_from_source` reopens a locally-done task
(ROADBLOCKS §D2, "Canvas is authoritative"). This matters when a reminder
match false-positively closed a task that's actually still open upstream.

**Relevance filter.** `list_active_tasks` should only return items relevant
to the solver's near-term window (see `CanvasTaskSource._is_relevant`: -3d
to +21d by default). Returning everything forever wastes bandwidth and
context — trim at the provider boundary.

### Sketch: Todoist task source

```python
# providers/todoist_task_source.py
import requests
from .task_source import SourceTask, TaskSource

class TodoistTaskSource(TaskSource):
    SOURCE = "todoist"

    def __init__(self, *, token: str, project_filter: list[str] | None = None,
                 require_approval: bool = False):
        self.token = token
        self.projects = project_filter
        self.require_approval = require_approval

    def list_active_tasks(self):
        r = requests.get(
            "https://api.todoist.com/rest/v2/tasks",
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=30,
        )
        r.raise_for_status()
        out = []
        for t in r.json():
            if self.projects and t.get("project_id") not in self.projects:
                continue
            out.append(SourceTask(
                source=self.SOURCE, source_id=str(t["id"]),
                title=t["content"],
                course=t.get("labels", [None])[0],
                deadline_utc=_parse_iso(t.get("due", {}).get("datetime")),
                is_completed=t.get("is_completed", False),
                notes=t.get("description"),
            ))
        return out

    def health_check(self):
        return {"source": self.SOURCE,
                "require_approval": self.require_approval,
                "projects": self.projects or "all"}
```

---

## TodoSource

The todo cross-reference is how "I checked this off in my todo app" auto-closes
the matching task. It's optional — `TODO_SOURCE = None` disables it.

```python
class TodoSource(ABC):
    SOURCE: str = ""

    @abstractmethod
    def list_items(self, *, include_completed: bool = True) -> list[TodoItem]: ...

    def health_check(self) -> dict: ...
```

The orchestrator matches completed `TodoItem`s against active tasks by:
1. Course match (via the `TODO_LIST_TO_COURSE` mapping in `schedule_config.py`,
   or substring fallback against the task's `course` field).
2. A shared non-digit token AND a shared digit (if both titles have digits).
   See `ROADBLOCKS.md §R4` — this digit-match guard is what stops "Chapter 14"
   from closing "Chapter 15".

After a match fires, a row lands in the `reminder_matches` handshake table so
the same `(todo_id, task_id)` pair can't re-close the same task after
upstream reopens it.

---

## Notifier

Simplest ABC:

```python
class Notifier(ABC):
    CHANNEL: str = ""

    @abstractmethod
    def send(self, body: str, *, title: Optional[str] = None,
             priority: str = "normal") -> dict: ...

    def health_check(self) -> dict: ...
```

`send()` returns `{"ok": True, ...}` or `{"ok": False, "error": "..."}`.
**Never raises** — a notifier failure shouldn't take down the session.

`priority` values: `"low"`, `"normal"`, `"high"`. Translate to whatever your
channel accepts; unknown values should fall back to `"normal"`.

### Writing a new Notifier

Look at `providers/ntfy_notifier.py` — it's 87 lines and covers the whole
shape: init, send, error handling, health_check with topic redaction. A
Pushover / email / Slack notifier is the same structure with a different
POST.

---

## Testing your provider

Every provider test in `tests/` uses the same pattern: monkeypatch
`requests.get` (or `requests.post`) on the provider module, return canned
JSON, assert the provider normalized it correctly.

```python
# tests/test_my_provider.py
from types import SimpleNamespace

from providers import my_provider as mod

def _fake_response(body):
    return SimpleNamespace(status_code=200,
                           json=lambda: body,
                           raise_for_status=lambda: None)

def test_happy_path(monkeypatch):
    def router(url, headers=None, params=None, timeout=None):
        return _fake_response([{"id": 1, "name": "HW", "due_at": "..."}])
    monkeypatch.setattr(mod.requests, "get", router)

    provider = mod.MyTaskSource(base_url="https://x", token="t")
    out = provider.list_active_tasks()

    assert len(out) == 1
    assert out[0].title == "HW"
```

Reference: `tests/test_canvas_provider.py`, `tests/test_ntfy_provider.py`.
Both run on Python 3.9 and 3.12 in CI.

For providers that wrap a non-`requests` library (e.g. `caldav.DAVClient`),
the pattern is the same but you monkeypatch the library's top-level
factory. See `providers/icloud_caldav.py` for where the library boundary
is; the corresponding test file is still pending — contributions welcome.

---

## Wiring

Once your class exists, the only other file that changes is
`schedule_config.py`:

```python
from providers.my_provider import MyTaskSource

TASK_SOURCES: list[TaskSource] = [
    CanvasTaskSource(...),
    MyTaskSource(token=os.environ["MY_TOKEN"], require_approval=True),
]
```

The orchestrator loops over `TASK_SOURCES` — multiple sources work out of
the box. The hub and solver never know there's a new backend.

Add the env var to `.env.example` and to `install.py` if you want it in the
interactive wizard.
