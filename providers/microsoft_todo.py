"""Microsoft To Do task source.

Pulls tasks from the user's Microsoft To Do lists (the built-in
todo app that ships with Outlook.com / Microsoft 365). Active
(non-completed) tasks become SourceTask rows; completed tasks are
still emitted so the orchestrator closes their locally-scheduled
equivalents. Priority_hint maps Microsoft's three-level importance
(high / normal / low) onto our four-level scale (asap / high /
medium / low), with normal → medium and asap reserved for Canvas
/ LLM extractions that know a real deadline is <24h away.

API surface used:
    GET /me/todo/lists
    GET /me/todo/lists/{list_id}/tasks?$filter=...

We do NOT drop completed tasks server-side — instead we fetch
everything and let the orchestrator's authoritative-upstream logic
close any locally-open task the user finished in To Do. That
mirrors how the Canvas provider + Todoist behave (both are also
authoritative over their own completion state).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import microsoft_graph_auth as mg
from .task_source import SourceTask, TaskSource


_IMPORTANCE_MAP = {
    "high": "high",
    "normal": None,   # normal maps to no hint — solver uses default
    "low": "low",
}


class MicrosoftTodoProvider(TaskSource):
    """Microsoft To Do task source.

    Parameters
    ----------
    client_id : str
        Azure AD app Application (client) ID. Shared with the
        calendar + mail providers — one MSAL token cache covers
        all Microsoft providers in this app.
    tenant : str, default "common"
        Azure tenant to authenticate against. See the calendar
        provider for tenant semantics.
    token_path : Path, optional
        MSAL token cache path. Defaults to
        `paths.microsoft_token_path()`.
    list_ids : list[str], optional
        Restrict to a specific set of To Do lists by their
        `todoTaskList` id. If None (default), every list the
        account has access to is scanned. Handy when To Do mixes
        shopping / personal / work lists and only some are
        actually schedulable work.
    require_approval : bool, default False
        If True, new tasks land in pending_review instead of
        scheduled. Off by default — To Do entries are usually
        already-triaged by the user, unlike LLM-extracted email
        tasks.
    """

    SOURCE = "microsoft_todo"

    def __init__(
        self,
        *,
        client_id: str,
        tenant: str = "common",
        token_path: Optional[Path] = None,
        list_ids: Optional[list[str]] = None,
        require_approval: bool = False,
    ):
        self.client_id = client_id
        self.tenant = tenant
        if token_path is None:
            from paths import microsoft_token_path
            token_path = microsoft_token_path()
        self.token_path = Path(token_path).expanduser()
        self.list_ids = set(list_ids) if list_ids else None
        self.require_approval = require_approval

    # ---- TaskSource contract ----

    def list_active_tasks(self) -> list[SourceTask]:
        try:
            token = mg.get_access_token(
                client_id=self.client_id,
                tenant=self.tenant,
                token_path=self.token_path,
            )
        except Exception:
            return []

        try:
            lists_resp = mg.graph_get(token, "/me/todo/lists")
        except Exception:
            return []
        lists = lists_resp.get("value", []) or []

        out: list[SourceTask] = []
        for lst in lists:
            list_id = lst.get("id")
            list_name = lst.get("displayName") or ""
            if not list_id:
                continue
            if self.list_ids is not None and list_id not in self.list_ids:
                continue
            try:
                tasks_resp = mg.graph_get(
                    token,
                    f"/me/todo/lists/{list_id}/tasks",
                    params={"$top": "200"},
                )
            except Exception:
                continue
            for raw in tasks_resp.get("value", []) or []:
                try:
                    out.append(self._to_source_task(raw, list_id, list_name))
                except Exception:
                    continue
        return out

    def health_check(self) -> dict:
        info = {
            "source": self.SOURCE,
            "require_approval": self.require_approval,
            "client_id": self.client_id,
            "tenant": self.tenant,
            "list_filter": sorted(self.list_ids) if self.list_ids else "all",
        }
        status = mg.token_status(token_path=self.token_path)
        info["token_present"] = status["token_present"]
        info["accounts"] = status["accounts"]
        if not status["token_present"]:
            info["error"] = (
                "No Microsoft token yet. Run "
                "`python authorize_microsoft.py` once to complete sign-in."
            )
        return info

    # ---- Internals ----

    def _to_source_task(self, t: dict, list_id: str, list_name: str) -> SourceTask:
        task_id = t.get("id") or ""
        title = t.get("title") or "(untitled To Do task)"
        status = (t.get("status") or "").lower()
        is_completed = status == "completed"

        importance = (t.get("importance") or "").lower()
        priority_hint = _IMPORTANCE_MAP.get(importance)

        deadline_utc = _parse_todo_datetime(t.get("dueDateTime"))

        body = t.get("body") or {}
        notes = body.get("content") or None
        # Body contentType can be "text" or "html"; the LLM loop doesn't
        # need nuance here so we hand the raw string through.

        return SourceTask(
            source=self.SOURCE,
            source_id=f"{list_id}::{task_id}",
            title=title,
            course=list_name or None,  # surface list name as course-ish tag
            deadline_utc=deadline_utc,
            is_completed=is_completed,
            duration_hint_min=None,
            priority_hint=priority_hint,
            notes=notes,
            extra={
                "list_id": list_id,
                "list_name": list_name,
                "importance": importance or None,
                "status": status or None,
            },
        )


# ---- Module-private helpers ----

def _parse_todo_datetime(dtz: Optional[dict]) -> Optional[datetime]:
    """Microsoft To Do returns {"dateTime": "2026-04-22T17:00:00.0000000",
    "timeZone": "UTC"} (or the user's local zone). We treat the string as
    UTC if the zone is "UTC"; otherwise we still trust the wall-clock
    value as UTC, since the orchestrator only deals in UTC and getting
    zoneinfo right across platforms isn't worth a dep for a "deadline-
    by-end-of-day" field that almost never hinges on exact minutes."""
    if not isinstance(dtz, dict):
        return None
    raw = dtz.get("dateTime")
    if not raw:
        return None
    # Trim fractional seconds to 6 digits to keep fromisoformat happy.
    cleaned = raw
    if "." in cleaned:
        head, frac = cleaned.split(".", 1)
        frac = frac[:6]
        cleaned = f"{head}.{frac}"
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        try:
            dt = datetime.strptime(cleaned[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
