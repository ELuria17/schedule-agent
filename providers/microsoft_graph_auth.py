"""Microsoft Graph authentication + thin HTTP helpers.

Shared by every Microsoft provider (OutlookCalendarProvider,
OutlookMailScanner, MicrosoftTodoProvider). There's no class here —
just a handful of module-level functions those providers call.

Why MSAL + public client?
-------------------------
Microsoft Graph for desktop apps uses the Microsoft Authentication
Library (MSAL). Unlike Google's installed-app flow which needs a
client_id + client_secret pair packaged with the download, MSAL
"public" clients authenticate with just a client_id — the secret
is never required, because public clients can't keep secrets
anyway. Each user creates their own Azure AD App Registration
(see docs/microsoft-graph-setup.md) and supplies only the
resulting Application (client) ID via env.

Tenants
-------
- "common"        — both personal (Outlook.com, Hotmail, etc.) and
                     work/school (Microsoft 365) accounts. Default.
- "consumers"     — only personal accounts.
- "organizations" — only work/school accounts.
- <tenant GUID>   — a specific Azure AD tenant.
Most self-hosters want "common". Enterprise users with conditional
access policies that block multi-tenant apps should pass their
tenant id.

Token cache
-----------
MSAL persists refresh + access tokens into a SerializableTokenCache.
We serialize that cache to disk at `token_path` (see
paths.microsoft_token_path()). The cache is JSON-serialized; it
contains refresh tokens, so treat it as you would ~/.ssh — don't
commit, don't share, 600 permissions where practical.

HTTP helpers
------------
`graph_get` / `graph_post` / `graph_patch` / `graph_delete` are
intentionally tiny. They prepend the v1.0 base URL, add the auth
header, call requests, raise on non-2xx, and return JSON (or None
for delete). Providers layer their own error handling on top.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import requests


# ---------- Module constants ----------

DEFAULT_SCOPES = [
    "Calendars.ReadWrite",
    "Mail.Read",
    "Tasks.ReadWrite",
    "User.Read",
    # `offline_access` is automatically added by MSAL when a refresh
    # token is needed; don't include it explicitly or MSAL flags the
    # scope list as containing a reserved value.
]
DEFAULT_AUTHORITY = "https://login.microsoftonline.com/common"  # both personal + work/school
REDIRECT_HOST = "localhost"
REDIRECT_PORT = 8766  # distinct from Google's 8765 so both can coexist

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
DEFAULT_HTTP_TIMEOUT = 30


# ---------- Cache helpers ----------

def _authority(tenant: str) -> str:
    return f"https://login.microsoftonline.com/{tenant}"


def _load_cache(token_path: Path):
    """Return a SerializableTokenCache, pre-loaded from `token_path` if it
    exists. MSAL is imported lazily so importing this module doesn't hard-
    require msal at runtime (keeps test seams clean when msal is faked)."""
    import msal

    cache = msal.SerializableTokenCache()
    token_path = Path(token_path).expanduser()
    if token_path.exists():
        try:
            cache.deserialize(token_path.read_text())
        except Exception:
            # A corrupt cache shouldn't brick auth — just start fresh.
            # The user's next acquire_token_interactive call rebuilds it.
            pass
    return cache


def _persist_cache(cache, token_path: Path) -> None:
    """Write cache to `token_path` if anything changed."""
    token_path = Path(token_path).expanduser()
    if cache.has_state_changed:
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(cache.serialize())


# ---------- Public auth API ----------

def authorize_interactive(
    *,
    client_id: str,
    tenant: str = "common",
    token_path: Path,
    scopes: Optional[list[str]] = None,
    port: int = REDIRECT_PORT,
) -> None:
    """Run the MSAL interactive sign-in flow and persist the token cache.

    Opens the user's browser to the Microsoft sign-in page, waits for
    them to complete consent, then writes the resulting refresh +
    access tokens into a serialized token cache at `token_path`.
    Safe to re-run (idempotent) to refresh or switch accounts — the
    existing cache is loaded first, and the new account is appended.

    Parameters
    ----------
    client_id : str
        The Application (client) ID from your Azure AD app
        registration. Public clients ONLY need this — no secret.
    tenant : str
        Azure tenant to authenticate against. "common" (default)
        accepts both personal and work/school accounts. Pass a
        specific tenant GUID when using an organization-only app.
    token_path : Path
        File where the serialized token cache will be written.
        Typically `paths.microsoft_token_path()`.
    scopes : list[str], optional
        Graph scopes to request. Defaults to DEFAULT_SCOPES which
        cover every Microsoft provider this app ships.
    port : int
        Local loopback port for the OAuth redirect. Default 8766.
    """
    import msal

    scopes = list(scopes) if scopes is not None else list(DEFAULT_SCOPES)
    token_path = Path(token_path).expanduser()

    cache = _load_cache(token_path)
    app = msal.PublicClientApplication(
        client_id,
        authority=_authority(tenant),
        token_cache=cache,
    )
    result = app.acquire_token_interactive(scopes, port=port)
    if not isinstance(result, dict) or "access_token" not in result:
        err = (result or {}).get("error_description") or (result or {}).get("error") or "unknown"
        raise RuntimeError(f"Microsoft interactive sign-in failed: {err}")
    _persist_cache(cache, token_path)


def get_access_token(
    *,
    client_id: str,
    tenant: str = "common",
    token_path: Path,
    scopes: Optional[list[str]] = None,
) -> str:
    """Return a valid access token, refreshing silently if needed.

    Loads the serialized token cache from `token_path`, constructs a
    PublicClientApplication, and calls `acquire_token_silent` against
    the first account in the cache. If that fails (no cache, cache
    without refresh token, expired refresh token, etc.) raises
    RuntimeError — callers should tell the user to run
    `python authorize_microsoft.py` again.

    Every provider calls this before every Graph API request. MSAL
    itself caches the access token in memory for its remaining
    lifetime, so repeated calls within a 50-60 minute window don't
    hit the network — MSAL just returns the same token. A higher-
    level LRU isn't necessary.

    Parameters
    ----------
    client_id : str
        Application (client) ID from the Azure AD app registration.
    tenant : str
        Azure tenant (default "common"). Must match the tenant used
        during authorize_interactive() — MSAL accounts are scoped
        to the authority they were acquired under.
    token_path : Path
        Path to the serialized token cache.
    scopes : list[str], optional
        Scopes to request on the access token. Defaults to
        DEFAULT_SCOPES. Must be a subset of scopes granted during
        interactive consent; otherwise MSAL falls back to an
        interactive flow (which we don't want in headless paths).
    """
    import msal

    scopes = list(scopes) if scopes is not None else list(DEFAULT_SCOPES)
    token_path = Path(token_path).expanduser()

    cache = _load_cache(token_path)
    if not token_path.exists() and not cache.has_state_changed:
        raise RuntimeError(
            "No Microsoft token cache found — run "
            "`python authorize_microsoft.py` once to sign in."
        )

    app = msal.PublicClientApplication(
        client_id,
        authority=_authority(tenant),
        token_cache=cache,
    )
    accounts = app.get_accounts() or []
    if not accounts:
        raise RuntimeError(
            "Microsoft token cache has no accounts — run "
            "`python authorize_microsoft.py` to sign in."
        )
    result = app.acquire_token_silent(scopes, account=accounts[0])
    # A successful silent refresh may update the cache.
    _persist_cache(cache, token_path)
    if not result or "access_token" not in result:
        raise RuntimeError(
            "No valid Microsoft token — silent refresh failed. "
            "Re-run `python authorize_microsoft.py`."
        )
    return result["access_token"]


def token_status(*, token_path: Path) -> dict:
    """Inspect the cache without triggering a refresh.

    Returns {"token_present": bool, "accounts": [username, ...]}.
    Used by provider health_check() methods. Never raises — a
    missing cache, a corrupt cache, or a missing msal package all
    return token_present=False.
    """
    token_path = Path(token_path).expanduser()
    if not token_path.exists():
        return {"token_present": False, "accounts": []}
    try:
        import msal
    except Exception:
        # msal not installed → we still know a cache file exists on
        # disk, but we can't parse it. Report presence but no accounts.
        return {"token_present": True, "accounts": []}
    try:
        cache = msal.SerializableTokenCache()
        cache.deserialize(token_path.read_text())
        # Accounts are found by walking the cache dict. MSAL exposes
        # this via any PublicClientApplication built on top, but we
        # can avoid constructing one just for introspection by reading
        # the cache's `find` API.
        app = msal.PublicClientApplication(
            "00000000-0000-0000-0000-000000000000",
            authority=DEFAULT_AUTHORITY,
            token_cache=cache,
        )
        usernames = [a.get("username", "") for a in (app.get_accounts() or [])]
        return {"token_present": True, "accounts": usernames}
    except Exception:
        return {"token_present": True, "accounts": []}


# ---------- Graph HTTP helpers ----------

def _auth_headers(access_token: str, *, extra: Optional[dict] = None) -> dict:
    h = {"Authorization": f"Bearer {access_token}",
         "Accept": "application/json"}
    if extra:
        h.update(extra)
    return h


def graph_get(
    access_token: str,
    path: str,
    params: Optional[dict] = None,
    *,
    timeout: int = DEFAULT_HTTP_TIMEOUT,
) -> dict:
    """GET {GRAPH_BASE_URL}{path}. Returns response.json(). Raises HTTPError on non-2xx."""
    r = requests.get(
        f"{GRAPH_BASE_URL}{path}",
        headers=_auth_headers(access_token),
        params=params,
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()


def graph_post(
    access_token: str,
    path: str,
    body: dict,
    *,
    timeout: int = DEFAULT_HTTP_TIMEOUT,
) -> dict:
    """POST JSON `body` to {GRAPH_BASE_URL}{path}. Returns response.json()."""
    r = requests.post(
        f"{GRAPH_BASE_URL}{path}",
        headers=_auth_headers(access_token, extra={"Content-Type": "application/json"}),
        data=json.dumps(body),
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json() if r.content else {}


def graph_patch(
    access_token: str,
    path: str,
    body: dict,
    *,
    timeout: int = DEFAULT_HTTP_TIMEOUT,
) -> dict:
    """PATCH JSON `body` to {GRAPH_BASE_URL}{path}. Returns response.json()."""
    r = requests.patch(
        f"{GRAPH_BASE_URL}{path}",
        headers=_auth_headers(access_token, extra={"Content-Type": "application/json"}),
        data=json.dumps(body),
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json() if r.content else {}


def graph_delete(
    access_token: str,
    path: str,
    *,
    timeout: int = DEFAULT_HTTP_TIMEOUT,
) -> None:
    """DELETE {GRAPH_BASE_URL}{path}. Returns None; raises on non-2xx."""
    r = requests.delete(
        f"{GRAPH_BASE_URL}{path}",
        headers=_auth_headers(access_token),
        timeout=timeout,
    )
    r.raise_for_status()
    return None
