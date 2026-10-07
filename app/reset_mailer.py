from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage


def send_password_reset_email(*, to_email: str, reset_link: str) -> None:
    provider = os.getenv("RESET_EMAIL_PROVIDER", "dev").strip().lower()
    if provider == "dev":
        # Development mode: no external email side effect.
        return
    if provider != "smtp":
        raise RuntimeError(f"unsupported_reset_email_provider:{provider}")

    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_port = int(os.getenv("SMTP_PORT", "587").strip() or "587")
    smtp_username = os.getenv("SMTP_USERNAME", "").strip()
    smtp_password = os.getenv("SMTP_PASSWORD", "").strip()
    smtp_from = os.getenv("SMTP_FROM", "").strip()
    smtp_starttls = (os.getenv("SMTP_STARTTLS", "true").strip().lower() != "false")

    if not smtp_host or not smtp_from:
        raise RuntimeError("smtp_not_configured")

    msg = EmailMessage()
    msg["Subject"] = "LedgerSnaps password reset"
    msg["From"] = smtp_from
    msg["To"] = to_email
    msg.set_content(
        "You requested to reset your LedgerSnaps password.\n\n"
        f"Reset link: {reset_link}\n\n"
        "If you did not request this, you can ignore this email."
    )

    with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as s:
        if smtp_starttls:
            s.starttls()
        if smtp_username:
            s.login(smtp_username, smtp_password)
        s.send_message(msg)
