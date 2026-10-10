from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Lock
import os
import sqlite3
from typing import Optional

try:
    import psycopg
    from psycopg.rows import dict_row
except Exception:  # pragma: no cover
    psycopg = None
    dict_row = None

# Paid plans (AUD)
PLAN_CATALOG = {
    "starter_14_95": {"price_aud": 14.95, "invoice_limit": 100, "period_days": 30},
    "pro_29_95": {"price_aud": 29.95, "invoice_limit": 300, "period_days": 30},
}

TRIAL_DAYS = 7
TRIAL_INVOICE_LIMIT = 10
TOPUP_PACK_SIZE = 50
TOPUP_PACK_PRICE_AUD = 5.95


@dataclass
class AccountState:
    user_id: str
    trial_started_at: datetime
    trial_ends_at: datetime
    trial_total: int = TRIAL_INVOICE_LIMIT
    trial_used: int = 0
    plan_id: str | None = None
    plan_period_start: datetime | None = None
    plan_period_end: datetime | None = None
    plan_quota_total: int = 0
    plan_used: int = 0
    topup_total: int = 0
    topup_used: int = 0

    @property
    def trial_active(self) -> bool:
        return _now() <= self.trial_ends_at

    @property
    def trial_remaining(self) -> int:
        if not self.trial_active:
            return 0
        return max(0, self.trial_total - self.trial_used)

    @property
    def plan_remaining(self) -> int:
        if not self.plan_id:
            return 0
        if self.plan_period_end and _now() > self.plan_period_end:
            return 0
        return max(0, self.plan_quota_total - self.plan_used)

    @property
    def topup_remaining(self) -> int:
        return max(0, self.topup_total - self.topup_used)

    @property
    def remaining_invoices(self) -> int:
        return self.trial_remaining + self.plan_remaining + self.topup_remaining


_LOCK = Lock()
_SQLITE_CONN: Optional[sqlite3.Connection] = None
_PG_CONN: Optional[psycopg.Connection] = None
_SCHEMA_READY = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.isoformat()


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


def _sqlite_path() -> str:
    return os.getenv("LEDGERSNAPS_DB_PATH", os.path.join(os.getcwd(), ".data", "ledgersnaps.sqlite3"))


def _pg_dsn() -> Optional[str]:
    dsn = (os.getenv("LEDGERSNAPS_PG_DSN") or os.getenv("DATABASE_URL") or "").strip()
    return dsn or None


def _pg_enabled() -> bool:
    return (_pg_dsn() is not None) and (psycopg is not None)


def _ensure_schema() -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return

    if _pg_enabled():
        conn = _ensure_pg_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS account_states (
                  user_id TEXT PRIMARY KEY,
                  trial_started_at TEXT NOT NULL,
                  trial_ends_at TEXT NOT NULL,
                  trial_total INTEGER NOT NULL,
                  trial_used INTEGER NOT NULL,
                  plan_id TEXT,
                  plan_period_start TEXT,
                  plan_period_end TEXT,
                  plan_quota_total INTEGER NOT NULL,
                  plan_used INTEGER NOT NULL,
                  topup_total INTEGER NOT NULL,
                  topup_used INTEGER NOT NULL,
                  updated_at TEXT NOT NULL
                )
                """
            )
        conn.commit()
    else:
        conn = _ensure_sqlite_conn()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS account_states (
              user_id TEXT PRIMARY KEY,
              trial_started_at TEXT NOT NULL,
              trial_ends_at TEXT NOT NULL,
              trial_total INTEGER NOT NULL,
              trial_used INTEGER NOT NULL,
              plan_id TEXT,
              plan_period_start TEXT,
              plan_period_end TEXT,
              plan_quota_total INTEGER NOT NULL,
              plan_used INTEGER NOT NULL,
              topup_total INTEGER NOT NULL,
              topup_used INTEGER NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()

    _SCHEMA_READY = True


def _ensure_pg_conn() -> psycopg.Connection:
    global _PG_CONN
    if _PG_CONN is not None and not _PG_CONN.closed:
        return _PG_CONN
    dsn = _pg_dsn()
    if not dsn:
        raise RuntimeError("postgres_dsn_missing")
    if psycopg is None or dict_row is None:
        raise RuntimeError("psycopg_not_installed")
    _PG_CONN = psycopg.connect(dsn, autocommit=False, row_factory=dict_row)
    return _PG_CONN


def _ensure_sqlite_conn() -> sqlite3.Connection:
    global _SQLITE_CONN
    if _SQLITE_CONN is not None:
        return _SQLITE_CONN
    path = _sqlite_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _SQLITE_CONN = conn
    return conn


def _new_state(user_id: str) -> AccountState:
    start = _now()
    return AccountState(
        user_id=user_id,
        trial_started_at=start,
        trial_ends_at=start + timedelta(days=TRIAL_DAYS),
    )


def _row_to_state(row: dict) -> AccountState:
    return AccountState(
        user_id=str(row.get("user_id") or ""),
        trial_started_at=_parse_dt(row.get("trial_started_at")) or _now(),
        trial_ends_at=_parse_dt(row.get("trial_ends_at")) or (_now() + timedelta(days=TRIAL_DAYS)),
        trial_total=int(row.get("trial_total") or TRIAL_INVOICE_LIMIT),
        trial_used=int(row.get("trial_used") or 0),
        plan_id=(str(row.get("plan_id")) if row.get("plan_id") else None),
        plan_period_start=_parse_dt(row.get("plan_period_start")),
        plan_period_end=_parse_dt(row.get("plan_period_end")),
        plan_quota_total=int(row.get("plan_quota_total") or 0),
        plan_used=int(row.get("plan_used") or 0),
        topup_total=int(row.get("topup_total") or 0),
        topup_used=int(row.get("topup_used") or 0),
    )


def _save_state(st: AccountState) -> None:
    _ensure_schema()
    now = _iso(_now())
    if _pg_enabled():
        conn = _ensure_pg_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO account_states(
                  user_id, trial_started_at, trial_ends_at, trial_total, trial_used,
                  plan_id, plan_period_start, plan_period_end, plan_quota_total, plan_used,
                  topup_total, topup_used, updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(user_id) DO UPDATE SET
                  trial_started_at=excluded.trial_started_at,
                  trial_ends_at=excluded.trial_ends_at,
                  trial_total=excluded.trial_total,
                  trial_used=excluded.trial_used,
                  plan_id=excluded.plan_id,
                  plan_period_start=excluded.plan_period_start,
                  plan_period_end=excluded.plan_period_end,
                  plan_quota_total=excluded.plan_quota_total,
                  plan_used=excluded.plan_used,
                  topup_total=excluded.topup_total,
                  topup_used=excluded.topup_used,
                  updated_at=excluded.updated_at
                """,
                (
                    st.user_id,
                    _iso(st.trial_started_at),
                    _iso(st.trial_ends_at),
                    st.trial_total,
                    st.trial_used,
                    st.plan_id,
                    _iso(st.plan_period_start),
                    _iso(st.plan_period_end),
                    st.plan_quota_total,
                    st.plan_used,
                    st.topup_total,
                    st.topup_used,
                    now,
                ),
            )
        conn.commit()
        return

    conn = _ensure_sqlite_conn()
    conn.execute(
        """
        INSERT INTO account_states(
          user_id, trial_started_at, trial_ends_at, trial_total, trial_used,
          plan_id, plan_period_start, plan_period_end, plan_quota_total, plan_used,
          topup_total, topup_used, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
          trial_started_at=excluded.trial_started_at,
          trial_ends_at=excluded.trial_ends_at,
          trial_total=excluded.trial_total,
          trial_used=excluded.trial_used,
          plan_id=excluded.plan_id,
          plan_period_start=excluded.plan_period_start,
          plan_period_end=excluded.plan_period_end,
          plan_quota_total=excluded.plan_quota_total,
          plan_used=excluded.plan_used,
          topup_total=excluded.topup_total,
          topup_used=excluded.topup_used,
          updated_at=excluded.updated_at
        """,
        (
            st.user_id,
            _iso(st.trial_started_at),
            _iso(st.trial_ends_at),
            st.trial_total,
            st.trial_used,
            st.plan_id,
            _iso(st.plan_period_start),
            _iso(st.plan_period_end),
            st.plan_quota_total,
            st.plan_used,
            st.topup_total,
            st.topup_used,
            now,
        ),
    )
    conn.commit()


def _load_state(user_id: str) -> AccountState:
    _ensure_schema()
    row = None

    if _pg_enabled():
        conn = _ensure_pg_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM account_states WHERE user_id=%s LIMIT 1", (user_id,))
            row = cur.fetchone()
        if row:
            return _row_to_state(dict(row))
    else:
        conn = _ensure_sqlite_conn()
        cur = conn.execute("SELECT * FROM account_states WHERE user_id=? LIMIT 1", (user_id,))
        rr = cur.fetchone()
        if rr:
            return _row_to_state(dict(rr))

    st = _new_state(user_id)
    _save_state(st)
    return st


def _maybe_rollover_plan(state: AccountState) -> AccountState:
    if not state.plan_id or not state.plan_period_end:
        return state
    if _now() <= state.plan_period_end:
        return state
    plan = PLAN_CATALOG[state.plan_id]
    start = _now()
    state.plan_period_start = start
    state.plan_period_end = start + timedelta(days=plan["period_days"])
    state.plan_quota_total = int(plan["invoice_limit"])
    state.plan_used = 0
    return state


def get_account(user_id: str) -> AccountState:
    with _LOCK:
        st = _load_state(user_id)
        st = _maybe_rollover_plan(st)
        _save_state(st)
        return st


def set_plan(user_id: str, plan_id: str) -> AccountState:
    if plan_id not in PLAN_CATALOG:
        raise ValueError(f"unknown_plan: {plan_id}")
    plan = PLAN_CATALOG[plan_id]
    with _LOCK:
        st = _load_state(user_id)
        start = _now()
        st.plan_id = plan_id
        st.plan_period_start = start
        st.plan_period_end = start + timedelta(days=plan["period_days"])
        st.plan_quota_total = int(plan["invoice_limit"])
        st.plan_used = 0
        _save_state(st)
        return st


def add_topup(user_id: str, packs: int = 1) -> AccountState:
    if packs <= 0:
        raise ValueError("invalid_topup_packs")
    with _LOCK:
        st = _load_state(user_id)
        st = _maybe_rollover_plan(st)
        st.topup_total += int(packs) * TOPUP_PACK_SIZE
        _save_state(st)
        return st


def can_consume(user_id: str, count: int) -> bool:
    st = get_account(user_id)
    return st.remaining_invoices >= count


def consume_invoices(user_id: str, count: int) -> AccountState:
    if count <= 0:
        return get_account(user_id)
    with _LOCK:
        st = _load_state(user_id)
        st = _maybe_rollover_plan(st)
        remaining = int(count)

        take_trial = min(st.trial_remaining, remaining)
        st.trial_used += take_trial
        remaining -= take_trial

        take_plan = min(st.plan_remaining, remaining)
        st.plan_used += take_plan
        remaining -= take_plan

        take_topup = min(st.topup_remaining, remaining)
        st.topup_used += take_topup
        remaining -= take_topup

        if remaining > 0:
            raise ValueError("quota_exceeded")

        _save_state(st)
        return st


def account_snapshot(user_id: str) -> dict:
    st = get_account(user_id)
    plan_price = PLAN_CATALOG[st.plan_id]["price_aud"] if st.plan_id else 0.0
    return {
        "user_id": st.user_id,
        "plan_id": st.plan_id or "none",
        "price_aud": plan_price,
        "trial": {
            "started_at": st.trial_started_at.isoformat(),
            "ends_at": st.trial_ends_at.isoformat(),
            "active": st.trial_active,
            "invoice_limit": st.trial_total,
            "used_invoices": st.trial_used,
            "remaining_invoices": st.trial_remaining,
        },
        "plan": {
            "period_start": st.plan_period_start.isoformat() if st.plan_period_start else None,
            "period_end": st.plan_period_end.isoformat() if st.plan_period_end else None,
            "invoice_limit": st.plan_quota_total,
            "used_invoices": st.plan_used,
            "remaining_invoices": st.plan_remaining,
        },
        "topup": {
            "pack_size": TOPUP_PACK_SIZE,
            "pack_price_aud": TOPUP_PACK_PRICE_AUD,
            "total_invoices": st.topup_total,
            "used_invoices": st.topup_used,
            "remaining_invoices": st.topup_remaining,
        },
        "remaining_invoices": st.remaining_invoices,
    }
