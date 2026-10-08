from __future__ import annotations

import os
import smtplib
import secrets
from email.message import EmailMessage

from azure.communication.email import EmailClient


def _provider() -> str:
    return os.getenv("RESET_EMAIL_PROVIDER", "dev").strip().lower()


def _send_via_smtp(*, to_email: str, subject: str, body_text: str, body_html: str | None = None) -> None:
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
    if body_html:
        msg.set_content(body_text)
        msg.add_alternative(body_html, subtype="html")
    else:
        msg.set_content(body_text)

    with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as s:
        if smtp_starttls:
            s.starttls()
        if smtp_username:
            s.login(smtp_username, smtp_password)
        s.send_message(msg)


def _send_via_azure(*, to_email: str, subject: str, body_text: str, body_html: str | None = None, headers: dict[str, str] | None = None) -> None:
    conn = os.getenv("AZURE_EMAIL_CONNECTION_STRING", "").strip()
    sender = os.getenv("AZURE_EMAIL_SENDER_ADDRESS", "").strip()
    if not conn or not sender:
        raise RuntimeError("azure_email_not_configured")

    client = EmailClient.from_connection_string(conn)
    content = {"subject": subject, "plainText": body_text}
    if body_html:
        content["html"] = body_html
    payload: dict[str, object] = {
        "senderAddress": sender,
        "recipients": {"to": [{"address": to_email}]},
        "content": content,
    }
    if headers:
        payload["headers"] = headers

    poller = client.begin_send(payload)
    poller.result()


def _send_email(
    *,
    to_email: str,
    subject: str,
    body_text: str,
    body_html: str | None = None,
    headers: dict[str, str] | None = None,
) -> None:
    provider = _provider()
    if provider == "dev":
        return
    if provider == "smtp":
        _send_via_smtp(to_email=to_email, subject=subject, body_text=body_text, body_html=body_html)
        return
    if provider == "azure":
        _send_via_azure(
            to_email=to_email,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            headers=headers,
        )
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


def _sender_domain() -> str:
    sender = os.getenv("AZURE_EMAIL_SENDER_ADDRESS", "").strip()
    if "@" in sender:
        return sender.split("@", 1)[1].strip().lower()
    return "ledgersnaps.com"


def _recipient_token(to_email: str) -> str:
    _ = to_email
    return secrets.token_urlsafe(18)


def _unsubscribe_link(to_email: str) -> str:
    base = os.getenv("EMAIL_PREFERENCE_BASE_URL", "https://www.ledgersnaps.com").strip().rstrip("/")
    token = _recipient_token(to_email)
    return f"{base}/email/preferences?token={token}"


def _signup_headers(to_email: str) -> dict[str, str]:
    unsubscribe_url = _unsubscribe_link(to_email)
    sender_domain = _sender_domain()
    return {
        "Reply-To": f"support@{sender_domain}",
        "List-Unsubscribe": f"<{unsubscribe_url}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        "X-Entity-Ref-ID": secrets.token_hex(16),
    }


def send_signup_verification_email(*, to_email: str, verify_link: str, verify_code: str) -> None:
    html_body = f"""<!doctype html>
<html>
  <body style=\"margin:0;padding:0;background:#f5f1eb;\">
    <table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" style=\"background:#f5f1eb;padding:28px 0;\">
      <tr>
        <td align=\"center\">
          <table role=\"presentation\" width=\"560\" cellpadding=\"0\" cellspacing=\"0\" style=\"max-width:560px;width:100%;background:#ffffff;border:1px solid #e5e7eb;border-radius:14px;overflow:hidden;\">
            <tr>
              <td style=\"padding:24px 28px 10px 28px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;color:#111827;\">
                <div style=\"font-size:20px;font-weight:700;line-height:1.4;\">LedgerSnaps</div>
                <div style=\"margin-top:14px;font-size:16px;line-height:1.6;color:#1f2937;\">Hi there,</div>
                <div style=\"margin-top:10px;font-size:15px;line-height:1.7;color:#374151;\">Thanks for signing up. Please enter this 6-digit verification code on the signup page:</div>
              </td>
            </tr>
            <tr>
              <td style=\"padding:8px 28px 4px 28px;\">
                <div style=\"display:inline-block;background:#111827;color:#ffffff;border-radius:10px;padding:12px 18px;font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,'Liberation Mono','Courier New',monospace;font-size:28px;font-weight:700;letter-spacing:0.24em;\">{verify_code}</div>
              </td>
            </tr>
            <tr>
              <td style=\"padding:8px 28px 24px 28px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;color:#4b5563;\">
                <div style=\"font-size:14px;line-height:1.7;\">For your security, this code expires in 30 minutes. If you didn’t request this, you can safely ignore this email.</div>
                <div style=\"margin-top:18px;font-size:14px;line-height:1.7;color:#111827;\">Warm regards,<br/>LedgerSnaps team</div>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>"""

    plain_text = (
        "Hi there,\n\n"
        "Thanks for signing up for LedgerSnaps.\n"
        f"Your 6-digit verification code is: {verify_code}\n\n"
        "Enter this code on the signup page to continue.\n"
        "This code expires in 30 minutes.\n\n"
        "If you did not request this, you can ignore this email.\n\n"
        "LedgerSnaps team"
    )

    _send_email(
        to_email=to_email,
        subject="Your LedgerSnaps verification code",
        body_text=plain_text,
        body_html=html_body,
        headers=_signup_headers(to_email),
    )
