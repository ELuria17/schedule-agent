"""
User configuration file. This is the one file an end user edits when
swapping backends. The orchestrator imports CALENDAR, TASK_SOURCES,
TODO_SOURCE, NOTIFIER from here by name.

=======================================================================
PICK YOUR BACKENDS
=======================================================================
Each provider category has one ABC (in `providers/`) and zero-or-more
concrete implementations. Uncomment the constructor you want, fill in
the env vars, and the orchestrator never needs to change.

Environment variables referenced below live in `.env` (see .env.example).
Any required values missing from the environment will raise KeyError at
import time — fail-fast, by design.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

# Base ABCs (used for type hints only — never instantiated directly).
from providers.base import CalendarProvider
from providers.task_source import TaskSource
from providers.todo_source import TodoSource
from providers.notifier import Notifier

# ----------------------------- Concrete impls -----------------------------
# Import only the ones you plan to use. Unused imports are harmless but noisy.

from providers.icloud_caldav import ICloudCalDAVProvider
from providers.canvas_task_source import CanvasTaskSource
from providers.apple_reminders import AppleRemindersTodoSource
from providers.imessage_notifier import IMessageNotifier
from providers.ntfy_notifier import NtfyNotifier


_PROJECT_DIR = Path(__file__).resolve().parent


# =======================================================================
# TIMEZONE  — your home timezone. Any IANA name works.
# =======================================================================
# Drives solver local-time math and the agent's summary framing.
# Can also be overridden at runtime via the TIMEZONE env var.

TIMEZONE: str = os.environ.get("TIMEZONE", "America/New_York")


# =======================================================================
# WORKING HOURS  — when the solver is allowed to place chunks, in local
# time. Mon=0, Sun=6 (Python weekday()).
# =======================================================================

WORKING_HOURS: dict[int, tuple[str, str]] = {
    0: ("09:00", "22:00"),
    1: ("09:00", "22:00"),
    2: ("09:00", "22:00"),
    3: ("09:00", "22:00"),
    4: ("09:00", "22:00"),
    5: ("10:00", "22:00"),
    6: ("10:00", "22:00"),
}


# =======================================================================
# CLASS / FIXED BLOCKS  — recurring commitments the solver must not
# schedule over (classes, standing meetings, sleep, religious observance).
# =======================================================================
# Each entry: (kind, label, rrule, start_local, end_local) with kind in
# ('class','sleep','shabbat','gym_typical','custom'). RRULE BYDAY uses
# iCal two-letter day codes (MO,TU,WE,TH,FR,SA,SU).
#
# Seeded into config_blocks on first boot only — after that you can also
# edit via the hub UI. New installs default to empty (no classes).

CLASS_BLOCKS: list[tuple[str, str, str, str, str]] = [
    # Example:
    # ("class", "CS 101", "FREQ=WEEKLY;BYDAY=MO,WE,FR", "09:00", "09:50"),
]


# =======================================================================
# TODO → TASK-SOURCE MAPPING  — bridges the TodoSource list name with
# the TaskSource `course` field so "checked off in Todoist" can close
# "Canvas assignment for ECON 104".
# =======================================================================
# Keys are lowercased todo-list names; values are course codes as they
# appear on TaskSource items. Leave empty if you don't use a TodoSource,
# or if your todo-list names already equal the task course codes.

TODO_LIST_TO_COURSE: dict[str, str] = {
    # Example:
    # "macroeconomics": "ECON 104",
}


# =======================================================================
# CALENDAR PROVIDER  — where the solver writes Study Blocks.
# =======================================================================
# Today: iCloud CalDAV. Alternatives (planned): Google Calendar,
# Microsoft Graph (Outlook), generic CalDAV server.

CALENDAR: CalendarProvider = ICloudCalDAVProvider(
    username=os.environ["ICLOUD_USER"],
    app_password=os.environ["ICLOUD_APP_PASSWORD"],
    write_calendar_name=os.environ.get("WRITE_CALENDAR_NAME", "Study Blocks"),
    # Uncomment + populate to restrict which calendars the solver treats
    # as busy-time context. By default every non-write calendar counts.
    # read_calendar_allowlist=["Eytan", "School", "Family"],
)


# =======================================================================
# TASK SOURCES  — where upstream work items (assignments, tickets, etc.)
# come from. This is a list; you can configure multiple.
# =======================================================================
# Today: Canvas LMS. Alternatives (planned): Brightspace, Moodle, Notion,
# Linear, GitHub Issues, a CSV file, "manual only".

TASK_SOURCES: list[TaskSource] = [
    CanvasTaskSource(
        base_url=os.environ.get("CANVAS_BASE_URL",
                                "https://psu.instructure.com/api/v1"),
        token=os.environ["CANVAS_TOKEN"],
        # Set to True if you want every new Canvas assignment to land in the
        # pending-review queue (visible in the hub) before it's eligible for
        # scheduling. Default False = trust Canvas, schedule immediately.
        require_approval=False,
    ),
]


# =======================================================================
# TODO SOURCE  — optional "user checked something off in their to-do app"
# signal used to auto-close matching tasks.
# =======================================================================
# Today: Apple Reminders (macOS only). Alternatives (planned): Todoist,
# Google Tasks, TickTick, MS To Do. Set to None to disable.

from paths import reminders_fetch_binary_path

TODO_SOURCE: Optional[TodoSource] = AppleRemindersTodoSource(
    binary_path=str(reminders_fetch_binary_path()),
)


# =======================================================================
# NOTIFIER  — how the agent reaches you with the daily summary / updates.
# =======================================================================
# Swap one of the options below. They all implement the same `Notifier`
# interface — the agent and orchestrator don't care which you pick.
#
# Option 1: iMessage (macOS only; Mac must be signed in to Messages.app).
#           Self-send does NOT push-notify (ROADBLOCKS §M2); shows as
#           badge on Messages.app.
#
# Option 2: ntfy.sh (cross-platform, free, no account).
#           Install the ntfy app on phone, subscribe to the same topic.
#           Actual push notifications on any platform.

NOTIFIER: Notifier = IMessageNotifier(
    script_path=str(_PROJECT_DIR / "macos" / "send_imessage.applescript"),
    recipient=os.environ["USER_PHONE"],
)

# To switch to ntfy.sh: comment the block above and uncomment the one
# below. Set NTFY_TOPIC in `.env` to a hard-to-guess string — anyone
# with that topic can publish to and read your notifications.
#
# NOTIFIER: Notifier = NtfyNotifier(
#     topic=os.environ["NTFY_TOPIC"],
#     default_tags=["calendar"],
# )


# =======================================================================
# Convenience: health check across all configured providers.
# =======================================================================

def providers_health() -> dict:
    """Snapshot of every provider's self-reported status. Used by the hub
    to show which backends are wired up and whether they're reachable."""
    return {
        "calendar": CALENDAR.health_check(),
        "task_sources": [s.health_check() for s in TASK_SOURCES],
        "todo_source": TODO_SOURCE.health_check() if TODO_SOURCE else None,
        "notifier": NOTIFIER.health_check(),
        "timezone": TIMEZONE,
    }
