"""Provider-neutral outbound-notification interface.

Every notifier takes a plain-text body and delivers it to the user. Optional
`title` + `priority` are accepted for channels that support them (Pushover,
ntfy, email); channels that don't just ignore them (iMessage, SMS).

Returns a dict with at least `{"ok": bool}` so the caller can log success.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional


class Notifier(ABC):
    CHANNEL: str = ""

    @abstractmethod
    def send(
        self,
        body: str,
        *,
        title: Optional[str] = None,
        priority: str = "normal",
    ) -> dict:
        """Deliver a message. Priority is one of 'low' | 'normal' | 'high'.
        Returns e.g. {"ok": True} or {"ok": False, "error": "..."}.
        """

    def health_check(self) -> dict:
        return {"channel": self.CHANNEL or type(self).__name__}
