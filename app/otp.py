from __future__ import annotations

import json
import os
import random
import urllib.error
import urllib.request
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

        msg = f"Your LedgerSnaps verification code is: {code}. It expires in 10 minutes."

        # 1) Try native ACS SMS SDK first (legacy path).
        try:
            from azure.communication.sms import SmsClient  # type: ignore

            client = SmsClient.from_connection_string(conn)
            resp = client.send(from_=sender, to=[phone_e164], message=msg, enable_delivery_report=False)
            if not resp or not isinstance(resp, list):
                raise RuntimeError("azure_sms_send_failed")
            st = (resp[0].successful if hasattr(resp[0], "successful") else True)
            if st:
                return
        except Exception:
            # Fall through to Messaging Connect preview API.
            pass

        # 2) Messaging Connect preview fallback (partner: Infobip).
        infobip_api_key = os.getenv("INFOBIP_API_KEY", "").strip()
        if not infobip_api_key:
            raise RuntimeError("infobip_api_key_missing: set INFOBIP_API_KEY for Messaging Connect SMS")

        endpoint = conn.split(";", 1)[0]
        if endpoint.lower().startswith("endpoint="):
            endpoint = endpoint[len("endpoint=") :]
        endpoint = endpoint.rstrip("/")
        url = f"{endpoint}/sms?api-version=2025-05-29-preview"

        payload = {
            "from": sender,
            "to": [phone_e164],
            "message": msg,
            "options": {
                "enableDeliveryReport": False,
                "messagingConnect": {
                    "partner": "infobip",
                    "apiKey": infobip_api_key,
                },
            },
        }

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                body = r.read().decode("utf-8", errors="ignore")
                if r.status < 200 or r.status >= 300:
                    raise RuntimeError(f"messaging_connect_sms_failed:{r.status}:{body[:300]}")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="ignore")
            except Exception:
                detail = str(e)
            raise RuntimeError(f"messaging_connect_sms_failed:{e.code}:{detail[:300]}") from e


otp_provider = OtpProvider()
