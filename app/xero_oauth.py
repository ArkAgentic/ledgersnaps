from __future__ import annotations

import base64
import hashlib
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


def _basic_auth_header() -> str:
    raw = f"{_client_id()}:{_client_secret()}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _code_challenge(verifier: str) -> str:
    h = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(h).decode().rstrip("=")


def build_connect_url(*, user_id: str) -> dict[str, str]:
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    _STATE_CACHE[state] = {"user_id": user_id, "verifier": verifier, "created_at": int(time.time())}

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


async def exchange_code(*, code: str, state: str) -> dict[str, Any]:
    st = _STATE_CACHE.get(state)
    if not st:
        raise RuntimeError("xero_invalid_state")

    verifier = st["verifier"]
    user_id = st["user_id"]
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

        access_token = tok.get("access_token")
        refresh_token = tok.get("refresh_token")
        token_type = tok.get("token_type")
        scope = tok.get("scope")
        expires_in = int(tok.get("expires_in", 1800) or 1800)
        if not access_token or not refresh_token:
            raise RuntimeError("xero_token_payload_invalid")

        rc = await client.get(_CONNECTIONS_URL, headers={"Authorization": f"Bearer {access_token}"})
        if rc.status_code >= 400:
            raise RuntimeError(f"xero_connections_fetch_failed:{rc.status_code}:{rc.text[:200]}")
        conns = rc.json() if isinstance(rc.json(), list) else []
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


async def create_draft_invoice(*, user_id: str, draft_payload: dict[str, Any]) -> dict[str, Any]:
    row = get_xero_connection(user_id)
    if not row:
        raise RuntimeError("xero_not_connected")

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
        if r.status_code >= 400:
            raise RuntimeError(f"xero_create_draft_failed:{r.status_code}:{r.text[:300]}")
        return r.json()
