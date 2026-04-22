"""End-to-end wiring tests for schedule_config.py.

Exercises every documented env-var combination to confirm the cascade
picks the right CALENDAR provider and enables the intended task sources.
Nothing here hits the network — we only assert the *types* of the
instantiated providers, which is enough to prove `schedule_config`
reads env vars correctly.

These tests use a subprocess-free pattern: for each case, we manipulate
`os.environ` and re-import `schedule_config` in a clean namespace. That's
more work than monkeypatching, but schedule_config.py does all of its
work at module-import time, so a reload is the only way to exercise the
cascade honestly.
"""
from __future__ import annotations

import importlib
import sys

import pytest


@pytest.fixture
def reimport_config(monkeypatch):
    """Blow away any cached schedule_config so the next import re-runs the
    top-level cascade. Caller seeds os.environ *before* calling
    reimport_config() for each scenario."""
    def _reimport():
        # Drop any cached copies of schedule_config and every provider
        # module it imports — pytest collection may have cached them
        # against another test's env.
        for name in list(sys.modules):
            if name == "schedule_config":
                del sys.modules[name]
        return importlib.import_module("schedule_config")
    # Clear every key we manipulate so tests are independent.
    clear_keys = [
        "GOOGLE_CALENDAR_CREDENTIALS",
        "MICROSOFT_CLIENT_ID", "MICROSOFT_TENANT", "ENABLE_OUTLOOK_CALENDAR",
        "CALDAV_URL", "CALDAV_USER", "CALDAV_PASSWORD",
        "ICLOUD_USER", "ICLOUD_APP_PASSWORD",
        "CANVAS_TOKEN", "CANVAS_BASE_URL",
        "TODOIST_TOKEN",
        "ENABLE_GMAIL_SCAN", "GMAIL_LABEL_FILTER", "GMAIL_MAX_MESSAGES",
        "ENABLE_GOOGLE_TASKS",
        "ENABLE_OUTLOOK_MAIL_SCAN", "OUTLOOK_MAIL_FOLDER",
        "OUTLOOK_CATEGORY_FILTER", "OUTLOOK_MAX_MESSAGES",
        "ENABLE_MICROSOFT_TODO",
        "DEFAULT_REQUIRE_APPROVAL",
        "USER_PHONE", "PUSHOVER_USER_KEY", "PUSHOVER_APP_TOKEN",
        "SLACK_WEBHOOK_URL", "SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD",
    ]
    for k in clear_keys:
        monkeypatch.delenv(k, raising=False)
    # A notifier is required for schedule_config to finish importing.
    # ntfy is the safest default — needs only a topic string.
    monkeypatch.setenv("NTFY_TOPIC", "test-topic")
    # A calendar is required too. iCloud is the final-fallback branch;
    # seed it so tests that don't override the cascade can still import.
    monkeypatch.setenv("ICLOUD_USER", "fallback@example.com")
    monkeypatch.setenv("ICLOUD_APP_PASSWORD", "xxxx-xxxx-xxxx-xxxx")
    return _reimport


# ---------- Calendar cascade ----------

class TestCalendarCascade:
    def test_google_wins_when_credentials_path_set(self, reimport_config, monkeypatch):
        monkeypatch.setenv("GOOGLE_CALENDAR_CREDENTIALS", "/tmp/creds.json")
        monkeypatch.setenv("MICROSOFT_CLIENT_ID", "should-not-win")
        monkeypatch.setenv("ENABLE_OUTLOOK_CALENDAR", "true")
        monkeypatch.setenv("CALDAV_URL", "https://should-not-win.example.com/")
        sc = reimport_config()
        assert type(sc.CALENDAR).__name__ == "GoogleCalendarProvider"

    def test_outlook_wins_over_caldav_when_enabled(self, reimport_config, monkeypatch):
        monkeypatch.setenv("MICROSOFT_CLIENT_ID", "client-abc")
        monkeypatch.setenv("ENABLE_OUTLOOK_CALENDAR", "true")
        monkeypatch.setenv("CALDAV_URL", "https://should-not-win.example.com/")
        monkeypatch.setenv("CALDAV_USER", "x")
        monkeypatch.setenv("CALDAV_PASSWORD", "y")
        sc = reimport_config()
        assert type(sc.CALENDAR).__name__ == "OutlookCalendarProvider"

    def test_microsoft_client_id_alone_does_not_pick_outlook(self, reimport_config, monkeypatch):
        """Microsoft credentials are reused by Outlook Mail / MS To Do
        even when the user doesn't want Outlook as the write calendar."""
        monkeypatch.setenv("MICROSOFT_CLIENT_ID", "client-abc")
        # ENABLE_OUTLOOK_CALENDAR left unset
        monkeypatch.setenv("CALDAV_URL", "https://caldav.fastmail.com/")
        monkeypatch.setenv("CALDAV_USER", "u@fastmail.com")
        monkeypatch.setenv("CALDAV_PASSWORD", "p")
        sc = reimport_config()
        assert type(sc.CALENDAR).__name__ == "ICloudCalDAVProvider"  # aliased as Generic

    def test_caldav_wins_over_icloud_when_url_set(self, reimport_config, monkeypatch):
        monkeypatch.setenv("CALDAV_URL", "https://caldav.fastmail.com/")
        monkeypatch.setenv("CALDAV_USER", "u@fastmail.com")
        monkeypatch.setenv("CALDAV_PASSWORD", "p")
        monkeypatch.setenv("ICLOUD_USER", "should-not-win@icloud.com")
        monkeypatch.setenv("ICLOUD_APP_PASSWORD", "xxxx")
        sc = reimport_config()
        assert isinstance(sc.CALENDAR, sc.ICloudCalDAVProvider)
        assert sc.CALENDAR.url == "https://caldav.fastmail.com/"

    def test_icloud_as_last_resort(self, reimport_config, monkeypatch):
        monkeypatch.setenv("ICLOUD_USER", "me@example.com")
        monkeypatch.setenv("ICLOUD_APP_PASSWORD", "xxxx-xxxx-xxxx-xxxx")
        sc = reimport_config()
        assert type(sc.CALENDAR).__name__ == "ICloudCalDAVProvider"
        assert sc.CALENDAR.url == "https://caldav.icloud.com/"


# ---------- Task sources ----------

class TestTaskSources:
    def test_no_sources_when_none_configured(self, reimport_config, monkeypatch):
        monkeypatch.setenv("ICLOUD_USER", "me@example.com")
        monkeypatch.setenv("ICLOUD_APP_PASSWORD", "xxxx")
        sc = reimport_config()
        assert sc.TASK_SOURCES == []

    def test_canvas_only(self, reimport_config, monkeypatch):
        monkeypatch.setenv("ICLOUD_USER", "me@example.com")
        monkeypatch.setenv("ICLOUD_APP_PASSWORD", "xxxx")
        monkeypatch.setenv("CANVAS_TOKEN", "canvas-token")
        monkeypatch.setenv("CANVAS_BASE_URL", "https://psu.instructure.com/api/v1")
        sc = reimport_config()
        assert [type(s).__name__ for s in sc.TASK_SOURCES] == ["CanvasTaskSource"]

    def test_gmail_requires_google_credentials(self, reimport_config, monkeypatch):
        """ENABLE_GMAIL_SCAN without GOOGLE_CALENDAR_CREDENTIALS must
        silently skip the scanner — we don't want the orchestrator
        crashing on a half-configured setup."""
        monkeypatch.setenv("ICLOUD_USER", "me@example.com")
        monkeypatch.setenv("ICLOUD_APP_PASSWORD", "xxxx")
        monkeypatch.setenv("ENABLE_GMAIL_SCAN", "true")
        sc = reimport_config()
        assert sc.TASK_SOURCES == []

    def test_gmail_and_tasks_both_enable_off_google_creds(self, reimport_config, monkeypatch):
        monkeypatch.setenv("GOOGLE_CALENDAR_CREDENTIALS", "/tmp/creds.json")
        monkeypatch.setenv("ENABLE_GMAIL_SCAN", "true")
        monkeypatch.setenv("ENABLE_GOOGLE_TASKS", "true")
        sc = reimport_config()
        names = [type(s).__name__ for s in sc.TASK_SOURCES]
        assert "GmailScanner" in names
        assert "GoogleTasksProvider" in names

    def test_outlook_mail_requires_microsoft_client_id(self, reimport_config, monkeypatch):
        monkeypatch.setenv("ICLOUD_USER", "me@example.com")
        monkeypatch.setenv("ICLOUD_APP_PASSWORD", "xxxx")
        monkeypatch.setenv("ENABLE_OUTLOOK_MAIL_SCAN", "true")
        sc = reimport_config()
        assert sc.TASK_SOURCES == []

    def test_outlook_mail_and_mstodo_both_ride_microsoft_creds(self, reimport_config, monkeypatch):
        monkeypatch.setenv("ICLOUD_USER", "me@example.com")
        monkeypatch.setenv("ICLOUD_APP_PASSWORD", "xxxx")
        monkeypatch.setenv("MICROSOFT_CLIENT_ID", "client-abc")
        monkeypatch.setenv("ENABLE_OUTLOOK_MAIL_SCAN", "true")
        monkeypatch.setenv("ENABLE_MICROSOFT_TODO", "true")
        sc = reimport_config()
        names = [type(s).__name__ for s in sc.TASK_SOURCES]
        assert "OutlookMailScanner" in names
        assert "MicrosoftTodoProvider" in names

    def test_every_source_enabled_at_once(self, reimport_config, monkeypatch):
        monkeypatch.setenv("GOOGLE_CALENDAR_CREDENTIALS", "/tmp/creds.json")
        monkeypatch.setenv("MICROSOFT_CLIENT_ID", "client-abc")
        monkeypatch.setenv("CANVAS_TOKEN", "c")
        monkeypatch.setenv("TODOIST_TOKEN", "t")
        monkeypatch.setenv("ENABLE_GMAIL_SCAN", "true")
        monkeypatch.setenv("ENABLE_GOOGLE_TASKS", "true")
        monkeypatch.setenv("ENABLE_OUTLOOK_MAIL_SCAN", "true")
        monkeypatch.setenv("ENABLE_MICROSOFT_TODO", "true")
        sc = reimport_config()
        names = [type(s).__name__ for s in sc.TASK_SOURCES]
        assert set(names) == {
            "CanvasTaskSource", "TodoistTaskSource",
            "GmailScanner", "GoogleTasksProvider",
            "OutlookMailScanner", "MicrosoftTodoProvider",
        }


# ---------- Approval policy propagates ----------

class TestApprovalPropagation:
    def test_default_require_approval_flag_reaches_every_source(self, reimport_config, monkeypatch):
        monkeypatch.setenv("DEFAULT_REQUIRE_APPROVAL", "true")
        monkeypatch.setenv("GOOGLE_CALENDAR_CREDENTIALS", "/tmp/creds.json")
        monkeypatch.setenv("MICROSOFT_CLIENT_ID", "client-abc")
        monkeypatch.setenv("CANVAS_TOKEN", "c")
        monkeypatch.setenv("TODOIST_TOKEN", "t")
        monkeypatch.setenv("ENABLE_GMAIL_SCAN", "true")
        monkeypatch.setenv("ENABLE_GOOGLE_TASKS", "true")
        monkeypatch.setenv("ENABLE_OUTLOOK_MAIL_SCAN", "true")
        monkeypatch.setenv("ENABLE_MICROSOFT_TODO", "true")
        sc = reimport_config()
        for src in sc.TASK_SOURCES:
            assert src.require_approval is True, (
                f"{type(src).__name__} did not pick up DEFAULT_REQUIRE_APPROVAL=true"
            )

    def test_require_approval_defaults_off(self, reimport_config, monkeypatch):
        monkeypatch.setenv("CANVAS_TOKEN", "c")
        monkeypatch.setenv("TODOIST_TOKEN", "t")
        sc = reimport_config()
        for src in sc.TASK_SOURCES:
            assert src.require_approval is False

    def test_accepted_truthy_values(self, reimport_config, monkeypatch):
        for val in ("true", "True", "1", "yes", "YES"):
            monkeypatch.setenv("DEFAULT_REQUIRE_APPROVAL", val)
            monkeypatch.setenv("CANVAS_TOKEN", "c")
            sc = reimport_config()
            assert sc.TASK_SOURCES[0].require_approval is True, f"failed for {val!r}"


# ---------- Notifier cascade ----------

class TestNotifierCascade:
    def test_imessage_wins_when_phone_and_macos(self, reimport_config, monkeypatch):
        # `_IS_MACOS` is resolved at import time from sys.platform.
        # On non-mac, iMessage is skipped — we only assert on darwin.
        if sys.platform != "darwin":
            pytest.skip("macOS-only cascade path")
        monkeypatch.setenv("USER_PHONE", "+15551234567")
        monkeypatch.setenv("PUSHOVER_USER_KEY", "shouldnotwin")
        sc = reimport_config()
        assert type(sc.NOTIFIER).__name__ == "IMessageNotifier"

    def test_pushover_wins_over_slack(self, reimport_config, monkeypatch):
        monkeypatch.setenv("PUSHOVER_USER_KEY", "uk")
        monkeypatch.setenv("PUSHOVER_APP_TOKEN", "at")
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/x")
        sc = reimport_config()
        assert type(sc.NOTIFIER).__name__ == "PushoverNotifier"

    def test_ntfy_is_final_fallback(self, reimport_config, monkeypatch):
        sc = reimport_config()  # fixture only seeds NTFY_TOPIC
        assert type(sc.NOTIFIER).__name__ == "NtfyNotifier"
