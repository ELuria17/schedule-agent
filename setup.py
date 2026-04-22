"""
One-time setup: creates the environment + the schedule-agent Managed Agent
with the Motion-style system prompt and task-centric tool schema.

After the first run this script is not needed — subsequent changes to the
agent go through `agents.update()` in orchestrator code paths. Kept here as
the source of truth for "what does a fresh agent look like".
"""
import os
from anthropic import Anthropic
from dotenv import set_key, load_dotenv

from paths import env_path

# Use the resolved .env location — matters for the packaged app where
# .env lives in Application Support, not next to the binary.
_ENV_FILE = env_path()
load_dotenv(_ENV_FILE)
client = Anthropic()

TIMEZONE = os.environ.get("TIMEZONE", "America/New_York")

SYSTEM = f"""You are a personal task intake and scheduling assistant. A deterministic constraint solver owns calendar placement. Your only job is to keep the Task list accurate and to help the user understand their plan.

RESPONSIBILITIES
1. Morning kickoff: call schedule_query(today_start, tomorrow_end, include_at_risk=true), then send_sms with a concise summary. Include at-risk items prominently.
2. When the user mentions new work ("I have an essay due Friday"), call task_create with best-effort duration/priority/deadline.
3. When the user says something is done ("finished the lab report"), call task_complete.
4. Questions about the schedule -> call schedule_query and answer.
5. Configured task sources (LMS, issue tracker, etc.) and todo apps are synced automatically on every solver run. You do NOT need to import them.

HARD RULES
- You CANNOT write to the calendar directly. No calendar tools exist in your schema. The solver writes Study Block events to the configured calendar from Tasks.
- After task_create/task_update/task_complete, the solver automatically re-plans. The returned "resolve" field tells you chunk/at-risk counts.
- Notifications: keep under 320 chars when possible. Short lines, clear times, named tasks. NEVER generic ("Study 4pm"); ALWAYS specific ("4pm Ch. 8 problem set").
- Working hours come from the solver config; you don't manage them.

PRIORITY DEFAULTS (use unless the user overrides)
- asap: due <24h
- high: due <72h
- medium: due <7d
- low: due >7d

DURATION BASELINES (use when no history exists; otherwise task_history refines)
- Reading: 20-30 min per chapter.
- Problem set: 3-5 min per problem; cap at 90 min for one block.
- Timed quiz/exam: MATCH the stated time limit exactly. Separate prep block if review needed.
- Short written response / discussion post: 20-30 min.
- Essay/paper: ~15 min per 100 words.
- Lab/project/larger deliverable: 90-180 min.

All timestamps in UTC ISO 8601 with 'Z' suffix. User timezone: {TIMEZONE}.
"""


TOOLS = [
    {"type": "agent_toolset_20260401", "default_config": {"enabled": True}},
    {
        "type": "custom",
        "name": "task_create",
        "description": "Create a new Task for the solver to schedule. The solver re-plans automatically after creation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Specific task name — include any identifiers (course code, ticket id, chapter/quiz number) that help disambiguate."},
                "duration_min": {"type": "integer", "description": "Estimated minutes to complete."},
                "deadline_ts": {"type": "string", "description": "ISO 8601 UTC deadline; omit for open-ended tasks."},
                "priority": {"type": "string", "enum": ["asap", "high", "medium", "low"]},
                "course": {"type": "string", "description": "Optional grouping label for the task (e.g. a course code like 'ECON 104', a project name, or a client)."},
                "min_chunk_min": {"type": "integer", "description": "Smallest usable chunk (default 30)."},
                "max_chunk_min": {"type": "integer", "description": "Largest chunk (default 120)."},
                "preferred_window": {"type": "string", "enum": ["morning", "afternoon", "evening"]},
                "notes": {"type": "string"},
            },
            "required": ["title", "duration_min"],
        },
    },
    {
        "type": "custom",
        "name": "task_update",
        "description": "Update one or more fields of an existing task by id. Re-plans automatically.",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "title": {"type": "string"},
                "duration_min": {"type": "integer"},
                "deadline_ts": {"type": "string"},
                "priority": {"type": "string", "enum": ["asap", "high", "medium", "low"]},
                "course": {"type": "string"},
                "status": {"type": "string", "enum": ["scheduled", "in_progress", "done", "blocked", "hidden"]},
                "min_chunk_min": {"type": "integer"},
                "max_chunk_min": {"type": "integer"},
                "preferred_window": {"type": "string", "enum": ["morning", "afternoon", "evening"]},
                "notes": {"type": "string"},
            },
            "required": ["id"],
        },
    },
    {
        "type": "custom",
        "name": "task_complete",
        "description": "Mark a task done. Optional actual_min refines the solver's future duration estimates. Re-plans automatically.",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "actual_min": {"type": "integer", "description": "How long it actually took. Recorded to task_history for learning."},
            },
            "required": ["id"],
        },
    },
    {
        "type": "custom",
        "name": "task_list",
        "description": "List tasks. By default returns active (not-done, not-hidden) tasks.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status_filter": {
                    "description": "Single status or list of statuses to filter by.",
                    "oneOf": [
                        {"type": "string", "enum": ["scheduled", "in_progress", "done", "blocked", "hidden"]},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                },
                "limit": {"type": "integer", "default": 50},
            },
        },
    },
    {
        "type": "custom",
        "name": "schedule_query",
        "description": "Return solver-placed chunks in [start, end) with optional at-risk list. Use for morning summaries and 'what's today?' questions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "start": {"type": "string", "description": "ISO 8601 UTC"},
                "end": {"type": "string", "description": "ISO 8601 UTC"},
                "include_at_risk": {"type": "boolean", "default": True},
            },
            "required": ["start", "end"],
        },
    },
    {
        "type": "custom",
        "name": "send_sms",
        "description": "Send a notification to the user via the configured Notifier (iMessage, ntfy.sh, email, etc.). Keep under 320 chars when possible.",
        "input_schema": {
            "type": "object",
            "properties": {"body": {"type": "string"}},
            "required": ["body"],
        },
    },
]


def main():
    env = client.beta.environments.create(
        name="schedule-agent-env",
        config={"type": "cloud", "networking": {"type": "unrestricted"}},
    )
    agent = client.beta.agents.create(
        name="Schedule Planner",
        model="claude-sonnet-4-6",
        system=SYSTEM,
        tools=TOOLS,
    )
    # Ensure the file exists so set_key has a target to write into.
    _ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    _ENV_FILE.touch(exist_ok=True)
    set_key(str(_ENV_FILE), "ENVIRONMENT_ID", env.id)
    set_key(str(_ENV_FILE), "AGENT_ID", agent.id)
    print(f"Environment: {env.id}\nAgent: {agent.id}")


if __name__ == "__main__":
    main()
