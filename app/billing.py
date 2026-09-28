from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Lock

# Paid plans (AUD)
PLAN_CATALOG = {
    "starter_14_95": {"price_aud": 14.95, "invoice_limit": 100, "period_days": 30},
    "pro_29_95": {"price_aud": 29.95, "invoice_limit": 300, "period_days": 30},
}

TRIAL_DAYS = 7
TRIAL_INVOICE_LIMIT = 15
TOPUP_PACK_SIZE = 50
TOPUP_PACK_PRICE_AUD = 5.95


@dataclass
class AccountState:
    user_id: str

    # one-off trial entitlement
    trial_started_at: datetime
    trial_ends_at: datetime
    trial_total: int = TRIAL_INVOICE_LIMIT
    trial_used: int = 0

    # paid plan monthly quota
    plan_id: str | None = None
    plan_period_start: datetime | None = None
    plan_period_end: datetime | None = None
    plan_quota_total: int = 0
    plan_used: int = 0

    # add-on packs (carry until used)
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


_STORE: dict[str, AccountState] = {}
_LOCK = Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_state(user_id: str) -> AccountState:
    start = _now()
    return AccountState(
        user_id=user_id,
        trial_started_at=start,
        trial_ends_at=start + timedelta(days=TRIAL_DAYS),
    )


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
        st = _STORE.get(user_id)
        if st is None:
            st = _new_state(user_id)
            _STORE[user_id] = st
        st = _maybe_rollover_plan(st)
        _STORE[user_id] = st
        return st


def set_plan(user_id: str, plan_id: str) -> AccountState:
    if plan_id not in PLAN_CATALOG:
        raise ValueError(f"unknown_plan: {plan_id}")
    plan = PLAN_CATALOG[plan_id]
    with _LOCK:
        st = _STORE.get(user_id) or _new_state(user_id)
        start = _now()
        st.plan_id = plan_id
        st.plan_period_start = start
        st.plan_period_end = start + timedelta(days=plan["period_days"])
        st.plan_quota_total = int(plan["invoice_limit"])
        st.plan_used = 0
        _STORE[user_id] = st
        return st


def add_topup(user_id: str, packs: int = 1) -> AccountState:
    if packs <= 0:
        raise ValueError("invalid_topup_packs")
    with _LOCK:
        st = _STORE.get(user_id) or _new_state(user_id)
        st = _maybe_rollover_plan(st)
        st.topup_total += int(packs) * TOPUP_PACK_SIZE
        _STORE[user_id] = st
        return st


def can_consume(user_id: str, count: int) -> bool:
    st = get_account(user_id)
    return st.remaining_invoices >= count


def consume_invoices(user_id: str, count: int) -> AccountState:
    if count <= 0:
        return get_account(user_id)
    with _LOCK:
        st = _STORE.get(user_id) or _new_state(user_id)
        st = _maybe_rollover_plan(st)
        remaining = int(count)

        # Priority: trial -> paid plan -> topup
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

        _STORE[user_id] = st
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
