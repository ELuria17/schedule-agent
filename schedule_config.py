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
import sys
from pathlib import Path
from typing import Optional

_IS_MACOS = sys.platform == "darwin"

# Base ABCs (used for type hints only — never instantiated directly).
from providers.base import CalendarProvider
from providers.task_source import TaskSource
from providers.todo_source import TodoSource
from providers.notifier import Notifier

# ----------------------------- Concrete impls -----------------------------
# Import only the ones you plan to use. Unused imports are harmless but noisy.

from providers.icloud_caldav import ICloudCalDAVProvider, GenericCalDAVProvider
from providers.google_calendar import GoogleCalendarProvider
from providers.outlook_calendar import OutlookCalendarProvider
from providers.canvas_task_source import CanvasTaskSource
from providers.todoist_task_source import TodoistTaskSource
from providers.gmail_scanner import GmailScanner
from providers.google_tasks import GoogleTasksProvider
from providers.outlook_mail_scanner import OutlookMailScanner
from providers.microsoft_todo import MicrosoftTodoProvider
from providers.apple_reminders import AppleRemindersTodoSource
from providers.imessage_notifier import IMessageNotifier
from providers.ntfy_notifier import NtfyNotifier
from providers.pushover_notifier import PushoverNotifier
from providers.email_notifier import EmailNotifier
from providers.slack_notifier import SlackNotifier


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
# iCloud is the default. For any other CalDAV-speaking server (Fastmail,
# Posteo, Mailbox.org, Nextcloud, self-hosted Radicale), swap the
# constructor for GenericCalDAVProvider below and set CALDAV_URL.
# Google Calendar and Microsoft Outlook need OAuth2 and are next on the
# roadmap — see docs/providers.md.

if os.environ.get("GOOGLE_CALENDAR_CREDENTIALS"):
    from paths import google_token_path
    CALENDAR: CalendarProvider = GoogleCalendarProvider(
        credentials_path=os.environ["GOOGLE_CALENDAR_CREDENTIALS"],
        token_path=str(google_token_path()),
        write_calendar_name=os.environ.get("WRITE_CALENDAR_NAME", "Study Blocks"),
        # read_calendar_allowlist=["Personal", "School", "Family"],
    )
elif (os.environ.get("MICROSOFT_CLIENT_ID")
      and os.environ.get("ENABLE_OUTLOOK_CALENDAR", "false").lower() in ("1", "true", "yes")):
    CALENDAR: CalendarProvider = OutlookCalendarProvider(
        client_id=os.environ["MICROSOFT_CLIENT_ID"],
        tenant=os.environ.get("MICROSOFT_TENANT", "common"),
        write_calendar_name=os.environ.get("WRITE_CALENDAR_NAME", "Study Blocks"),
        # read_calendar_allowlist=["Work", "Personal"],
    )
elif os.environ.get("CALDAV_URL"):
    CALENDAR: CalendarProvider = GenericCalDAVProvider(
        url=os.environ["CALDAV_URL"],
        username=os.environ["CALDAV_USER"],
        app_password=os.environ["CALDAV_PASSWORD"],
        write_calendar_name=os.environ.get("WRITE_CALENDAR_NAME", "Study Blocks"),
    )
else:
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

TASK_SOURCES: list[TaskSource] = []

# Default approval policy for every source. False = new tasks flow
# straight into scheduling. True = new tasks land in the hub's
# pending-review queue and the user has to approve them first.
# Per-source overrides are allowed below.
_DEFAULT_REQUIRE_APPROVAL = (
    os.environ.get("DEFAULT_REQUIRE_APPROVAL", "false").lower()
    in ("1", "true", "yes")
)

# --- Canvas LMS --- (students).
if os.environ.get("CANVAS_TOKEN"):
    TASK_SOURCES.append(CanvasTaskSource(
        base_url=os.environ.get("CANVAS_BASE_URL",
                                "https://psu.instructure.com/api/v1"),
        token=os.environ["CANVAS_TOKEN"],
        require_approval=_DEFAULT_REQUIRE_APPROVAL,
    ))

# --- Todoist --- (any platform).
# Generate a token at Settings → Integrations → Developer in the Todoist
# web app, then set TODOIST_TOKEN in .env.
if os.environ.get("TODOIST_TOKEN"):
    TASK_SOURCES.append(TodoistTaskSource(
        token=os.environ["TODOIST_TOKEN"],
        # Optional: only pull from specific Todoist projects (IDs are
        # strings). Leave as None / unset to pull from all projects.
        # project_ids=["2337281111"],
        # Optional: only pull tasks tagged with one of these labels.
        # label_filter=["focus"],
        default_duration_min=45,
        require_approval=_DEFAULT_REQUIRE_APPROVAL,
    ))


# --- Gmail inbox scanner --- (rides on Google OAuth).
# Claude reads unread/labeled emails and extracts actionable tasks.
# Costs Anthropic tokens per scan, so it's opt-in.
if (os.environ.get("GOOGLE_CALENDAR_CREDENTIALS")
        and os.environ.get("ENABLE_GMAIL_SCAN", "false").lower() in ("1", "true", "yes")):
    from paths import google_token_path as _gtp
    _gmail_labels = os.environ.get("GMAIL_LABEL_FILTER", "")
    TASK_SOURCES.append(GmailScanner(
        credentials_path=os.environ["GOOGLE_CALENDAR_CREDENTIALS"],
        token_path=str(_gtp()),
        label_filter=[l.strip() for l in _gmail_labels.split(",") if l.strip()] or None,
        max_messages=int(os.environ.get("GMAIL_MAX_MESSAGES", "25")),
        require_approval=_DEFAULT_REQUIRE_APPROVAL,
    ))

# --- Google Tasks --- (rides on Google OAuth). Free, no LLM calls.
if (os.environ.get("GOOGLE_CALENDAR_CREDENTIALS")
        and os.environ.get("ENABLE_GOOGLE_TASKS", "false").lower() in ("1", "true", "yes")):
    from paths import google_token_path as _gtp
    TASK_SOURCES.append(GoogleTasksProvider(
        credentials_path=os.environ["GOOGLE_CALENDAR_CREDENTIALS"],
        token_path=str(_gtp()),
        require_approval=_DEFAULT_REQUIRE_APPROVAL,
    ))

# --- Outlook Mail inbox scanner --- (rides on Microsoft Graph OAuth).
if (os.environ.get("MICROSOFT_CLIENT_ID")
        and os.environ.get("ENABLE_OUTLOOK_MAIL_SCAN", "false").lower() in ("1", "true", "yes")):
    _outlook_cats = os.environ.get("OUTLOOK_CATEGORY_FILTER", "")
    TASK_SOURCES.append(OutlookMailScanner(
        client_id=os.environ["MICROSOFT_CLIENT_ID"],
        tenant=os.environ.get("MICROSOFT_TENANT", "common"),
        folder=os.environ.get("OUTLOOK_MAIL_FOLDER", "inbox"),
        category_filter=[c.strip() for c in _outlook_cats.split(",") if c.strip()] or None,
        max_messages=int(os.environ.get("OUTLOOK_MAX_MESSAGES", "25")),
        require_approval=_DEFAULT_REQUIRE_APPROVAL,
    ))

# --- Microsoft To Do --- (rides on Microsoft Graph OAuth).
if (os.environ.get("MICROSOFT_CLIENT_ID")
        and os.environ.get("ENABLE_MICROSOFT_TODO", "false").lower() in ("1", "true", "yes")):
    TASK_SOURCES.append(MicrosoftTodoProvider(
        client_id=os.environ["MICROSOFT_CLIENT_ID"],
        tenant=os.environ.get("MICROSOFT_TENANT", "common"),
        require_approval=_DEFAULT_REQUIRE_APPROVAL,
    ))


# =======================================================================
# TODO SOURCE  — optional "user checked something off in their to-do app"
# signal used to auto-close matching tasks.
# =======================================================================
# Apple Reminders is macOS-only. On Windows / Linux this defaults to None
# (no todo cross-reference); swap in a cloud-API TodoSource like Todoist
# or Google Tasks when one lands in providers/.

from paths import reminders_fetch_binary_path

TODO_SOURCE: Optional[TodoSource] = (
    AppleRemindersTodoSource(binary_path=str(reminders_fetch_binary_path()))
    if _IS_MACOS else None
)


# =======================================================================
# NOTIFIER  — how the agent reaches you with the daily summary / updates.
# =======================================================================
# The first env var that's set wins, in the order below. To force a
# specific notifier, replace the `_pick_notifier()` call with the single
# constructor you want.
#
# Supported, in priority order:
#   1. iMessage (macOS only) — if USER_PHONE is set.
#   2. Pushover               — if PUSHOVER_USER_KEY and PUSHOVER_APP_TOKEN.
#   3. Slack                  — if SLACK_WEBHOOK_URL.
#   4. Email (SMTP)           — if SMTP_HOST + SMTP_USERNAME + SMTP_PASSWORD.
#   5. ntfy.sh                — if NTFY_TOPIC (default fallback, cross-platform).

def _pick_notifier() -> Notifier:
    if _IS_MACOS and os.environ.get("USER_PHONE"):
        return IMessageNotifier(
            script_path=str(_PROJECT_DIR / "macos" / "send_imessage.applescript"),
            recipient=os.environ["USER_PHONE"],
        )
    if os.environ.get("PUSHOVER_USER_KEY") and os.environ.get("PUSHOVER_APP_TOKEN"):
        return PushoverNotifier(
            user_key=os.environ["PUSHOVER_USER_KEY"],
            app_token=os.environ["PUSHOVER_APP_TOKEN"],
            device=os.environ.get("PUSHOVER_DEVICE") or None,
        )
    if os.environ.get("SLACK_WEBHOOK_URL"):
        return SlackNotifier(
            webhook_url=os.environ["SLACK_WEBHOOK_URL"],
            username=os.environ.get("SLACK_USERNAME") or "AutoPlan",
            icon_emoji=os.environ.get("SLACK_ICON_EMOJI") or ":calendar:",
        )
    if (os.environ.get("SMTP_HOST") and os.environ.get("SMTP_USERNAME")
            and os.environ.get("SMTP_PASSWORD")):
        return EmailNotifier(
            smtp_host=os.environ["SMTP_HOST"],
            smtp_port=int(os.environ.get("SMTP_PORT", "587")),
            username=os.environ["SMTP_USERNAME"],
            password=os.environ["SMTP_PASSWORD"],
            from_addr=os.environ.get("SMTP_FROM") or os.environ["SMTP_USERNAME"],
            to_addr=os.environ.get("SMTP_TO") or os.environ["SMTP_USERNAME"],
            use_tls=os.environ.get("SMTP_USE_TLS", "1") not in ("0", "false", "False"),
            use_ssl=os.environ.get("SMTP_USE_SSL", "0") in ("1", "true", "True"),
        )
    # Default fallback — works anywhere with a phone and the free ntfy app.
    return NtfyNotifier(
        topic=os.environ["NTFY_TOPIC"],
        default_tags=["calendar"],
    )

NOTIFIER: Notifier = _pick_notifier()


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
