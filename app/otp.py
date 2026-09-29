from __future__ import annotations

import os
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional


@dataclass
class OtpRecord:
    phone_e164: str
    code: str
    expires_at: datetime
    sent_at: datetime


class OtpProvider:
    """OTP sender/verifier abstraction.

    Modes:
    - dev: store code in-memory, no external SMS cost
    - azure_sms: send via Azure Communication Services SMS
    """

    def __init__(self, mode: Optional[str] = None) -> None:
        self.mode = (mode or os.getenv("OTP_PROVIDER", "dev")).strip().lower()
        self._cache: dict[str, OtpRecord] = {}

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _normalize(self, phone_e164: str) -> str:
        return phone_e164.strip()

    def send_code(self, phone_e164: str) -> dict:
        p = self._normalize(phone_e164)
        code = f"{random.randint(0, 999999):06d}"
        now = self._now()
        rec = OtpRecord(phone_e164=p, code=code, expires_at=now + timedelta(minutes=10), sent_at=now)
        self._cache[p] = rec

        if self.mode == "azure_sms":
            self._send_azure_sms(p, code)
        # dev mode sends nothing externally

        out = {"ok": True, "provider": self.mode, "expires_in_seconds": 600}
        if self.mode == "dev":
            out["dev_code"] = code  # test-only visibility
        return out

    def verify_code(self, phone_e164: str, code: str) -> bool:
        p = self._normalize(phone_e164)
        rec = self._cache.get(p)
        if not rec:
            return False
        if self._now() > rec.expires_at:
            return False
        if str(code).strip() != rec.code:
            return False
        return True

    def _send_azure_sms(self, phone_e164: str, code: str) -> None:
        conn = os.getenv("AZURE_COMMUNICATION_CONNECTION_STRING", "").strip()
        sender = os.getenv("AZURE_COMMUNICATION_SMS_FROM", "").strip()
        if not conn or not sender:
            raise RuntimeError(
                "azure_sms_not_configured: set AZURE_COMMUNICATION_CONNECTION_STRING and AZURE_COMMUNICATION_SMS_FROM"
            )
        try:
            from azure.communication.sms import SmsClient  # type: ignore
        except Exception as e:  # noqa: BLE001
            raise RuntimeError("azure_communication_sms_sdk_missing: pip install azure-communication-sms") from e

        msg = f"Your LedgerSnaps verification code is: {code}. It expires in 10 minutes."
        client = SmsClient.from_connection_string(conn)
        resp = client.send(from_=sender, to=[phone_e164], message=msg, enable_delivery_report=False)
        if not resp or not isinstance(resp, list):
            raise RuntimeError("azure_sms_send_failed")
        st = (resp[0].successful if hasattr(resp[0], "successful") else True)
        if not st:
            raise RuntimeError("azure_sms_send_failed")


otp_provider = OtpProvider()
