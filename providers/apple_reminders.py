"""Apple Reminders todo source (macOS only).

Shells out to the compiled Swift binary `macos/reminders_fetch` (source at
`macos/reminders_fetch.swift`). The binary uses EventKit predicates and
returns sub-second results even on ~1000 reminders (JXA took minutes — see
ROADBLOCKS §R2).

Requires:
- macOS (EventKit).
- TCC permission for the binary to read Reminders. Granted on first run
  via a system prompt. See ROADBLOCKS §R3.
- `cd macos && swiftc -O -o reminders_fetch reminders_fetch.swift` before
  first use.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import field
from datetime import datetime, timezone
from typing import Optional

from .todo_source import TodoItem, TodoSource


class AppleRemindersTodoSource(TodoSource):
    SOURCE = "apple_reminders"

    def __init__(self, *, binary_path: str, timeout: int = 30):
        self.binary_path = binary_path
        self.timeout = timeout

    def list_items(self, *, include_completed: bool = True) -> list[TodoItem]:
        """Swift binary returns both incomplete + recently-completed (last 7 days).
        `include_completed=False` filters out completed rows client-side."""
        try:
            r = subprocess.run(
                [self.binary_path],
                capture_output=True, text=True, timeout=self.timeout,
            )
        except Exception:
            return []
        if r.returncode != 0:
            return []
        try:
            raw = json.loads(r.stdout) or []
        except json.JSONDecodeError:
            return []
        out: list[TodoItem] = []
        for row in raw:
            if not include_completed and row.get("completed"):
                continue
            out.append(TodoItem(
                id=str(row.get("uid") or ""),
                list_name=row.get("list") or "",
                title=row.get("title") or "",
                completed=bool(row.get("completed")),
                due_utc=_parse_iso(row.get("due")),
                completed_at_utc=_parse_iso(row.get("completed_at")),
                notes=row.get("notes"),
                extra={},
            ))
        return out

    def health_check(self) -> dict:
        info = {"source": self.SOURCE, "binary": self.binary_path}
        try:
            r = subprocess.run([self.binary_path], capture_output=True,
                               text=True, timeout=self.timeout)
            info["exit_code"] = r.returncode
            if r.returncode == 0:
                data = json.loads(r.stdout or "[]")
                info["total_reminders_surfaced"] = len(data)
        except Exception as ex:
            info["error"] = f"{type(ex).__name__}: {ex}"
        return info


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None
