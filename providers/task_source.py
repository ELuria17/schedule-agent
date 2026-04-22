"""Provider-neutral task source interface.

A TaskSource is a system that exposes a list of work items — Canvas for
students, Brightspace, Moodle, a Notion database, Linear issues, GitHub
issues, or a plain CSV. The orchestrator periodically imports from every
configured source into the local SQLite tasks table.

Contract is intentionally small. Each source normalizes its upstream shape
into `SourceTask` values; `orchestrator.sync_tasks_from_sources()` upserts
them. The same mechanism that reopens a Canvas task when Canvas says "still
unsubmitted" (see ROADBLOCKS §D2) applies uniformly — sources are
authoritative over their own items.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class SourceTask:
    """Normalized shape the orchestrator understands.

    `source` + `source_id` together form the upsert key. `course` is
    optional but drives the reminder-match course mapping — if a source
    emits tasks without a course concept, leave it None.

    `is_completed` is authoritative: if True, the orchestrator closes the
    local task; if False, it reopens any locally-done task with the same
    source key (Canvas-authoritative pattern).
    """
    source: str                               # provider identifier: "canvas", "notion", etc.
    source_id: str                            # stable id within that provider
    title: str
    course: Optional[str] = None              # course code or project name
    deadline_utc: Optional[datetime] = None
    is_completed: bool = False                # upstream says done
    duration_hint_min: Optional[int] = None   # source's best guess at time required
    priority_hint: Optional[str] = None       # 'asap'|'high'|'medium'|'low' or None
    notes: Optional[str] = None               # URL, description, etc.
    extra: dict = field(default_factory=dict)


class TaskSource(ABC):
    """One system that emits tasks. Implementations: CanvasTaskSource,
    NotionTaskSource, LinearTaskSource, etc.

    The provider identifier (e.g., "canvas") should match the `source` field
    on every SourceTask it emits. That key is unique to a source *type*, not
    an instance — if a user wires two Canvas domains, both should use
    `source="canvas"` with distinct `source_id`s (e.g., domain-prefixed) to
    stay globally unique.

    `require_approval` (default False) opts this source into a review gate:
    new tasks upserted from it land in `pending_review` instead of
    `scheduled`, so nothing reaches the calendar until the user approves
    them via the hub. Existing (already-approved) rows are not demoted.
    Flip it on per-source when you want to see what the source wants to
    schedule before it lands on your calendar — e.g. a noisy LMS or an
    LLM-driven intake channel.
    """

    #: Short lowercase identifier written into SourceTask.source.
    SOURCE: str = ""
    #: If True, new tasks from this source start in pending_review.
    require_approval: bool = False

    @abstractmethod
    def list_active_tasks(self) -> list[SourceTask]:
        """Return every relevant task this source knows about. "Relevant"
        is source-defined — Canvas uses a -3d..+21d due-date window, a
        Linear source might use workspace+state filters, etc.

        Must be safe to call repeatedly; orchestrator calls this on every
        solver resolve.
        """

    def health_check(self) -> dict:
        return {"source": self.SOURCE or type(self).__name__,
                "require_approval": self.require_approval}
