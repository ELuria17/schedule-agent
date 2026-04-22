"""Slack notifier (incoming webhook).

Simplest possible Slack integration: you create an Incoming Webhook in
your workspace's Slack app settings, AutoPlan POSTs JSON to it. No OAuth,
no bot tokens, no scopes.

Setup:
1. Go to https://api.slack.com/apps → Create New App → From scratch.
2. Pick a name ("AutoPlan") and the workspace you want messages in.
3. In the app settings, go to "Incoming Webhooks" → toggle on.
4. Click "Add New Webhook to Workspace" → pick the channel (or "Directly
   to yourself" for a DM).
5. Copy the Webhook URL (starts with https://hooks.slack.com/services/...)
   into SLACK_WEBHOOK_URL in your .env.

The webhook is channel-bound — one URL posts to one channel. If you
want AutoPlan to post in multiple places, add additional webhooks.
"""
from __future__ import annotations

from typing import Optional

import requests

from .notifier import Notifier


# Slack itself doesn't have a priority field for incoming webhooks. We
# render high/low as a leading emoji so you can tell apart summaries
# from at-risk alerts at a glance.
_PRIORITY_PREFIX = {
    "high": ":rotating_light: ",
    "normal": "",
    "low": ":grey_exclamation: ",
}


class SlackNotifier(Notifier):
    CHANNEL = "slack"

    def __init__(
        self,
        *,
        webhook_url: str,
        username: Optional[str] = None,
        icon_emoji: Optional[str] = None,
        timeout: int = 15,
    ):
        """
        webhook_url: the Incoming Webhook URL from Slack app settings.
        username: optional display name (overrides the app's default).
        icon_emoji: optional override icon, e.g. ":calendar:".
        """
        self.webhook_url = webhook_url
        self.username = username
        self.icon_emoji = icon_emoji
        self.timeout = timeout

    def send(self, body: str, *, title: Optional[str] = None,
             priority: str = "normal") -> dict:
        prefix = _PRIORITY_PREFIX.get(priority, "")
        if title:
            text = f"{prefix}*{title}*\n{body}"
        else:
            text = f"{prefix}{body}"

        payload: dict = {"text": text}
        if self.username:
            payload["username"] = self.username
        if self.icon_emoji:
            payload["icon_emoji"] = self.icon_emoji

        try:
            r = requests.post(self.webhook_url, json=payload,
                              timeout=self.timeout)
        except Exception as ex:
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}
        if r.status_code >= 400:
            return {"ok": False,
                    "error": f"slack HTTP {r.status_code}: {r.text[:200]}"}
        # Slack returns "ok" for webhook POSTs that land.
        return {"ok": True}

    def health_check(self) -> dict:
        # Redact the webhook URL — the random suffix is the secret.
        redacted = self.webhook_url.split("/services/", 1)
        url_display = (redacted[0] + "/services/…"
                       if len(redacted) == 2 else "…")
        return {
            "channel": self.CHANNEL,
            "webhook": url_display,
            "username": self.username,
        }
