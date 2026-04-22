"""Gmail task source.

Pulls recent messages from the authenticated Google account, hands each
one to the Claude-based extractor (providers/email_task_extractor.py),
and emits a SourceTask per actionable item the reader needs to do.

Designed to share the OAuth stack with GoogleCalendarProvider — same
credentials.json, same google_token.json. The only requirement is that
the cached token was minted with the `gmail.readonly` scope, which is
part of `DEFAULT_SCOPES` in providers/google_calendar.py. Tokens
authorized before that scope was widened need a one-time re-auth
(delete google_token.json and re-run `python authorize_google.py`).

What this provider pulls:
- Up to `max_messages` recent messages matching the configured query.
- The default query is `newer_than:3d is:unread`; when `label_filter`
  is set, it switches to `label:<L1> OR label:<L2>` so you can scope to
  manually-tagged important mail.
- Each message's subject, sender, date, and body are fed to Claude once.
- Every extracted task dict becomes a SourceTask stamped with a stable
  `{message_id}::{index}` source_id so re-scanning the same email
  upserts idempotently.

What it does NOT pull:
- Thread context. Each message is extracted on its own.
- Attachments. We look at text/plain only (text/html fallback is
  intentionally skipped — the extractor expects prose, not DOM soup).
- Historical mail. Bounded by `newer_than:` so the first scan doesn't
  blow up your Anthropic bill. Widen the query if you really want to
  backfill.

Error boundary: a single bad message (malformed payload, network blip on
.get(), extractor exception) is swallowed so the batch continues. The
orchestrator never sees a half-scanned inbox crash its sync pass.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .email_task_extractor import extract_tasks_from_email
from .task_source import SourceTask, TaskSource


DEFAULT_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class GmailScanner(TaskSource):
    """Gmail TaskSource that extracts actionable items via Claude.

    Parameters
    ----------
    credentials_path: str | Path
        Path to the `credentials.json` file downloaded from Google Cloud
        Console. This is the SAME file GoogleCalendarProvider uses —
        point both providers at the same path. Must be a "Desktop" OAuth
        client.
    token_path: str | Path
        Where the refresh token is cached. Also shared with
        GoogleCalendarProvider. If the token was minted before
        `gmail.readonly` was added to DEFAULT_SCOPES, delete the file
        and re-run `python authorize_google.py` once.
    label_filter: optional list[str]
        If set, restrict the scan to messages carrying ANY of these Gmail
        labels (e.g. ["IMPORTANT", "TODO"]). Labels are matched with
        Gmail's `label:` query syntax; system labels use uppercase
        (IMPORTANT, STARRED) and user labels use their visible name. If
        None, the default query `newer_than:3d is:unread` is used.
    max_messages: int
        Cap per scan. Every message costs one Claude call, so this is a
        cost knob. Default 25 is enough for a quick review of today's
        unread mail without runaway spend.
    anthropic_client: optional
        Test seam. Pass a client with a `.messages.create(...)` method
        that returns the canned response shape used by the extractor.
        Production calls leave this None; the extractor then builds an
        Anthropic client from the ambient env (ANTHROPIC_API_KEY).
    require_approval: bool
        Forwarded to TaskSource base. When True, freshly-extracted tasks
        land in `pending_review` instead of going straight to the
        solver. Recommended for Gmail — LLM-extracted tasks have a
        higher false-positive rate than structured sources, and the
        review queue is the right place to filter them.
    """

    SOURCE = "gmail"

    def __init__(
        self,
        *,
        credentials_path: str | Path,
        token_path: str | Path,
        label_filter: Optional[list[str]] = None,
        max_messages: int = 25,
        anthropic_client=None,
        require_approval: bool = False,
    ):
        self.credentials_path = Path(credentials_path).expanduser()
        self.token_path = Path(token_path).expanduser()
        self.label_filter = list(label_filter) if label_filter else None
        self.max_messages = max_messages
        self.anthropic_client = anthropic_client
        self.require_approval = require_approval
        self.scopes = list(DEFAULT_SCOPES)
        self._service = None  # lazy

    # ---- TaskSource contract ----

    def list_active_tasks(self) -> list[SourceTask]:
        try:
            svc = self._svc()
        except Exception:
            return []

        query = self._build_query()
        try:
            resp = svc.users().messages().list(
                userId="me", q=query, maxResults=self.max_messages,
            ).execute()
        except Exception:
            return []

        message_ids = [m["id"] for m in (resp.get("messages") or []) if m.get("id")]
        out: list[SourceTask] = []
        for mid in message_ids:
            try:
                out.extend(self._scan_message(svc, mid))
            except Exception:
                # One broken email never kills the batch.
                continue
        return out

    def health_check(self) -> dict:
        info = {
            "source": self.SOURCE,
            "credentials_path": str(self.credentials_path),
            "token_present": self.token_path.exists(),
            "label_filter": list(self.label_filter) if self.label_filter else None,
            "max_messages": self.max_messages,
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
        return info

    # ---- Internals ----

    def _svc(self):
        if self._service is not None:
            return self._service
        creds = self._load_credentials()
        from googleapiclient.discovery import build
        self._service = build("gmail", "v1", credentials=creds,
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

    def _build_query(self) -> str:
        if self.label_filter:
            # Gmail search uses `label:X` — quote labels that contain
            # spaces so `label:"TODO list"` parses correctly.
            parts = []
            for lbl in self.label_filter:
                safe = lbl.replace('"', "")
                if any(ch.isspace() for ch in safe):
                    parts.append(f'label:"{safe}"')
                else:
                    parts.append(f"label:{safe}")
            return " OR ".join(parts)
        return "newer_than:3d is:unread"

    def _scan_message(self, svc, mid: str) -> list[SourceTask]:
        msg = svc.users().messages().get(
            userId="me", id=mid, format="full",
        ).execute()
        subject, sender, date_iso = _extract_headers(msg)
        body = _extract_body(msg)
        extracted = extract_tasks_from_email(
            subject=subject,
            sender=sender,
            date_iso=date_iso,
            body=body,
            anthropic_client=self.anthropic_client,
        )
        note_suffix = f"{subject} — from {sender}".strip(" —")
        out: list[SourceTask] = []
        for idx, item in enumerate(extracted):
            out.append(SourceTask(
                source=self.SOURCE,
                source_id=f"{mid}::{idx}",
                title=item["title"],
                deadline_utc=_parse_deadline(item.get("deadline_ts")),
                duration_hint_min=item.get("duration_min"),
                priority_hint=item.get("priority"),
                notes=note_suffix or None,
                extra={"message_id": mid, "extract_index": idx},
            ))
        return out


# ---- Module-private helpers ----

def _extract_headers(msg: dict) -> tuple[str, str, str]:
    """Return (subject, sender, date_iso) from a Gmail message dict."""
    headers = ((msg.get("payload") or {}).get("headers") or [])
    subject = sender = date_iso = ""
    for h in headers:
        name = (h.get("name") or "").lower()
        value = h.get("value") or ""
        if name == "subject" and not subject:
            subject = value
        elif name == "from" and not sender:
            sender = value
        elif name == "date" and not date_iso:
            date_iso = value
    return subject, sender, date_iso


def _extract_body(msg: dict) -> str:
    """Pull text/plain content out of a Gmail payload.

    Gmail returns MIME trees. We walk every part looking for text/plain
    and concatenate. If no parts exist (simple message), we decode
    payload.body.data directly. Returns "" on any decode failure —
    the extractor tolerates empty bodies.
    """
    payload = msg.get("payload") or {}
    parts = payload.get("parts")
    if parts:
        chunks: list[str] = []
        _walk_parts(parts, chunks)
        return "\n".join(c for c in chunks if c)
    # Non-multipart: the whole body lives on payload.body.data.
    data = (payload.get("body") or {}).get("data")
    return _decode_base64url(data)


def _walk_parts(parts: list, chunks: list[str]) -> None:
    for part in parts or []:
        mime = part.get("mimeType") or ""
        sub = part.get("parts")
        if sub:
            _walk_parts(sub, chunks)
            continue
        if mime == "text/plain":
            data = (part.get("body") or {}).get("data")
            text = _decode_base64url(data)
            if text:
                chunks.append(text)


def _decode_base64url(data: Optional[str]) -> str:
    if not data:
        return ""
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _parse_deadline(raw: Optional[str]) -> Optional[datetime]:
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
