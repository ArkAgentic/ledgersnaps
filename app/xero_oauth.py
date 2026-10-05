from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from .store import get_xero_connection, upsert_xero_connection

_AUTH_BASE = "https://login.xero.com/identity/connect/authorize"
_TOKEN_URL = "https://identity.xero.com/connect/token"
_CONNECTIONS_URL = "https://api.xero.com/connections"
_INVOICES_URL = "https://api.xero.com/api.xro/2.0/Invoices"

# local in-memory state map for dev flow
_STATE_CACHE: dict[str, dict[str, Any]] = {}


def _require_env(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        raise RuntimeError(f"xero_config_missing:{name}")
    return v


def _client_id() -> str:
    return _require_env("XERO_CLIENT_ID")


def _client_secret() -> str:
    return _require_env("XERO_CLIENT_SECRET")


def _redirect_uri() -> str:
    return _require_env("XERO_REDIRECT_URI")


def _default_scope() -> str:
    # Use new granular accounting scopes (accounting.transactions is deprecated).
    return os.getenv("XERO_SCOPE", "offline_access accounting.invoices accounting.contacts").strip()


def _public_auth_scope() -> str:
    # For sign-in/sign-up identity and future invoice draft push in one consent.
    return os.getenv(
        "XERO_AUTH_SCOPE",
        "openid profile email offline_access accounting.invoices accounting.contacts",
    ).strip()


def _basic_auth_header() -> str:
    raw = f"{_client_id()}:{_client_secret()}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _code_challenge(verifier: str) -> str:
    h = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(h).decode().rstrip("=")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * ((4 - len(data) % 4) % 4)
    return base64.urlsafe_b64decode(data + pad)


def _parse_jwt_payload_unverified(token: str) -> dict[str, Any]:
    # Xero id_token payload parsing for identity bootstrap.
    # Signature verification can be added with JWK retrieval if needed.
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload_raw = _b64url_decode(parts[1]).decode("utf-8")
        data = json.loads(payload_raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


async def _exchange_token_payload(*, code: str, verifier: str) -> dict[str, Any]:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": _redirect_uri(),
        "code_verifier": verifier,
    }
    headers = {
        "Authorization": _basic_auth_header(),
        "Content-Type": "application/x-www-form-urlencoded",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(_TOKEN_URL, data=data, headers=headers)
        if r.status_code >= 400:
            raise RuntimeError(f"xero_token_exchange_failed:{r.status_code}:{r.text[:200]}")
        tok = r.json()
        if not isinstance(tok, dict):
            raise RuntimeError("xero_token_payload_invalid")
        return tok


async def _fetch_connections(access_token: str) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=30) as client:
        rc = await client.get(_CONNECTIONS_URL, headers={"Authorization": f"Bearer {access_token}"})
        if rc.status_code >= 400:
            raise RuntimeError(f"xero_connections_fetch_failed:{rc.status_code}:{rc.text[:200]}")
        data = rc.json()
        return data if isinstance(data, list) else []


def build_connect_url(*, user_id: str) -> dict[str, str]:
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    _STATE_CACHE[state] = {
        "mode": "linked",
        "user_id": user_id,
        "verifier": verifier,
        "created_at": int(time.time()),
    }

    params = {
        "response_type": "code",
        "client_id": _client_id(),
        "redirect_uri": _redirect_uri(),
        "scope": _default_scope(),
        "state": state,
        "code_challenge": _code_challenge(verifier),
        "code_challenge_method": "S256",
    }
    return {
        "state": state,
        "url": _AUTH_BASE + "?" + urllib.parse.urlencode(params),
    }


def build_public_connect_url() -> dict[str, str]:
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    _STATE_CACHE[state] = {
        "mode": "public",
        "verifier": verifier,
        "created_at": int(time.time()),
    }

    params = {
        "response_type": "code",
        "client_id": _client_id(),
        "redirect_uri": _redirect_uri(),
        "scope": _public_auth_scope(),
        "state": state,
        "code_challenge": _code_challenge(verifier),
        "code_challenge_method": "S256",
    }
    return {
        "state": state,
        "url": _AUTH_BASE + "?" + urllib.parse.urlencode(params),
    }


async def exchange_code(*, code: str, state: str) -> dict[str, Any]:
    st = _STATE_CACHE.get(state)
    if not st:
        raise RuntimeError("xero_invalid_state")

    verifier = st["verifier"]
    user_id = st["user_id"]
    tok = await _exchange_token_payload(code=code, verifier=verifier)

    access_token = tok.get("access_token")
    refresh_token = tok.get("refresh_token")
    token_type = tok.get("token_type")
    scope = tok.get("scope")
    expires_in = int(tok.get("expires_in", 1800) or 1800)
    if not access_token or not refresh_token:
        raise RuntimeError("xero_token_payload_invalid")

    conns = await _fetch_connections(str(access_token))
    if not conns:
        raise RuntimeError("xero_no_tenant_connection")
    tenant_id = conns[0].get("tenantId")
    if not tenant_id:
        raise RuntimeError("xero_missing_tenant_id")

    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()
    upsert_xero_connection(
        user_id,
        tenant_id=tenant_id,
        access_token=access_token,
        refresh_token=refresh_token,
        token_type=token_type,
        scope=scope,
        expires_at=expires_at,
    )
    _STATE_CACHE.pop(state, None)

    return {
        "user_id": user_id,
        "tenant_id": tenant_id,
        "scope": scope,
        "expires_at": expires_at,
    }


async def exchange_code_public(*, code: str, state: str) -> dict[str, Any]:
    st = _STATE_CACHE.get(state)
    if not st or st.get("mode") != "public":
        raise RuntimeError("xero_invalid_state")

    verifier = st["verifier"]
    tok = await _exchange_token_payload(code=code, verifier=verifier)

    access_token = str(tok.get("access_token") or "")
    refresh_token = str(tok.get("refresh_token") or "")
    token_type = tok.get("token_type")
    scope = tok.get("scope")
    expires_in = int(tok.get("expires_in", 1800) or 1800)
    id_token = str(tok.get("id_token") or "")
    if not access_token or not refresh_token:
        raise RuntimeError("xero_token_payload_invalid")

    claims = _parse_jwt_payload_unverified(id_token) if id_token else {}
    provider_subject_id = str(claims.get("sub") or "").strip()
    provider_email = str(claims.get("email") or "").strip() or None
    full_name = (
        str(claims.get("name") or "").strip()
        or str(claims.get("preferred_username") or "").strip()
        or None
    )

    conns = await _fetch_connections(access_token)
    tenant_id = None
    tenant_name = None
    if conns:
        tenant_id = conns[0].get("tenantId")
        tenant_name = conns[0].get("tenantName")

    if not provider_subject_id:
        raise RuntimeError("xero_identity_missing_sub")

    _STATE_CACHE.pop(state, None)
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()

    return {
        "provider": "xero",
        "provider_subject_id": provider_subject_id,
        "provider_email": provider_email,
        "full_name": full_name,
        "tenant_id": tenant_id,
        "tenant_name": tenant_name,
        "scope": scope,
        "expires_at": expires_at,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": token_type,
    }


def get_connection_status(*, user_id: str) -> dict[str, Any]:
    row = get_xero_connection(user_id)
    if not row:
        return {"connected": False}
    return {
        "connected": True,
        "tenant_id": row.get("tenant_id"),
        "scope": row.get("scope"),
        "expires_at": row.get("expires_at"),
    }


def _is_expiring_soon(expires_at_iso: Optional[str], seconds: int = 120) -> bool:
    if not expires_at_iso:
        return True
    try:
        dt = datetime.fromisoformat(str(expires_at_iso).replace("Z", "+00:00"))
    except Exception:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt <= (datetime.now(timezone.utc) + timedelta(seconds=seconds))


async def _refresh_access_token_if_needed(*, user_id: str, force: bool = False) -> dict[str, Any]:
    # Central token lifecycle guard used by ALL write paths.
    # If token is close to expiry, refresh first so front-end upload buttons
    # remain "single click" instead of randomly failing with 401 mid-action.
    row = get_xero_connection(user_id)
    if not row:
        raise RuntimeError("xero_not_connected")

    if not force and not _is_expiring_soon(row.get("expires_at")):
        return row

    refresh_token = row.get("refresh_token")
    if not refresh_token:
        raise RuntimeError("xero_missing_refresh_token")

    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    headers = {
        "Authorization": _basic_auth_header(),
        "Content-Type": "application/x-www-form-urlencoded",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(_TOKEN_URL, data=data, headers=headers)
        if r.status_code >= 400:
            raise RuntimeError(f"xero_token_refresh_failed:{r.status_code}:{r.text[:200]}")
        tok = r.json()
        access_token = tok.get("access_token")
        new_refresh_token = tok.get("refresh_token") or refresh_token
        token_type = tok.get("token_type")
        scope = tok.get("scope")
        expires_in = int(tok.get("expires_in", 1800) or 1800)
        if not access_token:
            raise RuntimeError("xero_token_refresh_payload_invalid")

        conns = await _list_connections_with_access_token(access_token)
        tenant_id = row.get("tenant_id")
        if conns:
            candidate_ids = {str(c.get("tenantId")) for c in conns if c.get("tenantId")}
            if tenant_id not in candidate_ids:
                tenant_id = str(conns[0].get("tenantId"))

    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()
    upsert_xero_connection(
        user_id,
        tenant_id=str(tenant_id),
        access_token=str(access_token),
        refresh_token=str(new_refresh_token),
        token_type=token_type,
        scope=scope,
        expires_at=expires_at,
    )
    refreshed = get_xero_connection(user_id)
    if not refreshed:
        raise RuntimeError("xero_connection_refresh_readback_failed")
    return refreshed


async def _list_connections_with_access_token(access_token: str) -> list[dict[str, Any]]:
    if not access_token:
        return []
    async with httpx.AsyncClient(timeout=20) as client:
        rc = await client.get(_CONNECTIONS_URL, headers={"Authorization": f"Bearer {access_token}"})
        if rc.status_code >= 400:
            return []
        data = rc.json()
        if isinstance(data, list):
            return [dict(x) for x in data if isinstance(x, dict)]
        return []


async def get_connection_status_live(*, user_id: str) -> dict[str, Any]:
    # Read endpoint for frontend connection panel. Returns active tenant and
    # selectable tenant list so UI can render a company switcher.
    row = get_xero_connection(user_id)
    if not row:
        return {"connected": False}
    try:
        row = await _refresh_access_token_if_needed(user_id=user_id)
    except Exception:
        pass

    tenants = await _list_connections_with_access_token(str(row.get("access_token") or ""))
    return {
        "connected": True,
        "tenant_id": row.get("tenant_id"),
        "scope": row.get("scope"),
        "expires_at": row.get("expires_at"),
        "tenants": tenants,
    }


def set_active_tenant(*, user_id: str, tenant_id: str) -> dict[str, Any]:
    # Update only the tenant pointer; keep tokens unchanged.
    # This supports "switch company" without forcing re-auth.
    row = get_xero_connection(user_id)
    if not row:
        raise RuntimeError("xero_not_connected")
    upsert_xero_connection(
        user_id,
        tenant_id=tenant_id,
        access_token=str(row.get("access_token") or ""),
        refresh_token=str(row.get("refresh_token") or ""),
        token_type=row.get("token_type"),
        scope=row.get("scope"),
        expires_at=str(row.get("expires_at") or ""),
    )
    return get_connection_status(user_id=user_id)


async def create_draft_invoice(*, user_id: str, draft_payload: dict[str, Any]) -> dict[str, Any]:
    # Upload path contract:
    # 1) try with current token (after proactive refresh)
    # 2) if Xero still returns 401, force one refresh + single retry
    # 3) bubble explicit error for frontend to trigger re-auth modal
    row = await _refresh_access_token_if_needed(user_id=user_id)

    access_token = row.get("access_token")
    tenant_id = row.get("tenant_id")
    if not access_token or not tenant_id:
        raise RuntimeError("xero_connection_invalid")

    body = {"Invoices": [draft_payload]}
    headers = {
        "Authorization": f"Bearer {access_token}",
        "xero-tenant-id": tenant_id,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(_INVOICES_URL, json=body, headers=headers)
        if r.status_code == 401:
            # one retry after forced refresh
            row = await _refresh_access_token_if_needed(user_id=user_id, force=True)
            headers["Authorization"] = f"Bearer {row.get('access_token')}"
            headers["xero-tenant-id"] = str(row.get("tenant_id") or tenant_id)
            r = await client.post(_INVOICES_URL, json=body, headers=headers)
        if r.status_code >= 400:
            raise RuntimeError(f"xero_create_draft_failed:{r.status_code}:{r.text[:300]}")
        return r.json()
