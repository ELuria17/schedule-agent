"""iMessage notifier (macOS only).

Invokes `macos/send_imessage.applescript` via osascript to send an iMessage
from the signed-in Messages.app to the configured recipient. See ROADBLOCKS
§M2 for the known "self-to-self doesn't push-notify" limitation, and §M3 for
the TCC prompt on first run.

Works only on a Mac with Messages.app signed in. For cross-platform use,
swap to NtfyNotifier (already in this repo), or implement a Pushover/email
notifier against the `Notifier` ABC.
"""
from __future__ import annotations

import subprocess
from typing import Optional

from .notifier import Notifier


class IMessageNotifier(Notifier):
    CHANNEL = "imessage"

    def __init__(self, *, script_path: str, recipient: str, timeout: int = 30):
        """
        script_path: absolute path to send_imessage.applescript.
        recipient: phone number (E.164) or email address registered with
            iMessage. For self-sending, this is the user's own number.
        """
        self.script_path = script_path
        self.recipient = recipient
        self.timeout = timeout

    def send(self, body: str, *, title: Optional[str] = None,
             priority: str = "normal") -> dict:
        # iMessage doesn't support title or priority; silently ignore.
        # (Notifier contract requires accepting them so callers don't branch.)
        try:
            r = subprocess.run(
                ["osascript", self.script_path, self.recipient, body],
                capture_output=True, text=True, timeout=self.timeout,
            )
        except Exception as ex:
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}
        if r.returncode != 0:
            return {"ok": False,
                    "error": f"osascript exit {r.returncode}: {r.stderr.strip()[:300]}"}
        return {"ok": True}

    def health_check(self) -> dict:
        return {
            "channel": self.CHANNEL,
            "script_path": self.script_path,
            "recipient": self.recipient,
        }
