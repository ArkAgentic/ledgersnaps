from __future__ import annotations

import os
from typing import Any, Optional

from .store import (
    dequeue_next_job,
    get_job_owned,
    get_job_result_owned,
    mark_job_queue_done,
    set_job_status_owned,
    upsert_job_result_owned,
)


def _worker_id() -> str:
    return os.getenv("WORKER_ID", "local-worker-1")


def process_one_queued_job() -> Optional[dict[str, Any]]:
    """Lightweight local worker step.

    Claim one queued job, mark running, and write a deterministic placeholder result.
    This validates end-to-end queue/status/isolation wiring before external queue/worker rollout.
    """
    item = dequeue_next_job(_worker_id())
    if not item:
        return None

    tenant_id = item["tenant_id"]
    user_id = item["user_id"]
    job_id = item["job_id"]

    job = get_job_owned(tenant_id=tenant_id, user_id=user_id, job_id=job_id)
    if not job:
        mark_job_queue_done(job_id, "failed")
        return {"job_id": job_id, "status": "failed", "reason": "job_not_found"}

    set_job_status_owned(tenant_id, user_id, job_id, "running")

    # Placeholder result envelope for async pipeline handshake.
    result_payload: dict[str, Any] = {
        "kind": "queued-worker-placeholder",
        "job_id": job_id,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "payload": item.get("payload", {}),
        "note": "Replace with real extraction execution in next phase",
    }

    upsert_job_result_owned(
        tenant_id=tenant_id,
        user_id=user_id,
        job_id=job_id,
        status="completed",
        result=result_payload,
    )
    set_job_status_owned(tenant_id, user_id, job_id, "completed")
    mark_job_queue_done(job_id, "completed")

    row = get_job_result_owned(tenant_id=tenant_id, user_id=user_id, job_id=job_id)
    return {
        "job_id": job_id,
        "status": "completed",
        "has_result": bool(row),
    }
