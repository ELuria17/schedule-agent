"""ntfy.sh notifier (cross-platform push notifications).

Free service at ntfy.sh — no account needed. Users:
  1. Install the ntfy app on their phone/desktop.
  2. Subscribe to a private topic (any random string works as a passphrase-like
     identifier — `schedule-<your-random-suffix>` is a reasonable pattern).
  3. Configure this notifier with the same topic.

Every call to `send()` POSTs to `https://ntfy.sh/<topic>` and all devices
subscribed to that topic receive a push notification within seconds. Delivery
is best-effort; failures are reported in the return dict.

For stronger privacy, a self-hosted ntfy server can be substituted via the
`base_url` parameter.
"""
from __future__ import annotations

from typing import Optional

import requests

from .notifier import Notifier


# Ntfy's priority values (1-5). Normalized to the Notifier contract.
_PRIORITY_MAP = {"low": 2, "normal": 3, "high": 5}


class NtfyNotifier(Notifier):
    CHANNEL = "ntfy"

    def __init__(
        self,
        *,
        topic: str,
        base_url: str = "https://ntfy.sh",
        bearer_token: Optional[str] = None,
        default_tags: Optional[list[str]] = None,
        timeout: int = 15,
    ):
        """
        topic: the ntfy topic slug — treat as a secret (anyone with it can
            both publish and subscribe). Pick something hard to guess.
        base_url: defaults to the public ntfy.sh; swap for a self-hosted
            server if needed.
        bearer_token: optional — only needed if your ntfy server requires
            auth (self-hosted behind ACL).
        default_tags: list of emoji aliases or words prepended to each
            notification (e.g., ["calendar", "alarm_clock"]).
        """
        self.topic = topic
        self.base_url = base_url.rstrip("/")
        self.bearer_token = bearer_token
        self.default_tags = default_tags or []
        self.timeout = timeout

    def send(self, body: str, *, title: Optional[str] = None,
             priority: str = "normal") -> dict:
        headers = {}
        if title:
            headers["Title"] = title
        headers["Priority"] = str(_PRIORITY_MAP.get(priority, 3))
        if self.default_tags:
            headers["Tags"] = ",".join(self.default_tags)
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        try:
            r = requests.post(
                f"{self.base_url}/{self.topic}",
                data=body.encode("utf-8"),
                headers=headers,
                timeout=self.timeout,
            )
        except Exception as ex:
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}
        if r.status_code >= 400:
            return {"ok": False,
                    "error": f"ntfy HTTP {r.status_code}: {r.text[:200]}"}
        return {"ok": True, "message_id": r.headers.get("X-Message-Id")}

    def health_check(self) -> dict:
        return {
            "channel": self.CHANNEL,
            "base_url": self.base_url,
            "topic": self.topic[:4] + "…" if len(self.topic) > 8 else self.topic,
            "authenticated": bool(self.bearer_token),
        }
