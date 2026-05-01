"""APNs notifier — pushes a notification to every registered iPhone + Mac.

Wired through the Notifier ABC so it slots into schedule_config alongside
existing notifiers (iMessage, ntfy, Pushover, Slack, email).

**Setup before this works:**

1. App Store Connect → Certificates → Keys → "+" → check "Apple Push
   Notifications service (APNs)" → register. Download the `.p8` file
   (one-time download — keep it safe).
2. Save the .p8 to `~/schedule-agent-public/secrets/apns_auth_key.p8`
   (or wherever; pass via `auth_key_path` in schedule_config).
3. Note your Team ID (App Store Connect → top-right → Membership) and the
   Key ID shown next to the key in the Keys list.
4. Bundle ID is the Xcode bundle identifier of the app. For our setup
   it's `com.eytanluria.scheduleagent` — must match for both targets.
5. Wire in `schedule_config.py`:

       from providers.apns_notifier import APNsNotifier
       NOTIFIER = APNsNotifier(
           auth_key_path="secrets/apns_auth_key.p8",
           key_id="ABC1234567",
           team_id="5BR8J7JYL3",
           bundle_id="com.eytanluria.scheduleagent",
           use_sandbox=False,   # True for development builds (TestFlight uses production)
       )

PyJWT must be installed: `pip install pyjwt cryptography`.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

import httpx

from .notifier import Notifier
import config as _config_mod


class APNsNotifier(Notifier):
    """Sends a push to every device_tokens row.

    Tokens that come back as 410 Gone are deleted from the registry —
    Apple returns 410 when the user uninstalls the app or revokes
    permission. Other failures are logged and skipped.
    """

    PRODUCTION_HOST = "api.push.apple.com"
    SANDBOX_HOST = "api.sandbox.push.apple.com"

    def __init__(
        self, *,
        auth_key_path: str,
        key_id: str,
        team_id: str,
        bundle_id: str,
        use_sandbox: bool = False,
        timeout: float = 10.0,
    ):
        self.auth_key_path = Path(auth_key_path)
        self.key_id = key_id
        self.team_id = team_id
        self.bundle_id = bundle_id
        self.use_sandbox = use_sandbox
        self.timeout = timeout
        self._cached_jwt: Optional[str] = None
        self._cached_jwt_at: float = 0.0

    # ---- Notifier interface ----

    def send(self, body: str) -> dict:
        tokens = self._load_tokens()
        if not tokens:
            return {"ok": True, "sent": 0, "note": "no registered devices"}
        sent, failed, dropped = 0, 0, 0
        host = self.SANDBOX_HOST if self.use_sandbox else self.PRODUCTION_HOST
        url_template = f"https://{host}/3/device/{{token}}"
        payload = json.dumps({"aps": {"alert": body, "sound": "default"}}).encode("utf-8")
        with httpx.Client(http2=True, timeout=self.timeout) as client:
            for row in tokens:
                token = row["token"]
                resp = client.post(
                    url_template.format(token=token),
                    headers={
                        "authorization": f"bearer {self._jwt()}",
                        "apns-topic": self.bundle_id,
                        "apns-push-type": "alert",
                    },
                    content=payload,
                )
                if resp.status_code == 200:
                    sent += 1
                elif resp.status_code == 410:
                    self._drop_token(token)
                    dropped += 1
                else:
                    failed += 1
        return {"ok": failed == 0, "sent": sent, "failed": failed, "dropped": dropped}

    def health_check(self) -> dict:
        info = {"provider": "APNsNotifier", "use_sandbox": self.use_sandbox,
                "bundle_id": self.bundle_id, "team_id": self.team_id}
        try:
            self._jwt(force=True)
            info["jwt"] = "ok"
        except Exception as ex:
            info["jwt_error"] = f"{type(ex).__name__}: {ex}"
        with _config_mod.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM device_tokens").fetchone()
        info["registered_devices"] = row["n"]
        return info

    # ---- Internals ----

    def _jwt(self, *, force: bool = False) -> str:
        # APNs JWTs are valid for 60 minutes; cache and refresh after 50.
        if not force and self._cached_jwt and (time.time() - self._cached_jwt_at) < 50 * 60:
            return self._cached_jwt
        try:
            import jwt as pyjwt
        except ImportError:
            raise RuntimeError("PyJWT not installed: pip install pyjwt cryptography")
        priv = self.auth_key_path.read_text()
        token = pyjwt.encode(
            {"iss": self.team_id, "iat": int(time.time())},
            priv,
            algorithm="ES256",
            headers={"kid": self.key_id, "alg": "ES256"},
        )
        self._cached_jwt = token
        self._cached_jwt_at = time.time()
        return token

    def _load_tokens(self) -> list[dict]:
        with _config_mod.connect() as conn:
            rows = conn.execute(
                "SELECT token, platform FROM device_tokens"
            ).fetchall()
        return [dict(r) for r in rows]

    def _drop_token(self, token: str) -> None:
        with _config_mod.connect() as conn:
            conn.execute("DELETE FROM device_tokens WHERE token=?", (token,))
