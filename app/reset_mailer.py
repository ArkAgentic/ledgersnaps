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
        "X-Auto-Response-Suppress": "All",
        "Precedence": "bulk",
        "X-Priority": "3",
    }


def render_signup_verification_email_html(*, verify_code: str) -> str:
    return f"""<!doctype html>
<html>
  <body style=\"margin:0;padding:0;background:#f5f1eb;font-family:'Sora','Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;\">
    <table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" style=\"border-collapse:collapse;background:#f5f1eb;background-image:url('https://www.ledgersnaps.com/assets/images/landing-bg-light.png');background-size:cover;background-position:center;\">
      <tr>
        <td style=\"padding:28px 24px 32px 24px;\">
          <!--[if gte mso 9]>
          <v:rect xmlns:v=\"urn:schemas-microsoft-com:vml\" fill=\"true\" stroke=\"false\" style=\"width:640px;height:560px;\">
            <v:fill type=\"frame\" src=\"https://www.ledgersnaps.com/assets/images/landing-bg-light.png\" color=\"#f5f1eb\" />
            <v:textbox inset=\"0,0,0,0\">
          <![endif]-->
          <table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" style=\"max-width:640px;margin:0 auto;border-collapse:collapse;\">
            <tr>
              <td style=\"padding:0 0 12px 0;\">
                <img src=\"https://www.ledgersnaps.com/assets/images/logo-transparent.png\" alt=\"LedgerSnaps\" width=\"210\" style=\"display:block;border:0;outline:none;text-decoration:none;height:auto;max-width:100%;\" />
              </td>
            </tr>
            <tr>
              <td style=\"padding:6px 0 0 0;\">
                <p style=\"margin:0 0 12px;font-size:15px;line-height:1.65;color:#0f172a;\">Hi there,</p>
                <p style=\"margin:0 0 12px;font-size:15px;line-height:1.65;color:#0f172a;\">Thanks for joining LedgerSnaps.</p>
                <p style=\"margin:0 0 22px;font-size:15px;line-height:1.65;color:#0f172a;\">Please use this 6-digit code to complete your sign up:</p>
                <p style=\"margin:0 0 22px;\"><span style=\"display:inline-block;padding:12px 22px;border-radius:12px;background:#0f172a;color:#ffffff;font-size:30px;letter-spacing:7px;font-weight:700;\">{verify_code}</span></p>
                <p style=\"margin:0 0 10px;font-size:13px;line-height:1.65;color:#475569;\">This code expires in 30 minutes.</p>
                <p style=\"margin:0 0 10px;font-size:13px;line-height:1.65;color:#475569;\">If you did not request this, you can safely ignore this email.</p>
                <p style=\"margin:16px 0 0;font-size:14px;line-height:1.65;color:#0f172a;\">Warm regards,<br/>LedgerSnaps team</p>
              </td>
            </tr>
          </table>
          <!--[if gte mso 9]>
            </v:textbox>
          </v:rect>
          <![endif]-->
        </td>
      </tr>
    </table>
  </body>
</html>"""


def send_signup_verification_email(*, to_email: str, verify_link: str, verify_code: str) -> None:
    _ = verify_link
    html_body = render_signup_verification_email_html(verify_code=verify_code)

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
