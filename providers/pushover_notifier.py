"""Pushover notifier.

Pushover (pushover.net) is a $5-one-time-per-platform push service with
a clean HTTP API. Good fit when you want reliable iOS + Android pushes
without standing up infrastructure.

Signup: https://pushover.net/

You need two tokens:
- USER key (per account): pushover.net/ → visible at top of dashboard.
- APP token: pushover.net/apps/build → "Create an Application" → copy
  the API token/key it generates.

Both go into .env. Treat the user key as a secret (anyone with it can
push messages to your devices).
"""
from __future__ import annotations

from typing import Optional

import requests

from .notifier import Notifier


_API_URL = "https://api.pushover.net/1/messages.json"

# Pushover priority is -2..+2. Map onto our "low"/"normal"/"high" tiers.
_PRIORITY_MAP = {"low": -1, "normal": 0, "high": 1}


class PushoverNotifier(Notifier):
    CHANNEL = "pushover"

    def __init__(
        self,
        *,
        user_key: str,
        app_token: str,
        device: Optional[str] = None,
        sound: Optional[str] = None,
        timeout: int = 15,
    ):
        """
        user_key: your personal user key (visible on pushover.net).
        app_token: an application token you generate at pushover.net/apps/build.
        device: optional — if set, push only to a specific device. If None,
            Pushover fans out to every device on your account.
        sound: optional — any of Pushover's built-in sound names (e.g.
            "pushover", "bike", "none"). None = app default.
        """
        self.user_key = user_key
        self.app_token = app_token
        self.device = device
        self.sound = sound
        self.timeout = timeout

    def send(self, body: str, *, title: Optional[str] = None,
             priority: str = "normal") -> dict:
        payload = {
            "user": self.user_key,
            "token": self.app_token,
            "message": body,
            "priority": _PRIORITY_MAP.get(priority, 0),
        }
        if title:
            payload["title"] = title
        if self.device:
            payload["device"] = self.device
        if self.sound:
            payload["sound"] = self.sound

        try:
            r = requests.post(_API_URL, data=payload, timeout=self.timeout)
        except Exception as ex:
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}
        if r.status_code >= 400:
            return {"ok": False,
                    "error": f"pushover HTTP {r.status_code}: {r.text[:200]}"}
        try:
            body_json = r.json()
        except Exception:
            body_json = {}
        return {"ok": bool(body_json.get("status") == 1),
                "request_id": body_json.get("request")}

    def health_check(self) -> dict:
        # Pushover has no free "ping" endpoint; /users/validate.json costs
        # a request against the daily quota. So we just report config.
        redacted_user = (self.user_key[:4] + "…" + self.user_key[-2:]
                         if len(self.user_key) > 8 else self.user_key)
        return {
            "channel": self.CHANNEL,
            "user_key": redacted_user,
            "device": self.device or "(all devices)",
        }
