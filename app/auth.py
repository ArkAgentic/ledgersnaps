from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from typing import Optional

from fastapi import Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer


bearer_scheme = HTTPBearer(auto_error=False)


@dataclass
class CurrentUser:
    user_id: str
    tenant_id: str
    auth_source: str  # bearer | legacy_header | demo


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * ((4 - len(data) % 4) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _auth_secret() -> str:
    return os.getenv("AUTH_SECRET", "dev-insecure-change-me")


def issue_dev_token(user_id: str, tenant_id: str = "default", ttl_seconds: int = 60 * 60 * 24) -> str:
    now = int(time.time())
    payload = {
        "sub": user_id,
        "tenant_id": tenant_id,
        "iat": now,
        "exp": now + ttl_seconds,
    }
    body = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(_auth_secret().encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64url_encode(sig)}"


def _verify_token(token: str) -> CurrentUser:
    try:
        body, sig = token.split(".", 1)
    except ValueError as e:
        raise HTTPException(status_code=401, detail="invalid_token_format") from e

    expected = hmac.new(_auth_secret().encode(), body.encode(), hashlib.sha256).digest()
    got = _b64url_decode(sig)
    if not hmac.compare_digest(expected, got):
        raise HTTPException(status_code=401, detail="invalid_token_signature")

    try:
        payload = json.loads(_b64url_decode(body).decode())
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=401, detail="invalid_token_payload") from e

    exp = int(payload.get("exp", 0))
    if exp <= int(time.time()):
        raise HTTPException(status_code=401, detail="token_expired")

    user_id = str(payload.get("sub", "")).strip()
    tenant_id = str(payload.get("tenant_id", "default")).strip() or "default"
    if not user_id:
        raise HTTPException(status_code=401, detail="token_missing_sub")

    return CurrentUser(user_id=user_id, tenant_id=tenant_id, auth_source="bearer")


def decode_token_optional(token: Optional[str], source: str = "bearer") -> Optional[CurrentUser]:
    if not token:
        return None
    try:
        u = _verify_token(token)
        return CurrentUser(user_id=u.user_id, tenant_id=u.tenant_id, auth_source=source)
    except HTTPException:
        return None


def resolve_current_user(
    credentials: Optional[HTTPAuthorizationCredentials],
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    dev_token: Optional[str] = None,
) -> CurrentUser:
    # 进入用户系统阶段后仅允许 Bearer Token；不再接受 X-User-Id/demo fallback。
    _ = x_user_id  # reserved for explicit deprecation handling/logging
    if credentials and credentials.scheme.lower() == "bearer" and credentials.credentials:
        return _verify_token(credentials.credentials)
    cookie_user = decode_token_optional(dev_token, source="cookie")
    if cookie_user:
        return cookie_user
    raise HTTPException(status_code=401, detail="missing_or_invalid_bearer_token")