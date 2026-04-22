"""Email / SMTP notifier.

Sends the daily summary as a plain-text email via any SMTP server.
Works with Gmail (app password), Outlook/Office365, Fastmail, SES,
Mailgun, etc. No OAuth — username + password (or SMTP token).

For Gmail, generate an app password at
https://myaccount.google.com/apppasswords (requires 2FA first).
For Outlook personal accounts, same pattern at
https://account.live.com/proofs/Manage/additional.
"""
from __future__ import annotations

import smtplib
from email.mime.text import MIMEText
from email.utils import formatdate
from typing import Optional

from .notifier import Notifier


class EmailNotifier(Notifier):
    CHANNEL = "email"

    def __init__(
        self,
        *,
        smtp_host: str,
        smtp_port: int,
        username: str,
        password: str,
        from_addr: str,
        to_addr: str,
        use_tls: bool = True,
        use_ssl: bool = False,
        timeout: int = 30,
        subject_prefix: str = "AutoPlan",
    ):
        """
        smtp_host / smtp_port: e.g. smtp.gmail.com / 587.
        username / password: SMTP credentials (often an app password — NOT
            your regular account password).
        from_addr: what appears in the From field. Usually == username.
        to_addr: recipient. Typically yourself.
        use_tls: STARTTLS upgrade on a plaintext port (587 typically).
        use_ssl: wrap the whole connection in TLS (465 typically). Mutually
            exclusive with use_tls — pick one based on your port.
        subject_prefix: prepended to every message subject, for easy
            inbox-filtering.
        """
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.username = username
        self.password = password
        self.from_addr = from_addr
        self.to_addr = to_addr
        self.use_tls = use_tls
        self.use_ssl = use_ssl
        self.timeout = timeout
        self.subject_prefix = subject_prefix

    def send(self, body: str, *, title: Optional[str] = None,
             priority: str = "normal") -> dict:
        subject = f"{self.subject_prefix}: {title}" if title \
                  else self.subject_prefix
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = self.from_addr
        msg["To"] = self.to_addr
        msg["Date"] = formatdate(localtime=True)
        # Priority headers — most mail clients render these as a star/flag.
        if priority == "high":
            msg["X-Priority"] = "1"
            msg["Importance"] = "High"
        elif priority == "low":
            msg["X-Priority"] = "5"
            msg["Importance"] = "Low"

        try:
            if self.use_ssl:
                server_cls = smtplib.SMTP_SSL
            else:
                server_cls = smtplib.SMTP
            with server_cls(self.smtp_host, self.smtp_port,
                            timeout=self.timeout) as s:
                if self.use_tls and not self.use_ssl:
                    s.starttls()
                s.login(self.username, self.password)
                s.send_message(msg)
        except Exception as ex:
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}
        return {"ok": True}

    def health_check(self) -> dict:
        return {
            "channel": self.CHANNEL,
            "smtp_host": self.smtp_host,
            "smtp_port": self.smtp_port,
            "from": self.from_addr,
            "to": self.to_addr,
            "tls": self.use_tls and not self.use_ssl,
            "ssl": self.use_ssl,
        }
