from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Lock


PLAN_CATALOG = {
    "trial": {"price_aud": 0.0, "invoice_limit": 20, "period_days": 7},
    "starter_9_9": {"price_aud": 9.9, "invoice_limit": 100, "period_days": 30},
    "growth_19_9": {"price_aud": 19.9, "invoice_limit": 250, "period_days": 30},
    "pro_39_9": {"price_aud": 39.9, "invoice_limit": 500, "period_days": 30},
}


@dataclass
class AccountState:
    user_id: str
    plan_id: str
    period_start: datetime
    period_end: datetime
    quota_total: int
    used_invoices: int = 0

    @property
    def remaining_invoices(self) -> int:
        return max(0, self.quota_total - self.used_invoices)


_STORE: dict[str, AccountState] = {}
_LOCK = Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_state(user_id: str, plan_id: str) -> AccountState:
    plan = PLAN_CATALOG[plan_id]
    start = _now()
    end = start + timedelta(days=plan["period_days"])
    return AccountState(
        user_id=user_id,
        plan_id=plan_id,
        period_start=start,
        period_end=end,
        quota_total=plan["invoice_limit"],
        used_invoices=0,
    )


def _maybe_rollover(state: AccountState) -> AccountState:
    if _now() <= state.period_end:
        return state
    # 到期自动续当前plan并清零额度（测试阶段）
    return _new_state(state.user_id, state.plan_id)


def get_account(user_id: str) -> AccountState:
    with _LOCK:
        st = _STORE.get(user_id)
        if st is None:
            st = _new_state(user_id, "trial")
            _STORE[user_id] = st
        st = _maybe_rollover(st)
        _STORE[user_id] = st
        return st


def set_plan(user_id: str, plan_id: str) -> AccountState:
    if plan_id not in PLAN_CATALOG:
        raise ValueError(f"unknown_plan: {plan_id}")
    with _LOCK:
        st = _new_state(user_id, plan_id)
        _STORE[user_id] = st
        return st


def can_consume(user_id: str, count: int) -> bool:
    st = get_account(user_id)
    return st.remaining_invoices >= count


def consume_invoices(user_id: str, count: int) -> AccountState:
    if count <= 0:
        return get_account(user_id)
    with _LOCK:
        st = _STORE.get(user_id) or _new_state(user_id, "trial")
        st = _maybe_rollover(st)
        if st.remaining_invoices < count:
            raise ValueError("quota_exceeded")
        st.used_invoices += count
        _STORE[user_id] = st
        return st


def account_snapshot(user_id: str) -> dict:
    st = get_account(user_id)
    plan = PLAN_CATALOG[st.plan_id]
    return {
        "user_id": st.user_id,
        "plan_id": st.plan_id,
        "price_aud": plan["price_aud"],
        "invoice_limit": st.quota_total,
        "used_invoices": st.used_invoices,
        "remaining_invoices": st.remaining_invoices,
        "period_start": st.period_start.isoformat(),
        "period_end": st.period_end.isoformat(),
    }
