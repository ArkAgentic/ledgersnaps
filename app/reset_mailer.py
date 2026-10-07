from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage

from azure.communication.email import EmailClient


def _provider() -> str:
    return os.getenv("RESET_EMAIL_PROVIDER", "dev").strip().lower()


def _send_via_smtp(*, to_email: str, subject: str, body_text: str) -> None:
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_port = int(os.getenv("SMTP_PORT", "587").strip() or "587")
    smtp_username = os.getenv("SMTP_USERNAME", "").strip()
    smtp_password = os.getenv("SMTP_PASSWORD", "").strip()
    smtp_from = os.getenv("SMTP_FROM", "").strip()
    smtp_starttls = (os.getenv("SMTP_STARTTLS", "true").strip().lower() != "false")

    if not smtp_host or not smtp_from:
        raise RuntimeError("smtp_not_configured")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_from
    msg["To"] = to_email
    msg.set_content(body_text)

    with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as s:
        if smtp_starttls:
            s.starttls()
        if smtp_username:
            s.login(smtp_username, smtp_password)
        s.send_message(msg)


def _send_via_azure(*, to_email: str, subject: str, body_text: str) -> None:
    conn = os.getenv("AZURE_EMAIL_CONNECTION_STRING", "").strip()
    sender = os.getenv("AZURE_EMAIL_SENDER_ADDRESS", "").strip()
    if not conn or not sender:
        raise RuntimeError("azure_email_not_configured")

    client = EmailClient.from_connection_string(conn)
    poller = client.begin_send(
        {
            "senderAddress": sender,
            "recipients": {"to": [{"address": to_email}]},
            "content": {"subject": subject, "plainText": body_text},
        }
    )
    poller.result()


def _send_email(*, to_email: str, subject: str, body_text: str) -> None:
    provider = _provider()
    if provider == "dev":
        return
    if provider == "smtp":
        _send_via_smtp(to_email=to_email, subject=subject, body_text=body_text)
        return
    if provider == "azure":
        _send_via_azure(to_email=to_email, subject=subject, body_text=body_text)
        return
    raise RuntimeError(f"unsupported_reset_email_provider:{provider}")


def send_password_reset_email(*, to_email: str, reset_link: str) -> None:
    _send_email(
        to_email=to_email,
        subject="LedgerSnaps password reset",
        body_text=(
            "You requested to reset your LedgerSnaps password.\n\n"
            f"Reset link: {reset_link}\n\n"
            "If you did not request this, you can ignore this email."
        ),
    )


def send_signup_verification_email(*, to_email: str, verify_link: str, verify_code: str) -> None:
    _send_email(
        to_email=to_email,
        subject="Your LedgerSnaps verification code",
        body_text=(
            "Welcome to LedgerSnaps.\n\n"
            f"Your 6-digit verification code: {verify_code}\n\n"
            "Enter this code in the signup page to complete registration.\n"
            f"You can also verify using this link: {verify_link}\n\n"
            "If you did not create this account, you can ignore this email."
        ),
    )
