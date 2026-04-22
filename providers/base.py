"""Provider-neutral calendar interface.

A CalendarProvider is what the solver and orchestrator see. Implementations
translate to a concrete backend (iCloud CalDAV, Google Calendar, Microsoft
Graph, etc.) — and all backend-specific quirks stay inside the implementation.

Design goals:
- Small surface. Five methods, one value type. Easy to implement.
- No leaked backend details (no VEVENT, no ICal strings, no CATEGORIES).
- AUTO-tagging is modeled as a boolean + optional task id on the event; each
  provider chooses how to serialize that (CalDAV CATEGORIES, Google
  extendedProperties, Outlook singleValueExtendedProperties, etc.).
- Read/write split: providers have one "write calendar" (where solver-placed
  chunks go) and zero-or-more "read calendars" (busy-time context).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class CalendarEvent:
    """Provider-neutral event.

    `id` is stable for the lifetime of the event within the provider (UID for
    CalDAV, eventId for Google, iCalUId or id for Outlook). `source_handle` is
    an opaque provider-specific string the provider uses to locate the event
    for delete — e.g. a CalDAV absolute URL, a Google "calendarId/eventId",
    an Outlook "user/events/{id}". Callers should NEVER parse `source_handle`.
    """
    id: str
    source_handle: Optional[str]
    title: str
    start: datetime                     # timezone-aware UTC
    end: datetime                       # timezone-aware UTC
    notes: str = ""
    is_auto: bool = False               # placed by our solver
    auto_task_id: Optional[int] = None  # solver task id when is_auto=True
    calendar_name: str = ""             # which source calendar; informational
    extra: dict = field(default_factory=dict)  # provider-specific, optional

    @property
    def duration_min(self) -> int:
        return int((self.end - self.start).total_seconds() / 60)


class CalendarProvider(ABC):
    """Five-method contract. A minimal provider is just this.

    Thread-safety: implementations must be safe for concurrent read calls.
    Writes are serialized by the orchestrator's solver lock, so providers
    don't need write-side locking.
    """

    # ---- Read ----

    @abstractmethod
    def list_events(
        self,
        start_utc: datetime,
        end_utc: datetime,
        *,
        include_write_calendar: bool = True,
        include_read_calendars: bool = True,
    ) -> list[CalendarEvent]:
        """All events overlapping [start_utc, end_utc).

        include_write_calendar: include events from the solver's write calendar.
        include_read_calendars: include events from any other configured
            "busy-time" calendars.

        Callers filter by `is_auto` on the returned objects as needed.
        """

    # ---- Write ----

    @abstractmethod
    def create_auto_event(
        self,
        *,
        start_utc: datetime,
        end_utc: datetime,
        title: str,
        notes: str = "",
        task_id: Optional[int] = None,
    ) -> CalendarEvent:
        """Create a new event on the write calendar with the AUTO marker.

        The returned CalendarEvent must have is_auto=True and carry whatever
        source_handle the provider needs to delete it later.
        """

    @abstractmethod
    def delete_event(self, event: CalendarEvent) -> None:
        """Delete an event. May raise on provider/network failure."""

    # ---- Lifecycle ----

    def reset(self) -> None:
        """Drop cached connections. Called after a suspected transient failure
        (e.g. keepalive timeout). Default is a no-op."""
        return

    def health_check(self) -> dict:
        """Return a small dict describing configuration/state. Used by the hub
        to show which providers are wired up. Default is name + class."""
        return {"provider": type(self).__name__}
