"""Email notifications with the alarm snapshot attached."""

import configparser
import os
import smtplib
import ssl
import threading
import time
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

PASSWORD_ENV = "ANTICAT_SMTP_PASSWORD"


class EmailNotifier:
    """Sends a JPEG snapshot by email in a background thread.

    Settings come from an INI file (see email.example.ini). The SMTP password can also
    be supplied through the ANTICAT_SMTP_PASSWORD environment variable instead of the file.
    """

    def __init__(self, config_path: Path, to_override: list[str] | None = None,
                 cooldown: float | None = None):
        cfg = configparser.ConfigParser()
        if not cfg.read(config_path, encoding="utf-8"):
            raise ValueError(f"Email config not found: {config_path}")
        smtp = cfg["smtp"]
        mail = cfg["email"] if cfg.has_section("email") else {}

        self.host = smtp.get("host")
        self.port = int(smtp.get("port", "587"))
        self.security = smtp.get("security", "starttls").lower()  # starttls | ssl | none
        self.user = smtp.get("user", "")
        self.password = os.environ.get(PASSWORD_ENV) or smtp.get("password", "")
        self.sender = mail.get("from") or self.user
        recipients = to_override or [a.strip() for a in mail.get("to", "").split(",")]
        self.to = [a for a in recipients if a]
        self.subject = mail.get("subject", "Anticat: cat detected")
        self.cooldown = cooldown if cooldown is not None else float(mail.get("cooldown", "60"))

        if not self.host:
            raise ValueError("Email config: [smtp] host is required")
        if not self.to:
            raise ValueError("Email config: [email] to needs at least one address")
        if self.security not in ("starttls", "ssl", "none"):
            raise ValueError("Email config: security must be starttls, ssl or none")

        self.last_sent = 0.0
        self.thread: threading.Thread | None = None

    def notify(self, jpeg: bytes, when: datetime) -> bool:
        """Queue an email unless one was sent within the cooldown. Returns True if queued."""
        now = time.monotonic()
        if self.last_sent and now - self.last_sent < self.cooldown:
            return False
        if self.thread and self.thread.is_alive():
            return False
        self.last_sent = now
        self.thread = threading.Thread(target=self._send_safe, args=(jpeg, when), daemon=True)
        self.thread.start()
        return True

    def _send_safe(self, jpeg: bytes, when: datetime) -> None:
        try:
            self.send(jpeg, when)
            print(f"Email sent to {', '.join(self.to)}")
        except Exception as exc:
            print(f"Email failed: {exc}")

    def send(self, jpeg: bytes, when: datetime) -> None:
        msg = EmailMessage()
        msg["Subject"] = f"{self.subject} ({when:%Y-%m-%d %H:%M:%S})"
        msg["From"] = self.sender
        msg["To"] = ", ".join(self.to)
        msg.set_content(f"A cat was detected by the camera at {when:%Y-%m-%d %H:%M:%S}.\n"
                        "The snapshot is attached.")
        msg.add_attachment(jpeg, maintype="image", subtype="jpeg",
                           filename=f"cat_{when:%Y%m%d_%H%M%S}.jpg")

        context = ssl.create_default_context()
        if self.security == "ssl":
            server = smtplib.SMTP_SSL(self.host, self.port, context=context, timeout=30)
        else:
            server = smtplib.SMTP(self.host, self.port, timeout=30)
        with server:
            if self.security == "starttls":
                server.starttls(context=context)
            if self.user:
                server.login(self.user, self.password)
            server.send_message(msg)

    def wait(self, timeout: float = 30) -> None:
        if self.thread:
            self.thread.join(timeout)
