from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Optional

from .store import has_trial_claim_for_phone_hash, upsert_user_entitlement

E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")


@dataclass
class TrialCheckResult:
    eligible: bool
    reason: str
    phone_hash: Optional[str] = None


def normalize_phone(phone: str) -> str:
    p = (phone or "").strip().replace(" ", "")
    if not E164_RE.match(p):
        raise ValueError("invalid_phone_e164")
    return p


def validate_phone_e164(phone: str) -> str:
    return normalize_phone(phone)


def hash_phone(phone_e164: str) -> str:
    return hashlib.sha256(phone_e164.encode()).hexdigest()


def check_trial_eligibility(phone_e164: str) -> TrialCheckResult:
    norm = normalize_phone(phone_e164)
    h = hash_phone(norm)
    if has_trial_claim_for_phone_hash(h):
        return TrialCheckResult(eligible=False, reason="trial_already_claimed_for_phone", phone_hash=h)
    return TrialCheckResult(eligible=True, reason="ok", phone_hash=h)


def record_trial_claim(
    user_id: str,
    *,
    phone_e164: str,
    device_fingerprint: Optional[str],
    signup_ip: Optional[str],
) -> None:
    norm = normalize_phone(phone_e164)
    upsert_user_entitlement(
        user_id,
        phone_e164=norm,
        phone_hash=hash_phone(norm),
        device_fingerprint=device_fingerprint,
        signup_ip=signup_ip,
        trial_granted=True,
    )
