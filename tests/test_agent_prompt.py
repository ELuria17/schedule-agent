"""Regression tests for the agent SYSTEM prompt + TOOLS schema in setup.py.

ROADBLOCKS Open Follow-up #9: every `agents.update()` risks regressing
behavior. There is no automated check that the agent still respects class
times, duration rules, etc. This file is that check — it asserts the
load-bearing contracts in setup.py.SYSTEM and the tool registry, so a
careless edit (e.g., dropping a hard rule, deleting a tool, removing the
asap/high/medium/low priority enum) trips a red test before the agent
ships to a real session.

These are *invariant* tests, not literal-string snapshots. Reword freely;
just keep the meaning.
"""
from __future__ import annotations

import pytest

import setup as setup_mod
from setup import SYSTEM, TOOLS


# ---------- SYSTEM prompt invariants ----------

class TestSystemInvariants:
    def test_no_personally_identifiable_setup_left(self):
        """The public fork must stay generic — no leftover 'Eytan' / 'Penn State'
        / 'Canvas' / 'iMessage' references."""
        for needle in ("Eytan", "Penn State", "Canvas", "iMessage", "iCloud"):
            assert needle not in SYSTEM, (
                f"SYSTEM prompt mentions {needle!r} — that's a leak from the "
                "private fork; replace with a provider-neutral phrasing."
            )

    def test_class_time_respect_is_implicit_via_solver_ownership(self):
        """The solver owns calendar placement (so it implicitly enforces class
        blocks). The prompt must say so — without that line the agent might
        try to schedule on top of classes."""
        assert "deterministic constraint solver" in SYSTEM
        assert ("CANNOT write to the calendar" in SYSTEM
                or "no calendar tools" in SYSTEM.lower())

    def test_priority_tiers_are_documented(self):
        # Every tier the solver understands must be explained for the agent.
        for tier in ("asap", "high", "medium", "low"):
            assert tier in SYSTEM, f"priority tier '{tier}' missing from prompt"

    def test_duration_baselines_present(self):
        """The Motion-style rebuild's baselines kept the LLM from inflating
        durations (ROADBLOCKS §A2). Don't drop them."""
        for needle in ("Reading:", "Problem set:", "Timed quiz", "Essay"):
            assert needle in SYSTEM, f"duration baseline '{needle}' missing"

    def test_quiz_duration_must_match_stated_limit(self):
        # ROADBLOCKS §A2 — first-pass agent inflated quiz durations. The fix
        # was an explicit "MATCH the stated time limit exactly" rule.
        assert "MATCH the stated time limit" in SYSTEM

    def test_notification_specificity_rule_present(self):
        # ROADBLOCKS §M? — generic SMS like "Study 4pm" was a regression we
        # codified into a hard rule. Keep the rule.
        assert "NEVER generic" in SYSTEM
        assert "ALWAYS specific" in SYSTEM

    def test_iso_timezone_template_is_filled(self):
        # The {TIMEZONE} f-string must be substituted, not left as a literal.
        assert "{TIMEZONE}" not in SYSTEM
        assert "User timezone:" in SYSTEM

    def test_solver_replans_after_mutations(self):
        # The agent shouldn't manually call schedule_query after every
        # task_create — the prompt promises auto-replan.
        assert "automatically re-plans" in SYSTEM or "automatically" in SYSTEM


# ---------- Tool schema invariants ----------

def _tool(name: str) -> dict:
    for t in TOOLS:
        if t.get("name") == name:
            return t
    pytest.fail(f"tool {name!r} missing from TOOLS")


class TestToolSchema:
    def test_required_tools_present(self):
        names = {t.get("name") for t in TOOLS if t.get("type") == "custom"}
        assert {"task_create", "task_update", "task_complete",
                "task_list", "schedule_query", "send_sms"}.issubset(names)

    def test_no_calendar_tool_exists(self):
        # ROADBLOCKS §A2 test signal: agent's tool list must contain ZERO
        # calendar_* tools. The deterministic solver writes the calendar.
        names = [t.get("name") for t in TOOLS if t.get("type") == "custom"]
        assert all(not (n or "").startswith("calendar_") for n in names), (
            f"calendar_ tool present in agent schema: {names}"
        )

    def test_task_create_required_fields(self):
        schema = _tool("task_create")["input_schema"]
        assert set(schema["required"]) == {"title", "duration_min"}

    def test_task_create_priority_enum_matches_solver(self):
        # The solver only knows these four tiers (priority_score's
        # _PRIORITY_WEIGHT). If the agent sends something outside this set
        # the schema rejects it — keeping the enums in lock-step matters.
        from solver import _PRIORITY_WEIGHT
        schema = _tool("task_create")["input_schema"]
        prio = schema["properties"]["priority"]["enum"]
        assert set(prio) == set(_PRIORITY_WEIGHT.keys())

    def test_preferred_window_enum_matches_solver(self):
        # Soft-bonus enforcement (ROADBLOCKS Open Follow-up #3) only handles
        # these three bands — adding a fourth here without updating
        # _PREFERRED_WINDOW_RANGES would silently degrade to "no preference".
        from solver import _PREFERRED_WINDOW_RANGES
        schema = _tool("task_create")["input_schema"]
        pw = schema["properties"]["preferred_window"]["enum"]
        assert set(pw) == set(_PREFERRED_WINDOW_RANGES.keys())

    def test_task_complete_actual_min_documented(self):
        # The actual_min path feeds task_history → relearn_durations().
        # If you remove actual_min, learning stops working.
        schema = _tool("task_complete")["input_schema"]
        assert "actual_min" in schema["properties"]

    def test_schedule_query_requires_start_and_end(self):
        schema = _tool("schedule_query")["input_schema"]
        assert set(schema["required"]) == {"start", "end"}

    def test_send_sms_required(self):
        schema = _tool("send_sms")["input_schema"]
        assert schema["required"] == ["body"]

    def test_managed_agents_toolset_enabled(self):
        """The built-in toolset (web search, etc.) is what lets the agent
        look up class syllabi or due dates the LMS doesn't expose. Keep it
        enabled — disabling it silently drops a lot of capability."""
        toolset = next((t for t in TOOLS
                        if t.get("type") == "agent_toolset_20260401"), None)
        assert toolset is not None
        assert toolset["default_config"]["enabled"] is True
