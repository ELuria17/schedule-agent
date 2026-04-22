"""Tests for providers/microsoft_graph_auth.py.

We inject a fake `msal` module into sys.modules (the provider imports
msal lazily inside each function, so this sticks) and assert:
    * authorize_interactive drives PublicClientApplication +
      acquire_token_interactive, and persists the cache to disk.
    * get_access_token silently refreshes when the cache has an
      account, and raises when it doesn't.
    * token_status reports absent/present without touching msal in
      the absent case and without raising if msal deserialization
      blows up.

No real network or msal is required.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest


# ---------- Fake msal ----------

class _FakeSerializableTokenCache:
    """Mirrors the bits of msal.SerializableTokenCache we use."""
    _instances: list["_FakeSerializableTokenCache"] = []

    def __init__(self):
        self._state = ""
        self.has_state_changed = False
        _FakeSerializableTokenCache._instances.append(self)

    def serialize(self) -> str:
        return self._state or '{"fake":"cache"}'

    def deserialize(self, s: str) -> None:
        self._state = s

    # Test hook:
    def _mark_changed(self):
        self.has_state_changed = True


class _FakePublicClientApplication:
    """Scripts: accounts, silent_result, interactive_result."""
    instances: list["_FakePublicClientApplication"] = []
    script: dict = {"accounts": [], "silent": None, "interactive": None,
                    "on_interactive": None}

    def __init__(self, client_id, authority=None, token_cache=None):
        self.client_id = client_id
        self.authority = authority
        self.token_cache = token_cache
        _FakePublicClientApplication.instances.append(self)

    def get_accounts(self):
        return list(self.script.get("accounts") or [])

    def acquire_token_silent(self, scopes, account=None):
        return self.script.get("silent")

    def acquire_token_interactive(self, scopes, port=None):
        hook = self.script.get("on_interactive")
        if callable(hook):
            hook(self)
        return self.script.get("interactive")


@pytest.fixture(autouse=True)
def fake_msal(monkeypatch):
    """Stuff a fake msal module into sys.modules and reset state per test."""
    fake = types.ModuleType("msal")
    fake.SerializableTokenCache = _FakeSerializableTokenCache
    fake.PublicClientApplication = _FakePublicClientApplication
    monkeypatch.setitem(sys.modules, "msal", fake)
    _FakeSerializableTokenCache._instances.clear()
    _FakePublicClientApplication.instances.clear()
    _FakePublicClientApplication.script = {
        "accounts": [], "silent": None, "interactive": None,
        "on_interactive": None,
    }
    yield


# ---------- authorize_interactive ----------

class TestAuthorizeInteractive:
    def test_happy_path_writes_cache(self, tmp_path):
        from providers import microsoft_graph_auth as mg

        token_path = tmp_path / "microsoft_token.json"

        def _on_interactive(app):
            app.token_cache._mark_changed()

        _FakePublicClientApplication.script.update({
            "interactive": {"access_token": "new-token"},
            "on_interactive": _on_interactive,
        })
        mg.authorize_interactive(
            client_id="abc123",
            token_path=token_path,
        )
        assert token_path.exists()
        # The authority the app was built with reflects the default tenant.
        app = _FakePublicClientApplication.instances[-1]
        assert app.client_id == "abc123"
        assert app.authority == "https://login.microsoftonline.com/common"

    def test_respects_tenant_override(self, tmp_path):
        from providers import microsoft_graph_auth as mg

        def _on_interactive(app):
            app.token_cache._mark_changed()

        _FakePublicClientApplication.script.update({
            "interactive": {"access_token": "t"},
            "on_interactive": _on_interactive,
        })
        mg.authorize_interactive(
            client_id="abc",
            tenant="organizations",
            token_path=tmp_path / "m.json",
        )
        app = _FakePublicClientApplication.instances[-1]
        assert app.authority == "https://login.microsoftonline.com/organizations"

    def test_raises_on_error_result(self, tmp_path):
        from providers import microsoft_graph_auth as mg

        _FakePublicClientApplication.script["interactive"] = {
            "error": "interaction_required",
            "error_description": "user closed window",
        }
        with pytest.raises(RuntimeError, match="user closed window"):
            mg.authorize_interactive(
                client_id="abc",
                token_path=tmp_path / "m.json",
            )


# ---------- get_access_token ----------

class TestGetAccessToken:
    def test_returns_token_from_silent_refresh(self, tmp_path):
        from providers import microsoft_graph_auth as mg

        token_path = tmp_path / "m.json"
        token_path.write_text('{"seeded":"cache"}')

        _FakePublicClientApplication.script.update({
            "accounts": [{"username": "u@example.com", "home_account_id": "x"}],
            "silent": {"access_token": "live-token"},
        })
        token = mg.get_access_token(
            client_id="abc",
            token_path=token_path,
        )
        assert token == "live-token"

    def test_raises_when_no_cache_file(self, tmp_path):
        from providers import microsoft_graph_auth as mg

        with pytest.raises(RuntimeError, match="authorize_microsoft"):
            mg.get_access_token(
                client_id="abc",
                token_path=tmp_path / "does-not-exist.json",
            )

    def test_raises_when_cache_has_no_accounts(self, tmp_path):
        from providers import microsoft_graph_auth as mg

        token_path = tmp_path / "m.json"
        token_path.write_text('{"cache":"empty"}')

        _FakePublicClientApplication.script["accounts"] = []
        with pytest.raises(RuntimeError, match="no accounts"):
            mg.get_access_token(client_id="abc", token_path=token_path)

    def test_raises_when_silent_refresh_returns_none(self, tmp_path):
        from providers import microsoft_graph_auth as mg

        token_path = tmp_path / "m.json"
        token_path.write_text('{"cache":"x"}')

        _FakePublicClientApplication.script.update({
            "accounts": [{"username": "u@example.com"}],
            "silent": None,
        })
        with pytest.raises(RuntimeError, match="silent refresh failed"):
            mg.get_access_token(client_id="abc", token_path=token_path)


# ---------- token_status ----------

class TestTokenStatus:
    def test_absent_cache(self, tmp_path):
        from providers import microsoft_graph_auth as mg

        info = mg.token_status(token_path=tmp_path / "nope.json")
        assert info == {"token_present": False, "accounts": []}

    def test_present_cache_lists_accounts(self, tmp_path):
        from providers import microsoft_graph_auth as mg

        p = tmp_path / "m.json"
        p.write_text('{"cache":"x"}')
        _FakePublicClientApplication.script["accounts"] = [
            {"username": "one@example.com"},
            {"username": "two@example.com"},
        ]
        info = mg.token_status(token_path=p)
        assert info["token_present"] is True
        assert set(info["accounts"]) == {"one@example.com", "two@example.com"}

    def test_corrupt_cache_returns_empty_accounts(self, tmp_path, monkeypatch):
        from providers import microsoft_graph_auth as mg

        p = tmp_path / "m.json"
        p.write_text("not-json-at-all")

        def _bad_deserialize(self, s):
            raise ValueError("corrupt cache")

        monkeypatch.setattr(
            _FakeSerializableTokenCache, "deserialize", _bad_deserialize,
        )
        info = mg.token_status(token_path=p)
        assert info["token_present"] is True
        assert info["accounts"] == []


# ---------- graph_* HTTP helpers ----------

class _FakeResponse:
    def __init__(self, body=None, status_code=200):
        self._body = body or {}
        self.status_code = status_code
        self.content = b"x" if body is not None else b""

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class TestGraphHelpers:
    def test_graph_get_includes_bearer_header(self, monkeypatch):
        from providers import microsoft_graph_auth as mg

        captured: dict = {}

        def _fake_get(url, headers=None, params=None, timeout=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["params"] = params
            return _FakeResponse({"value": [1, 2, 3]})

        monkeypatch.setattr(mg.requests, "get", _fake_get)
        out = mg.graph_get("tok", "/me/calendars", params={"$top": "5"})
        assert out == {"value": [1, 2, 3]}
        assert captured["url"] == "https://graph.microsoft.com/v1.0/me/calendars"
        assert captured["headers"]["Authorization"] == "Bearer tok"
        assert captured["params"] == {"$top": "5"}

    def test_graph_post_sends_json_body(self, monkeypatch):
        from providers import microsoft_graph_auth as mg

        captured: dict = {}

        def _fake_post(url, headers=None, data=None, timeout=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["data"] = data
            return _FakeResponse({"id": "new-id"})

        monkeypatch.setattr(mg.requests, "post", _fake_post)
        out = mg.graph_post("tok", "/me/events", {"subject": "hi"})
        assert out == {"id": "new-id"}
        assert captured["headers"]["Content-Type"] == "application/json"
        assert "subject" in captured["data"]
        assert "hi" in captured["data"]

    def test_graph_delete_returns_none(self, monkeypatch):
        from providers import microsoft_graph_auth as mg

        seen = {}

        def _fake_delete(url, headers=None, timeout=None):
            seen["url"] = url
            return _FakeResponse(None, status_code=204)

        monkeypatch.setattr(mg.requests, "delete", _fake_delete)
        assert mg.graph_delete("tok", "/me/events/abc") is None
        assert seen["url"].endswith("/me/events/abc")
