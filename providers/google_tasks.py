"""Google Tasks implementation of TaskSource.

Pulls active items from tasks.google.com (the checklist app Google
embeds in Calendar/Gmail sidebars). Shares the same OAuth stack as
GoogleCalendarProvider and GmailScanner — one credentials.json and one
google_token.json cover every Google provider in this app.

What this provider pulls:
- Every non-completed, non-hidden task across the user's task lists.
- Each task's title, due date (ISO 8601), and notes.
- Upstream completion state: if a user marks a task done inside Google
  Tasks, the next scan flips is_completed=True and the orchestrator
  closes the local row. Same pattern Canvas uses.

What it does NOT pull:
- Sub-tasks as separate rows. We flatten; `service.tasks().list()`
  returns parents and children at the same level, and we emit one
  SourceTask per row Google returns. If you want a strict hierarchy,
  Google Tasks is the wrong data store.
- Duration / priority hints. Google Tasks exposes neither concept, so
  `duration_hint_min` and `priority_hint` stay None and fall back to
  the orchestrator's defaults.

Credentials: the cached token must have been minted with
`tasks.readonly`. That scope is part of DEFAULT_SCOPES in
providers/google_calendar.py; users who authorized before it was added
need to delete google_token.json and re-run `python
authorize_google.py` once.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .task_source import SourceTask, TaskSource


DEFAULT_SCOPES = ["https://www.googleapis.com/auth/tasks.readonly"]


class GoogleTasksProvider(TaskSource):
    """Google Tasks TaskSource.

    Parameters
    ----------
    credentials_path: str | Path
        Path to the `credentials.json` file (same file Google Calendar
        uses). Must be a "Desktop" OAuth client.
    token_path: str | Path
        Where the refresh token is cached. Shared with every Google
        provider in this app. Minted with `tasks.readonly` via the
        widened DEFAULT_SCOPES in providers/google_calendar.py.
    list_ids: optional list[str]
        If provided, pull only from these task lists. List IDs come
        from `service.tasklists().list()`. If None (default), every
        visible list is scanned.
    require_approval: bool
        Forwarded to TaskSource base. When True, new tasks land in
        `pending_review` first. Google Tasks items are usually
        user-authored so approval is often unnecessary, but turn it on
        if a noisy shared list is wired in.
    """

    SOURCE = "google_tasks"

    def __init__(
        self,
        *,
        credentials_path: str | Path,
        token_path: str | Path,
        list_ids: Optional[list[str]] = None,
        require_approval: bool = False,
    ):
        self.credentials_path = Path(credentials_path).expanduser()
        self.token_path = Path(token_path).expanduser()
        self.list_ids = list(list_ids) if list_ids else None
        self.require_approval = require_approval
        self.scopes = list(DEFAULT_SCOPES)
        self._service = None  # lazy

    # ---- TaskSource contract ----

    def list_active_tasks(self) -> list[SourceTask]:
        try:
            svc = self._svc()
        except Exception:
            return []

        list_ids = self._resolve_list_ids(svc)
        if list_ids is None:
            return []

        out: list[SourceTask] = []
        for list_id in list_ids:
            try:
                resp = svc.tasks().list(
                    tasklist=list_id,
                    showCompleted=False,
                    showHidden=False,
                ).execute()
            except Exception:
                # Skip lists we can't read (permission weirdness, etc.).
                continue
            for item in resp.get("items") or []:
                task = self._to_source_task(item, list_id)
                if task is not None:
                    out.append(task)
        return out

    def health_check(self) -> dict:
        info = {
            "source": self.SOURCE,
            "credentials_path": str(self.credentials_path),
            "token_present": self.token_path.exists(),
            "list_ids": list(self.list_ids) if self.list_ids else None,
            "require_approval": self.require_approval,
        }
        if not self.credentials_path.exists():
            info["error"] = (
                f"credentials.json not found at {self.credentials_path}. "
                f"See docs/google-calendar-setup.md (same file Google "
                f"Calendar uses)."
            )
            return info
        if not self.token_path.exists():
            info["error"] = (
                "No Google refresh token yet. Run `python authorize_google.py` "
                "once — the same token covers Calendar, Gmail, and Tasks."
            )
            return info
        try:
            svc = self._svc()
            list_ids = self._resolve_list_ids(svc) or []
            total = 0
            for list_id in list_ids:
                try:
                    resp = svc.tasks().list(
                        tasklist=list_id, showCompleted=False, showHidden=False,
                    ).execute()
                    total += len(resp.get("items") or [])
                except Exception:
                    continue
            info["list_count"] = len(list_ids)
            info["total_tasks_visible"] = total
        except Exception as ex:
            info["error"] = f"{type(ex).__name__}: {ex}"
        return info

    # ---- Internals ----

    def _svc(self):
        if self._service is not None:
            return self._service
        creds = self._load_credentials()
        from googleapiclient.discovery import build
        self._service = build("tasks", "v1", credentials=creds,
                              cache_discovery=False)
        return self._service

    def _load_credentials(self):
        if not self.token_path.exists():
            raise RuntimeError(
                f"No Google OAuth token at {self.token_path}. "
                f"Run `python authorize_google.py` once to complete "
                f"authorization."
            )
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        creds = Credentials.from_authorized_user_file(
            str(self.token_path), self.scopes,
        )
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                self.token_path.write_text(creds.to_json())
            else:
                raise RuntimeError(
                    "Google token is invalid and can't be refreshed. "
                    "Delete the token file and re-run `python "
                    "authorize_google.py`."
                )
        return creds

    def _resolve_list_ids(self, svc) -> Optional[list[str]]:
        if self.list_ids is not None:
            return list(self.list_ids)
        try:
            resp = svc.tasklists().list().execute()
        except Exception:
            return None
        return [tl["id"] for tl in (resp.get("items") or []) if tl.get("id")]

    def _to_source_task(self, item: dict, list_id: str) -> Optional[SourceTask]:
        task_id = item.get("id")
        if not task_id:
            return None
        title = item.get("title") or "(untitled Google task)"
        deadline = _parse_due(item.get("due"))
        is_completed = (item.get("status") == "completed")
        return SourceTask(
            source=self.SOURCE,
            source_id=f"{list_id}::{task_id}",
            title=title,
            deadline_utc=deadline,
            is_completed=is_completed,
            notes=item.get("notes") or None,
            extra={"list_id": list_id},
        )


# ---- Module-private helpers ----

def _parse_due(raw: Optional[str]) -> Optional[datetime]:
    """Google Tasks `due` is ISO 8601 — e.g. "2026-04-25T00:00:00.000Z".

    It's date-only semantics (Google stores midnight UTC regardless of
    the user's timezone), but we keep whatever precision Google sent.
    """
    if not raw or not isinstance(raw, str):
        return None
    try:
        s = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
