"""Outlook / Microsoft 365 mail-to-task scanner.

Pulls recent unread messages from the user's Outlook mailbox via
Microsoft Graph, hands each one to the shared Claude-based email
task extractor (providers/email_task_extractor.py), and emits
normalized SourceTask rows. Mirrors the GmailScanner design —
mailbox + Claude are the substitutable moving parts; the surface
a TaskSource must expose stays identical.

Scope: we read one folder at a time (`inbox` by default). A user
who files actionable mail into a specific folder ("Todos",
"Follow up") can point `folder` at that folder's id/name. If you
also organize with Outlook categories (the coloured label dots),
`category_filter` narrows to messages tagged with any of those
categories — handy when a single folder mixes todo mail with
unrelated noise.

Why unread-only by default: once a user marks a message read, we
treat that as "I've dealt with this" — re-extracting tasks off
read mail would produce duplicates and stale reminders. The
extractor's source_id is `{msg_id}::{idx}`, so we're also upsert-
safe across scans, but unread-only keeps the LLM bill lower.

No category_filter, no folder override, no special prompting: we
delegate every word of extraction to email_task_extractor so
Outlook + Gmail produce consistent task titles / priorities.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import microsoft_graph_auth as mg
from .email_task_extractor import extract_tasks_from_email
from .task_source import SourceTask, TaskSource


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


class OutlookMailScanner(TaskSource):
    """Scan Outlook mail and emit one SourceTask per extracted task.

    Parameters
    ----------
    client_id : str
        Azure AD app Application (client) ID. Same app registration
        shared with OutlookCalendarProvider — one MSAL token cache
        covers all Microsoft providers.
    tenant : str, default "common"
        Azure tenant to authenticate against. See the calendar
        provider for tenant semantics.
    token_path : Path, optional
        MSAL token cache path. Defaults to
        `paths.microsoft_token_path()`.
    folder : str, default "inbox"
        Mail folder to scan. Accepts well-known Outlook folder ids
        ("inbox", "drafts", "sentitems", "junkemail") or a specific
        mailFolder id (the opaque base64-ish string Graph returns
        from `/me/mailFolders`). Users who file todos into a
        dedicated folder can point this at that folder's id.
    category_filter : list[str], optional
        If set, only messages carrying at least one of these
        Outlook categories (the coloured-dot labels) are scanned.
        None (default) scans every unread message in the folder.
    max_messages : int, default 25
        Cap on messages pulled per scan. Keeps token usage
        bounded — the orchestrator calls list_active_tasks on every
        solver resolve, so an unbounded inbox would be expensive.
    require_approval : bool, default False
        If True, new tasks land in pending_review instead of
        scheduled. Recommended for this source — LLM extractions
        occasionally surface noise, and a quick human tap accepts
        or rejects each one before it hits the calendar.
    anthropic_client : Anthropic | None
        Test seam. Passed through to extract_tasks_from_email so
        tests can inject a fake client and never touch the network.
        In production, leave None — the extractor builds one from
        ambient env (ANTHROPIC_API_KEY).
    """

    SOURCE = "outlook_mail"

    def __init__(
        self,
        *,
        client_id: str,
        tenant: str = "common",
        token_path: Optional[Path] = None,
        folder: str = "inbox",
        category_filter: Optional[list[str]] = None,
        max_messages: int = 25,
        require_approval: bool = False,
        anthropic_client=None,
    ):
        self.client_id = client_id
        self.tenant = tenant
        if token_path is None:
            from paths import microsoft_token_path
            token_path = microsoft_token_path()
        self.token_path = Path(token_path).expanduser()
        self.folder = folder
        self.category_filter = list(category_filter) if category_filter else None
        self.max_messages = max_messages
        self.require_approval = require_approval
        self.anthropic_client = anthropic_client

    # ---- TaskSource contract ----

    def list_active_tasks(self) -> list[SourceTask]:
        try:
            token = mg.get_access_token(
                client_id=self.client_id,
                tenant=self.tenant,
                token_path=self.token_path,
            )
        except Exception:
            return []

        # Build $filter. Graph uses OData:
        #   isRead eq false
        #   AND any(categories/any(c: c eq 'TODO'))
        odata_filter = "isRead eq false"
        if self.category_filter:
            clauses = [f"categories/any(c:c eq '{_odata_escape(c)}')"
                       for c in self.category_filter]
            if clauses:
                odata_filter = f"{odata_filter} and ({' or '.join(clauses)})"

        path = f"/me/mailFolders/{self.folder}/messages"
        params = {
            "$top": str(int(self.max_messages)),
            "$filter": odata_filter,
            "$select": "id,subject,from,receivedDateTime,body,categories",
            "$orderby": "receivedDateTime desc",
        }
        try:
            resp = mg.graph_get(token, path, params=params)
        except Exception:
            return []

        out: list[SourceTask] = []
        for msg in resp.get("value", []) or []:
            try:
                out.extend(self._extract_from_message(msg))
            except Exception:
                # One bad message shouldn't crash the batch.
                continue
        return out

    def health_check(self) -> dict:
        info = {
            "source": self.SOURCE,
            "require_approval": self.require_approval,
            "client_id": self.client_id,
            "tenant": self.tenant,
            "folder": self.folder,
            "category_filter": self.category_filter or "all",
            "max_messages": self.max_messages,
        }
        status = mg.token_status(token_path=self.token_path)
        info["token_present"] = status["token_present"]
        info["accounts"] = status["accounts"]
        if not status["token_present"]:
            info["error"] = (
                "No Microsoft token yet. Run "
                "`python authorize_microsoft.py` once to complete sign-in."
            )
        return info

    # ---- Internals ----

    def _extract_from_message(self, msg: dict) -> list[SourceTask]:
        msg_id = msg.get("id")
        if not msg_id:
            return []
        subject = msg.get("subject") or ""
        sender = ""
        frm = msg.get("from") or {}
        if isinstance(frm, dict):
            ea = frm.get("emailAddress") or {}
            sender = ea.get("address") or ea.get("name") or ""
        received = msg.get("receivedDateTime") or ""
        body = msg.get("body") or {}
        content_type = (body.get("contentType") or "").lower()
        body_text = body.get("content") or ""
        if content_type == "html":
            body_text = _strip_html(body_text)

        tasks = extract_tasks_from_email(
            subject=subject,
            sender=sender,
            date_iso=received,
            body=body_text,
            anthropic_client=self.anthropic_client,
        )
        out: list[SourceTask] = []
        for idx, t in enumerate(tasks):
            deadline_utc = _parse_iso_utc(t.get("deadline_ts"))
            out.append(SourceTask(
                source=self.SOURCE,
                source_id=f"{msg_id}::{idx}",
                title=t["title"],
                deadline_utc=deadline_utc,
                duration_hint_min=t.get("duration_min"),
                priority_hint=t.get("priority"),
                notes=f"From: {sender}\nSubject: {subject}" if sender or subject else None,
                extra={
                    "message_id": msg_id,
                    "received_at": received,
                    "categories": list(msg.get("categories") or []),
                },
            ))
        return out


# ---- Module-private helpers ----

def _strip_html(html: str) -> str:
    """Very small HTML → plain-text pass. The output is fed to an
    LLM, so fidelity matters only modestly — collapsing tags and
    extra whitespace is enough. We deliberately avoid pulling lxml
    or BeautifulSoup for a field the prompt caps at 8000 chars."""
    no_tags = _HTML_TAG_RE.sub(" ", html or "")
    # Unescape the handful of entities we reliably see.
    no_tags = (no_tags
               .replace("&nbsp;", " ")
               .replace("&amp;", "&")
               .replace("&lt;", "<")
               .replace("&gt;", ">")
               .replace("&quot;", '"')
               .replace("&#39;", "'"))
    return _WS_RE.sub(" ", no_tags).strip()


def _odata_escape(s: str) -> str:
    """OData string literals escape single quotes by doubling them."""
    return (s or "").replace("'", "''")


def _parse_iso_utc(s: Optional[str]) -> Optional[datetime]:
    if not s or not isinstance(s, str):
        return None
    raw = s.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
