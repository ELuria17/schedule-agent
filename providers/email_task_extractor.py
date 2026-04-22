"""Claude-based email-to-task extraction.

Shared by GmailScanner and OutlookMailScanner. Takes the subject, sender,
date, and body of a single email and returns zero or more task candidates
by asking Claude to pick out actionable items the *reader* needs to do.

Extraction is deliberately conservative — FYI mail, auto-notifications,
and social messages produce zero tasks. We cap body length to keep
Haiku cost and latency low, and we return plain dicts so scanners can
stamp their own `source` / `source_id` before upserting.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

# The Anthropic SDK is already a required dep (see requirements.txt). We
# import lazily inside the call so tests that fully mock the client don't
# need the package at import time.


_EXTRACTION_SYSTEM = """You read one email at a time and extract actionable tasks the READER of the email needs to do. Be conservative: FYI, marketing, automated notifications, social chit-chat, and things the SENDER is doing produce NO tasks.

Every task you return MUST have:
- title: short specific phrase starting with a verb (e.g. "Draft Q3 proposal", "Review Acme contract")
- duration_min: integer minutes (reasonable choices: 15, 30, 45, 60, 90, 120, 180)
- deadline_ts: ISO 8601 UTC ("2026-04-22T17:00:00Z") only if the email EXPLICITLY mentions a deadline or date/time. Otherwise null.
- priority: "asap" if due <24h from now, "high" if <72h, "medium" if <7d, "low" otherwise. Use "medium" if no deadline.

Return a compact JSON array. Empty array [] if no actionable tasks. Return ONLY the JSON — no explanation, no markdown fences."""


_EXTRACTION_USER_TEMPLATE = """Subject: {subject}
From: {sender}
Date: {date}
Current time: {now}

{body}"""


DEFAULT_MODEL = "claude-haiku-4-5-20251001"


def extract_tasks_from_email(
    *,
    subject: str,
    sender: str,
    date_iso: str,
    body: str,
    anthropic_client=None,
    model: str = DEFAULT_MODEL,
    body_char_cap: int = 8000,
    now_iso: Optional[str] = None,
) -> list[dict]:
    """Call Claude to extract tasks from a single email.

    Returns a list of dicts with keys: title, duration_min, deadline_ts,
    priority. Empty list on any failure (network, JSON parse, etc.) —
    scanners treat missing extractions as "skip this email" rather than
    a hard error.

    Pass your own `anthropic_client` in tests to avoid network calls; in
    production, leaving it None constructs one from the ambient env.
    """
    if anthropic_client is None:
        try:
            from anthropic import Anthropic
            anthropic_client = Anthropic()
        except Exception:
            return []

    body_trimmed = (body or "")[:body_char_cap]
    if now_iso is None:
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        msg = anthropic_client.messages.create(
            model=model,
            max_tokens=1024,
            system=_EXTRACTION_SYSTEM,
            messages=[{
                "role": "user",
                "content": _EXTRACTION_USER_TEMPLATE.format(
                    subject=(subject or "(no subject)").strip(),
                    sender=(sender or "(unknown)").strip(),
                    date=date_iso or "",
                    now=now_iso,
                    body=body_trimmed,
                ),
            }],
        )
    except Exception:
        return []

    # Concat every text block back into a single string.
    text_parts: list[str] = []
    for block in getattr(msg, "content", []) or []:
        t = getattr(block, "text", None)
        if t:
            text_parts.append(t)
    raw = "".join(text_parts).strip()

    # Tolerate markdown fences if Claude adds them.
    if raw.startswith("```"):
        lines = raw.split("\n")
        # drop first line (``` or ```json) and trailing ``` if present
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines).strip()

    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []

    if not isinstance(parsed, list):
        return []

    out: list[dict] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "")).strip()
        if not title:
            continue
        raw_duration = item.get("duration_min")
        if raw_duration is None:
            duration = 45
        else:
            try:
                duration = int(raw_duration)
            except (TypeError, ValueError):
                duration = 45
        duration = max(5, min(duration, 480))  # clamp 5min..8h
        deadline = item.get("deadline_ts")
        if deadline is not None and not isinstance(deadline, str):
            deadline = None
        priority = item.get("priority") or "medium"
        if priority not in ("asap", "high", "medium", "low"):
            priority = "medium"
        out.append({
            "title": title,
            "duration_min": duration,
            "deadline_ts": deadline,
            "priority": priority,
        })
    return out
