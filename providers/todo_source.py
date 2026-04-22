"""Provider-neutral to-do / reminder source.

Used for the "user checked something off outside the hub" signal.
Completed todos get matched against active tasks and mark them done.

Matching semantics live in the orchestrator, not here — the TodoSource just
returns raw items. See ROADBLOCKS §R4 for the handshake-table history that
prevents toggle loops with sources like Apple Reminders.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class TodoItem:
    id: str                                   # stable unique id within this source
    list_name: str                            # which list/project/folder it came from
    title: str
    completed: bool
    due_utc: Optional[datetime] = None
    completed_at_utc: Optional[datetime] = None
    notes: Optional[str] = None
    extra: dict = field(default_factory=dict)


class TodoSource(ABC):
    SOURCE: str = ""

    @abstractmethod
    def list_items(self, *, include_completed: bool = True) -> list[TodoItem]:
        """All todos visible to this source. Orchestrator filters to
        completed=True for cross-reference against Tasks."""

    def health_check(self) -> dict:
        return {"source": self.SOURCE or type(self).__name__}
