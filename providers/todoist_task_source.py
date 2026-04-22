"""Todoist task source.

Works against the Todoist REST API v2. The user provides a personal
access token from the Todoist web app (no OAuth flow — token-based).

What this provider pulls:
- Every active (not-completed) task from your Todoist account, optionally
  filtered to a subset of projects.
- Each task's due date / datetime (if set) → SourceTask.deadline_utc.
- Todoist priority 4/3/2/1 → 'asap'/'high'/'medium'/'low'.
- Task description → SourceTask.notes (empty if unused).

What it does NOT pull:
- Completed tasks. Todoist hides them from the REST endpoint; catching
  "user completed this in Todoist" would require the sync API + webhook
  plumbing. Not worth the complexity for v1.
- Recurring-task instances. Each visible instance is a normal task; we
  treat it as-is.

Token: generate at https://app.todoist.com/app/settings/integrations/developer
(Settings → Integrations → Developer → "Copy API token").
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import requests

from .task_source import SourceTask, TaskSource


_BASE_URL = "https://api.todoist.com/rest/v2"

# Todoist priority is 1 (default) .. 4 (urgent). Map onto our tiers.
_PRIORITY_MAP = {4: "asap", 3: "high", 2: "medium", 1: "low"}


class TodoistTaskSource(TaskSource):
    SOURCE = "todoist"

    def __init__(
        self,
        *,
        token: str,
        project_ids: Optional[list[str]] = None,
        label_filter: Optional[list[str]] = None,
        default_duration_min: int = 45,
        timeout: int = 30,
        require_approval: bool = False,
    ):
        """
        token: Todoist personal access token.
        project_ids: if provided, only tasks in these projects are ingested.
            Project IDs are the strings from `GET /projects`. If None, all
            active tasks are included.
        label_filter: if provided, only tasks that carry at least one of
            these label names are ingested. Useful if a single project
            mixes AutoPlan-scheduled work with unrelated items.
        default_duration_min: Todoist has no built-in duration field, so
            every imported task lands with this estimate. Users can edit
            per-task in the hub.
        require_approval: if True, new tasks land in the pending-review
            queue instead of going straight onto the calendar.
        """
        self.token = token
        self.project_ids = set(project_ids) if project_ids else None
        self.label_filter = set(label_filter) if label_filter else None
        self.default_duration_min = default_duration_min
        self.timeout = timeout
        self.require_approval = require_approval

    # ---- TaskSource contract ----

    def list_active_tasks(self) -> list[SourceTask]:
        try:
            raw = self._get("/tasks", params={})
        except Exception:
            return []
        out: list[SourceTask] = []
        for item in raw:
            if not self._passes_filter(item):
                continue
            out.append(self._to_source_task(item))
        return out

    def health_check(self) -> dict:
        info = {
            "source": self.SOURCE,
            "require_approval": self.require_approval,
            "project_filter": sorted(self.project_ids) if self.project_ids else "all",
            "label_filter": sorted(self.label_filter) if self.label_filter else "all",
        }
        try:
            r = requests.get(
                f"{_BASE_URL}/projects",
                headers=self._auth_header(),
                timeout=self.timeout,
            )
            r.raise_for_status()
            info["projects_visible"] = len(r.json())
        except Exception as ex:
            info["error"] = f"{type(ex).__name__}: {ex}"
        return info

    # ---- Internals ----

    def _auth_header(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def _get(self, path: str, params: dict) -> list | dict:
        r = requests.get(
            f"{_BASE_URL}{path}",
            headers=self._auth_header(),
            params=params, timeout=self.timeout,
        )
        r.raise_for_status()
        return r.json()

    def _passes_filter(self, item: dict) -> bool:
        if self.project_ids is not None:
            if str(item.get("project_id", "")) not in self.project_ids:
                return False
        if self.label_filter is not None:
            labels = set(item.get("labels") or [])
            if not (labels & self.label_filter):
                return False
        return True

    def _to_source_task(self, t: dict) -> SourceTask:
        deadline_utc = self._parse_due(t.get("due"))
        priority_hint = _PRIORITY_MAP.get(int(t.get("priority") or 1))

        labels = list(t.get("labels") or [])
        # If the user tagged one label "course-like" (e.g. "ECON104"),
        # surface it as the course field so the reminder-match heuristic
        # has something to anchor on. First label wins.
        course = labels[0] if labels else None

        return SourceTask(
            source=self.SOURCE,
            source_id=str(t["id"]),
            title=t.get("content") or "(untitled Todoist task)",
            course=course,
            deadline_utc=deadline_utc,
            is_completed=bool(t.get("is_completed") or t.get("completed")),
            duration_hint_min=self.default_duration_min,
            priority_hint=priority_hint,
            notes=t.get("description") or None,
            extra={"url": t.get("url"), "project_id": t.get("project_id"),
                   "labels": labels},
        )

    @staticmethod
    def _parse_due(due: Optional[dict]) -> Optional[datetime]:
        """Todoist returns a due dict like
            {"date": "2026-04-25", "datetime": "2026-04-25T23:59:00Z", "string": "..."}
        where `datetime` is only present when the user picked a time.
        """
        if not due:
            return None
        # Prefer the full datetime when available.
        dt_str = due.get("datetime") or due.get("date")
        if not dt_str:
            return None
        try:
            dt = datetime.fromisoformat(str(dt_str).replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            # Date-only — treat as end-of-day UTC so the solver still
            # respects the deadline instead of placing past it.
            dt = dt.replace(hour=23, minute=59, second=0, tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
