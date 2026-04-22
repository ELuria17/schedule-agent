"""Integration tests for setup_server.py wizard routes.

Uses FastAPI's TestClient so every route renders with real HTML and
every POST exercises the actual form-parsing logic. We stub out the
Anthropic `setup.main()` call (which would otherwise try to create a
real agent) and the Apple Reminders compile step, isolating the tests
from the network.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Return a TestClient against setup_server with .env pointed at tmp."""
    fake_env = tmp_path / ".env"
    fake_env.touch()

    # install._read_env / _write_env both go through paths.env_path().
    import paths
    monkeypatch.setattr(paths, "env_path", lambda: fake_env)

    # Also redirect token paths into the temp dir so the Google-upload
    # + MS-save endpoints don't collide with real files.
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(paths, "google_token_path", lambda: tmp_path / "google_token.json")
    monkeypatch.setattr(paths, "microsoft_token_path", lambda: tmp_path / "microsoft_token.json")

    # Re-import setup_server so it picks up the patched paths.
    import sys, importlib
    for name in ("setup_server", "install"):
        sys.modules.pop(name, None)
    import setup_server as _ss
    # Patch setup_server's direct imports too (we import data_dir + token_path
    # at module scope in setup_server).
    monkeypatch.setattr(_ss, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(_ss, "google_token_path", lambda: tmp_path / "google_token.json")
    monkeypatch.setattr(_ss, "microsoft_token_path", lambda: tmp_path / "microsoft_token.json")

    # Stub the Anthropic-agent creation call so POST /setup doesn't hit
    # the network. Also stub `bootstrap.run_all()` (the Apple Reminders
    # compile step) to avoid macOS-only compile.
    import importlib
    import setup as _setup_mod
    monkeypatch.setattr(_setup_mod, "main", lambda: None)
    # When setup_server reloads setup, our stub is preserved because we
    # patch setup_mod directly; importlib.reload re-runs module code which
    # would clobber our monkeypatch. Patch the reload site instead.
    monkeypatch.setattr(importlib, "reload", lambda m: m)

    import bootstrap as _bs
    monkeypatch.setattr(_bs, "run_all", lambda: [])

    return TestClient(_ss.app), fake_env


# ---------- GET routes ----------

class TestGetRoutes:
    def test_setup_form_renders(self, client):
        c, _ = client
        r = c.get("/setup")
        assert r.status_code == 200
        t = r.text
        # Every calendar option present.
        assert "Apple Calendar (iCloud)" in t
        assert "Google Calendar" in t
        assert "Outlook / Microsoft 365" in t
        assert "CalDAV" in t
        # Every task-source checkbox present.
        assert "Canvas LMS" in t
        assert "Todoist" in t
        assert "Gmail inbox scanning" in t
        assert "Google Tasks" in t
        assert "Outlook Mail inbox scanning" in t
        assert "Microsoft To Do" in t
        # Every notifier option present.
        for n in ("iMessage", "Pushover", "Slack", "Email (SMTP)", "ntfy"):
            assert n in t
        # Auto-vs-Review toggle.
        assert "How new tasks arrive" in t
        assert "Review first" in t

    def test_google_wizard_renders(self, client):
        c, _ = client
        r = c.get("/setup/google")
        assert r.status_code == 200
        assert "Connect Google Calendar" in r.text
        assert "credentials.json" in r.text

    def test_microsoft_wizard_renders(self, client):
        c, _ = client
        r = c.get("/setup/microsoft")
        assert r.status_code == 200
        t = r.text
        assert "Connect Microsoft 365" in t
        assert "Application (client) ID" in t
        assert "Calendars.ReadWrite" in t
        assert "Mail.Read" in t
        assert "Tasks.ReadWrite" in t
        assert "localhost:8766" in t


# ---------- Google sub-wizard ----------

class TestGoogleWizardUpload:
    def test_rejects_non_json(self, client):
        c, _ = client
        r = c.post("/setup/google/upload",
                   files={"credentials": ("bad.json", b"nope", "application/json")})
        assert r.status_code == 400
        assert "JSON" in r.json()["error"]

    def test_rejects_json_without_installed_or_web_key(self, client):
        c, _ = client
        bad = json.dumps({"something_else": {}}).encode()
        r = c.post("/setup/google/upload",
                   files={"credentials": ("bad.json", bad, "application/json")})
        assert r.status_code == 400
        assert "credentials.json" in r.json()["error"]

    def test_accepts_installed_shape(self, client, tmp_path):
        c, env = client
        good = json.dumps({
            "installed": {"client_id": "x", "client_secret": "y",
                          "redirect_uris": ["http://localhost"]},
        }).encode()
        r = c.post("/setup/google/upload",
                   files={"credentials": ("credentials.json", good,
                                          "application/json")})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        # File landed in the temp data_dir
        assert (tmp_path / "credentials.json").exists()
        # .env was updated
        env_text = env.read_text()
        assert "GOOGLE_CALENDAR_CREDENTIALS=" in env_text

    def test_accepts_web_shape(self, client):
        """Google occasionally returns a "web" key for OAuth clients
        registered with "Web application" instead of "Desktop app" —
        the wizard accepts either rather than failing hard."""
        c, _ = client
        good = json.dumps({
            "web": {"client_id": "x", "client_secret": "y"},
        }).encode()
        r = c.post("/setup/google/upload",
                   files={"credentials": ("creds.json", good,
                                          "application/json")})
        assert r.status_code == 200


class TestGoogleAuthorizeStatus:
    def test_authorize_without_creds_errors(self, client):
        c, _ = client
        r = c.post("/setup/google/authorize")
        assert r.status_code == 400
        assert "credentials.json" in r.json()["error"].lower()

    def test_status_defaults_to_idle(self, client):
        c, _ = client
        r = c.get("/setup/google/status")
        # Each test gets a fresh setup_server reload, so status starts at "idle".
        assert r.status_code == 200
        assert r.json()["status"] in ("idle", "pending", "success", "error")


# ---------- Microsoft sub-wizard ----------

class TestMicrosoftSave:
    def test_missing_client_id_errors(self, client):
        c, _ = client
        r = c.post("/setup/microsoft/save",
                   data={"client_id": "", "tenant": "common"})
        assert r.status_code == 400
        assert "Client ID" in r.json()["error"]

    def test_saves_client_id_to_env(self, client):
        c, env = client
        r = c.post("/setup/microsoft/save",
                   data={"client_id": "11111111-2222-3333-4444-555555555555",
                         "tenant": "common"})
        assert r.status_code == 200
        assert r.json()["ok"] is True
        env_text = env.read_text()
        assert "MICROSOFT_CLIENT_ID=11111111-2222-3333-4444-555555555555" in env_text
        assert "MICROSOFT_TENANT=common" in env_text

    def test_tenant_defaults_to_common_when_blank(self, client):
        c, env = client
        c.post("/setup/microsoft/save",
               data={"client_id": "cid", "tenant": ""})
        assert "MICROSOFT_TENANT=common" in env.read_text()


class TestMicrosoftAuthorize:
    def test_authorize_without_client_id_errors(self, client):
        c, _ = client
        r = c.post("/setup/microsoft/authorize")
        assert r.status_code == 400
        assert "Client ID" in r.json()["error"] or "client id" in r.json()["error"].lower()

    def test_status_endpoint_returns_state_dict(self, client):
        c, _ = client
        r = c.get("/setup/microsoft/status")
        assert r.status_code == 200
        assert "status" in r.json()


# ---------- POST /setup — final submit ----------

class TestSetupSubmit:
    def _base_form(self):
        return {
            "anthropic_api_key": "sk-ant-fake",
            "replan_token": "replan",
            "timezone": "America/New_York",
            "approval_mode": "auto",
            "calendar": "icloud",
            "icloud_user": "me@example.com",
            "icloud_app_password": "xxxx-xxxx-xxxx-xxxx",
            "write_calendar_name": "Study Blocks",
            "notifier": "ntfy",
            "ntfy_topic": "autoplan-secret",
        }

    def test_minimum_viable_submit(self, client):
        c, env = client
        r = c.post("/setup", data=self._base_form())
        assert r.status_code == 200, r.text[:400]
        env_text = env.read_text()
        assert "ANTHROPIC_API_KEY=sk-ant-fake" in env_text
        assert "ICLOUD_USER=me@example.com" in env_text
        assert "NTFY_TOPIC=autoplan-secret" in env_text
        assert "DEFAULT_REQUIRE_APPROVAL=false" in env_text

    def test_review_mode_flips_default_require_approval(self, client):
        c, env = client
        form = self._base_form()
        form["approval_mode"] = "review"
        c.post("/setup", data=form)
        assert "DEFAULT_REQUIRE_APPROVAL=true" in env.read_text()

    def test_missing_anthropic_key_errors(self, client):
        c, _ = client
        form = self._base_form()
        form["anthropic_api_key"] = ""
        r = c.post("/setup", data=form)
        assert r.status_code == 400
        assert "Anthropic" in r.text

    def test_gmail_without_google_creds_errors(self, client):
        c, _ = client
        form = self._base_form()
        form["src_gmail"] = "1"
        r = c.post("/setup", data=form)
        assert r.status_code == 400
        assert "Google Calendar connection" in r.text

    def test_outlook_mail_without_microsoft_client_errors(self, client):
        c, _ = client
        form = self._base_form()
        form["src_outlook_mail"] = "1"
        r = c.post("/setup", data=form)
        assert r.status_code == 400
        assert "Microsoft 365 client ID" in r.text or "Microsoft 365" in r.text

    def test_full_stack_with_google_and_microsoft_writes_all_flags(self, client):
        c, env = client
        # Pretend the user already did the two sub-wizards:
        env_path_obj = env  # reuse fixture-provided path
        env_path_obj.write_text(
            "GOOGLE_CALENDAR_CREDENTIALS=/tmp/creds.json\n"
            "MICROSOFT_CLIENT_ID=cid-abc\n"
            "MICROSOFT_TENANT=common\n"
        )
        form = self._base_form()
        form["calendar"] = "google"
        form["src_canvas"] = "1"
        form["canvas_token"] = "ctoken"
        form["src_todoist"] = "1"
        form["todoist_token"] = "ttoken"
        form["src_gmail"] = "1"
        form["src_gtasks"] = "1"
        form["src_outlook_mail"] = "1"
        form["src_mstodo"] = "1"
        r = c.post("/setup", data=form)
        assert r.status_code == 200, r.text[:400]
        t = env.read_text()
        # Every enable flag written
        for flag in ("ENABLE_GMAIL_SCAN=true", "ENABLE_GOOGLE_TASKS=true",
                     "ENABLE_OUTLOOK_MAIL_SCAN=true", "ENABLE_MICROSOFT_TODO=true"):
            assert flag in t, f"missing flag in .env: {flag}"
        # Google creds preserved (wizard sets them, submit doesn't overwrite)
        assert "GOOGLE_CALENDAR_CREDENTIALS=/tmp/creds.json" in t
        # Microsoft client_id preserved too
        assert "MICROSOFT_CLIENT_ID=cid-abc" in t

    def test_outlook_calendar_choice_preserves_microsoft_client_id(self, client):
        c, env = client
        env.write_text("MICROSOFT_CLIENT_ID=pre-saved-cid\n"
                       "MICROSOFT_TENANT=common\n")
        form = self._base_form()
        form["calendar"] = "outlook"
        form.pop("icloud_user", None)
        form.pop("icloud_app_password", None)
        r = c.post("/setup", data=form)
        assert r.status_code == 200, r.text[:400]
        t = env.read_text()
        assert "MICROSOFT_CLIENT_ID=pre-saved-cid" in t
        assert "ENABLE_OUTLOOK_CALENDAR=true" in t
