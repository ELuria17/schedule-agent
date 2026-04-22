"""End-to-end pipeline tests.

Simulates the full lifecycle of a task that enters the system from an
email/LMS scanner, goes through the pending-review queue, gets approved,
gets completed with a different actual duration than estimated, and
influences the next similar task's duration via the continuous-learning
loop.

Uses the in-memory SQLite DB fixture from conftest.py. No network, no
provider SDKs — we feed `upsert_from_source` synthetic SourceTasks
directly, which is what every real scanner ends up calling anyway.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from providers.task_source import SourceTask


def _mk_source_task(source: str, source_id: str, *, title: str,
                    course: str | None = None, duration_hint_min: int | None = None,
                    deadline_utc: datetime | None = None,
                    priority_hint: str | None = None) -> SourceTask:
    return SourceTask(
        source=source, source_id=source_id, title=title,
        course=course, duration_hint_min=duration_hint_min,
        deadline_utc=deadline_utc, priority_hint=priority_hint,
    )


def _now_utc():
    return datetime.now(timezone.utc)


# ---------- Scanner → hub ----------

class TestScannerToHub:
    def test_gmail_sourcetask_lands_in_scheduled_when_auto_mode(self, db):
        import tasks as tasks_mod
        st = _mk_source_task(
            "gmail", "msg-123::0",
            title="Finalize Acme proposal",
            duration_hint_min=90,
            deadline_utc=_now_utc() + timedelta(hours=48),
            priority_hint="high",
        )
        row = tasks_mod.upsert_from_source(st, require_approval=False)
        assert row["status"] == "scheduled"
        assert row["source"] == "gmail"
        assert row["source_id"] == "msg-123::0"
        # Priority hint carries through.
        assert row["priority"] == "high"

    def test_gmail_sourcetask_lands_in_pending_when_review_mode(self, db):
        import tasks as tasks_mod
        st = _mk_source_task(
            "gmail", "msg-456::0",
            title="Review Q3 forecast",
            duration_hint_min=60,
            deadline_utc=_now_utc() + timedelta(days=3),
        )
        row = tasks_mod.upsert_from_source(st, require_approval=True)
        assert row["status"] == "pending_review"

    def test_outlook_mail_sourcetask_lands_the_same_way(self, db):
        """Pipeline is source-agnostic — Outlook tasks behave like Gmail."""
        import tasks as tasks_mod
        st = _mk_source_task(
            "outlook_mail", "AAMkABC::0",
            title="Send contract to legal",
            duration_hint_min=15,
        )
        row = tasks_mod.upsert_from_source(st, require_approval=True)
        assert row["status"] == "pending_review"
        assert row["source"] == "outlook_mail"

    def test_mstodo_sourcetask_lands_the_same_way(self, db):
        import tasks as tasks_mod
        st = _mk_source_task(
            "microsoft_todo", "list-1::task-7",
            title="Book quarterly offsite venue",
            priority_hint="asap",
        )
        row = tasks_mod.upsert_from_source(st, require_approval=False)
        assert row["source"] == "microsoft_todo"
        assert row["priority"] == "asap"


# ---------- Hub approve / edit / reject ----------

class TestHubActions:
    def test_approve_moves_pending_to_scheduled(self, db):
        import tasks as tasks_mod
        st = _mk_source_task("gmail", "m1::0", title="Finalize prop",
                             duration_hint_min=60)
        row = tasks_mod.upsert_from_source(st, require_approval=True)
        assert row["status"] == "pending_review"

        approved = tasks_mod.approve(row["id"])
        assert approved["status"] == "scheduled"

    def test_reject_removes_or_hides_pending(self, db):
        import tasks as tasks_mod
        st = _mk_source_task("gmail", "m2::0", title="Spam-ish ask",
                             duration_hint_min=30)
        row = tasks_mod.upsert_from_source(st, require_approval=True)
        rejected = tasks_mod.reject(row["id"])
        # reject() can either drop the row or flip it to hidden; both are
        # acceptable — we just require it's gone from active/pending views.
        active_ids = [t["id"] for t in tasks_mod.list_active()]
        pending_ids = [t["id"] for t in tasks_mod.list_pending_review()]
        assert row["id"] not in active_ids
        assert row["id"] not in pending_ids
        if rejected is not None:
            assert rejected["status"] in ("hidden", "blocked")

    def test_edit_before_approve_preserves_user_changes(self, db):
        """User tweaks the proposed duration and title, then approves —
        the approval must not revert the edits."""
        import tasks as tasks_mod
        st = _mk_source_task("gmail", "m3::0",
                             title="Draft proposal (from email)",
                             duration_hint_min=120)
        row = tasks_mod.upsert_from_source(st, require_approval=True)
        edited = tasks_mod.update(
            row["id"],
            title="Draft Acme proposal",
            duration_min=90,
            priority="high",
        )
        assert edited["title"] == "Draft Acme proposal"
        assert edited["duration_min"] == 90
        assert edited["priority"] == "high"
        assert edited["duration_locked"] == 1
        approved = tasks_mod.approve(row["id"])
        assert approved["status"] == "scheduled"
        # Edits survive the approve.
        assert approved["title"] == "Draft Acme proposal"
        assert approved["duration_min"] == 90
        assert approved["duration_locked"] == 1


# ---------- Scanner dedup + authoritative-upstream ----------

class TestScannerDedup:
    def test_same_source_id_twice_only_creates_one_row(self, db):
        import tasks as tasks_mod
        st = _mk_source_task("gmail", "m1::0", title="x", duration_hint_min=30)
        r1 = tasks_mod.upsert_from_source(st, require_approval=False)
        r2 = tasks_mod.upsert_from_source(st, require_approval=False)
        assert r1["id"] == r2["id"]

    def test_upstream_completion_closes_local_task(self, db):
        """Mirrors orchestrator.sync_tasks_from_sources(): on each poll,
        active items hit upsert_from_source and completed items get
        routed through mark_done_by_source."""
        import tasks as tasks_mod
        opening = SourceTask(
            source="microsoft_todo", source_id="list-1::t-1",
            title="Send follow-up to Acme",
        )
        row = tasks_mod.upsert_from_source(opening, require_approval=False)
        assert row["status"] == "scheduled"
        # User ticks the task off in Microsoft To Do. On the next poll,
        # the orchestrator sees is_completed=True and routes it to
        # mark_done_by_source (see orchestrator.py:742).
        done = tasks_mod.mark_done_by_source("microsoft_todo", "list-1::t-1")
        assert done is not None
        assert done["status"] == "done"

    def test_existing_scheduled_task_not_demoted_to_pending(self, db):
        """If a user has already approved and the source re-emits the
        task, it must NOT be demoted back to pending_review."""
        import tasks as tasks_mod
        st = _mk_source_task("gmail", "m1::0", title="x", duration_hint_min=30)
        first = tasks_mod.upsert_from_source(st, require_approval=False)
        assert first["status"] == "scheduled"
        # Now re-emit with require_approval=True (say the user flipped
        # the global toggle between syncs).
        second = tasks_mod.upsert_from_source(st, require_approval=True)
        assert second["status"] == "scheduled"


# ---------- Learning loop ----------

class TestLearningLoop:
    def _complete_with_actual(self, tasks_mod, *, title, course, actual_min, source="gmail"):
        """Create + immediately complete a task with a specific actual_min,
        leaving a task_history row."""
        source_id = f"learn-{title}-{actual_min}"
        st = _mk_source_task(source, source_id, title=title, course=course,
                             duration_hint_min=60)
        row = tasks_mod.upsert_from_source(st, require_approval=False)
        tasks_mod.mark_complete(row["id"], actual_min=actual_min)

    def test_completed_task_writes_history_row(self, db):
        import tasks as tasks_mod
        self._complete_with_actual(tasks_mod, title="Draft proposal for Acme",
                                   course="Client", actual_min=45)
        with db._connect() as conn:
            rows = conn.execute("SELECT * FROM task_history").fetchall()
        assert len(rows) == 1
        assert rows[0]["actual_min"] == 45

    def test_relearn_adjusts_future_estimates_from_history(self, db):
        """Sequence: complete three similar tasks with actual_min=45,
        then a new scanner-sourced task arrives with duration_hint=90.
        `relearn_durations()` on the next solver pass should pull its
        estimate back toward the historical median."""
        import tasks as tasks_mod
        # Seed history
        for i, actual in enumerate([40, 45, 50]):
            self._complete_with_actual(
                tasks_mod,
                title=f"Draft proposal for Acme ({i})",
                course="Client",
                actual_min=actual,
            )
        # New active task with stale estimate.
        new_st = _mk_source_task(
            "gmail", "new-task-1",
            title="Draft proposal for Beta Corp",
            course="Client",
            duration_hint_min=90,
        )
        new_row = tasks_mod.upsert_from_source(new_st, require_approval=False)
        # At upsert, suggest_duration already adjusted to median=45.
        assert new_row["duration_min"] == 45
        # Now explicitly add more history that should shift median.
        for actual in (80, 82, 85):
            self._complete_with_actual(
                tasks_mod,
                title=f"Draft proposal for Gamma ({actual})",
                course="Client",
                actual_min=actual,
            )
        # Next plan cycle re-estimates.
        n_updated = tasks_mod.relearn_durations()
        assert n_updated >= 1
        after = tasks_mod.get(new_row["id"])
        # Median of [40,45,50,80,82,85] = 65 (average of 50 and 80).
        # suggest_duration returns the lower-middle element in even-sized sets.
        assert after["duration_min"] != 90
        assert 40 <= after["duration_min"] <= 90

    def test_user_edit_pins_duration_against_relearn(self, db):
        import tasks as tasks_mod
        # Seed history that would push estimate to ~45
        for actual in (40, 45, 50):
            self._complete_with_actual(
                tasks_mod,
                title=f"Draft proposal for seed {actual}",
                course="Client",
                actual_min=actual,
            )
        new_st = _mk_source_task(
            "gmail", "user-pin", title="Draft proposal for Beta",
            course="Client", duration_hint_min=120,
        )
        new_row = tasks_mod.upsert_from_source(new_st, require_approval=False)
        # User overrides to 180 via the hub modal.
        tasks_mod.update(new_row["id"], duration_min=180)
        tasks_mod.relearn_durations()
        after = tasks_mod.get(new_row["id"])
        assert after["duration_min"] == 180
        assert after["duration_locked"] == 1

    def test_manual_tasks_untouched_by_relearn(self, db):
        import tasks as tasks_mod
        for actual in (40, 45, 50):
            self._complete_with_actual(
                tasks_mod,
                title=f"Draft proposal (seed {actual})",
                course="Client",
                actual_min=actual,
            )
        # Manual task — user created via the hub's quick-add.
        manual = tasks_mod.create(
            title="Draft proposal for my side project",
            duration_min=120,
            course="Client",
        )
        tasks_mod.relearn_durations()
        after = tasks_mod.get(manual["id"])
        assert after["duration_min"] == 120, (
            "Manual tasks must not be rewritten by the learning loop"
        )


# ---------- Approval policy snapshot test ----------

class TestApprovalInteraction:
    def test_full_review_pipeline_from_scanner(self, db):
        """A Gmail-scanned task under review mode should:
        1. land in pending_review,
        2. be editable without losing the edits,
        3. flip to scheduled on approve.
        """
        import tasks as tasks_mod
        st = _mk_source_task("gmail", "full::0",
                             title="Proofread client SOW",
                             duration_hint_min=30,
                             priority_hint="medium")
        row = tasks_mod.upsert_from_source(st, require_approval=True)
        assert row["status"] == "pending_review"
        # Edit
        tasks_mod.update(row["id"], duration_min=45, priority="high",
                         title="Proofread and send Acme SOW")
        # Approve
        approved = tasks_mod.approve(row["id"])
        assert approved["status"] == "scheduled"
        assert approved["title"] == "Proofread and send Acme SOW"
        assert approved["duration_min"] == 45
        assert approved["priority"] == "high"
