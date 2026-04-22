"""Canvas LMS task source.

Works against any Canvas instance (not just psu.instructure.com). Configure
`base_url` to your school's `/api/v1` endpoint.

Encapsulates the Canvas-specific logic from ROADBLOCKS §C1-C3:
- Response-size trimming (Canvas returns full assignment descriptions that
  can balloon past 1 MB per course — we take only the fields we use).
- Relevance filter: due in [-3d, +21d]. Past-window assignments are hidden;
  far-future ones skipped until closer to the date.
- Course-code extraction from free-form course names
  (`"ECON104 - Dave Brown - SP26"` → `"ECON 104"`).
- Duration heuristics from assignment name + submission_types.
- has_submitted_submissions → is_completed; Canvas is authoritative.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

import requests

from .task_source import SourceTask, TaskSource


_FIELDS = (
    "id", "name", "due_at", "lock_at", "unlock_at", "time_limit",
    "points_possible", "submission_types", "has_submitted_submissions",
    "html_url",
)

_COURSE_CODE_RE = re.compile(r"([A-Z]{3,5})\s*(\d{3,4})")


class CanvasTaskSource(TaskSource):
    SOURCE = "canvas"

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        relevance_past_days: int = 3,
        relevance_future_days: int = 21,
        page_size: int = 100,
        timeout: int = 30,
        require_approval: bool = False,
    ):
        """
        base_url: e.g. "https://psu.instructure.com/api/v1" (no trailing slash).
        token: Canvas personal access token (profile/settings → "New Access Token").
        relevance_past_days / relevance_future_days: window bounds in days.
        require_approval: if True, new Canvas assignments land in pending_review
            rather than going straight onto the calendar. See TaskSource docs.
        """
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.past_days = relevance_past_days
        self.future_days = relevance_future_days
        self.page_size = page_size
        self.timeout = timeout
        self.require_approval = require_approval

    # ---- TaskSource ----

    def list_active_tasks(self) -> list[SourceTask]:
        out: list[SourceTask] = []
        try:
            courses = self._get("/courses", {"enrollment_state": "active",
                                             "per_page": self.page_size})
        except Exception:
            return out
        for course in courses:
            course_id = course.get("id")
            course_name = course.get("name") or ""
            course_code = self._extract_course_code(course_name)
            try:
                raw = self._get(
                    f"/courses/{course_id}/assignments",
                    {"per_page": self.page_size, "order_by": "due_at"},
                )
            except Exception:
                continue
            for a in raw:
                if not self._is_relevant(a):
                    continue
                out.append(self._to_source_task(a, course_id, course_code or course_name))
        return out

    def health_check(self) -> dict:
        info = {"source": self.SOURCE, "base_url": self.base_url,
                "require_approval": self.require_approval}
        try:
            profile = self._get("/users/self", {})
            info["canvas_user"] = profile.get("name")
        except Exception as ex:
            info["error"] = f"{type(ex).__name__}: {ex}"
        return info

    # ---- Helpers callable by the orchestrator when it wants details ----

    def get_assignment(self, course_id: int, assignment_id: int) -> dict:
        """Full assignment blob, used when the agent or hub wants the description."""
        return self._get(f"/courses/{course_id}/assignments/{assignment_id}", {})

    # ---- Internals ----

    def _get(self, path: str, params: dict) -> list | dict:
        r = requests.get(
            f"{self.base_url}{path}",
            headers={"Authorization": f"Bearer {self.token}"},
            params=params, timeout=self.timeout,
        )
        r.raise_for_status()
        return r.json()

    def _is_relevant(self, assignment: dict) -> bool:
        due = assignment.get("due_at")
        if not due:
            return False
        try:
            dt = datetime.fromisoformat(due.replace("Z", "+00:00"))
        except Exception:
            return False
        days = (dt - datetime.now(timezone.utc)).total_seconds() / 86400
        return -self.past_days <= days <= self.future_days

    def _to_source_task(self, a: dict, course_id, course_code: str) -> SourceTask:
        due = a.get("due_at")
        deadline_utc = None
        if due:
            try:
                deadline_utc = datetime.fromisoformat(due.replace("Z", "+00:00"))
            except Exception:
                deadline_utc = None
        trimmed = {k: a.get(k) for k in _FIELDS}
        trimmed["course_id"] = course_id
        return SourceTask(
            source=self.SOURCE,
            source_id=str(a["id"]),
            title=a.get("name") or "(untitled Canvas assignment)",
            course=course_code or None,
            deadline_utc=deadline_utc,
            is_completed=bool(a.get("has_submitted_submissions")),
            duration_hint_min=self._duration_hint(a),
            priority_hint=self._priority_hint(deadline_utc),
            notes=a.get("html_url"),
            extra=trimmed,
        )

    @staticmethod
    def _extract_course_code(course_name: str) -> Optional[str]:
        m = _COURSE_CODE_RE.search(course_name or "")
        return f"{m.group(1)} {m.group(2)}" if m else None

    @staticmethod
    def _duration_hint(a: dict) -> Optional[int]:
        """Rule-based baseline. Quiz `time_limit` wins; else heuristics on name."""
        tl = a.get("time_limit")
        if isinstance(tl, int) and tl > 0:
            return tl
        name = (a.get("name") or "").lower()
        subs = a.get("submission_types") or []
        if "online_quiz" in subs or "quiz" in subs or "quiz" in name:
            return 30
        if "discussion_topic" in subs or "discussion" in name:
            return 30
        if any(k in name for k in ("hw", "homework", "problem", "exercise", "lesson")):
            return 90
        if any(k in name for k in ("read", "chapter", "ch ", "ch.")):
            return 30
        if any(k in name for k in ("essay", "paper", "reflection", "post")):
            return 60
        if any(k in name for k in ("project", "lab")):
            return 120
        return 60

    @staticmethod
    def _priority_hint(deadline_utc: Optional[datetime]) -> Optional[str]:
        if deadline_utc is None:
            return None
        hours = (deadline_utc - datetime.now(timezone.utc)).total_seconds() / 3600
        if hours < 24:
            return "asap"
        if hours < 72:
            return "high"
        if hours < 168:
            return "medium"
        return "low"
