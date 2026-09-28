from __future__ import annotations

import asyncio
import base64
import os
from io import BytesIO
from pathlib import Path
from typing import Any, Literal, Optional, cast

from fastapi import UploadFile

from .billing import can_consume, consume_invoices
from .main import _extract_and_map_core, _extract_and_map_from_bytes
from .schemas import BatchExtractAndMapResponse, ExtractAndMapChunkResult
from .splitter import split_pdf_auto
from .storage_backend import StorageBackend
from .config import settings
from .store import (
    dequeue_next_job,
    get_job_owned,
    get_job_result_owned,
    get_queue_attempts,
    mark_job_queue_done,
    mark_job_queue_failed,
    mark_job_queue_retry,
    set_job_extraction_metrics_owned,
    set_job_status_owned,
    upsert_job_result_owned,
)


def _worker_id() -> str:
    return os.getenv("WORKER_ID", "local-worker-1")


async def _extract_and_map_batch_core_worker(
    target: Literal["xero", "myob"],
    flow_mode: Literal["auto", "ap", "ar"],
    filename: str,
    content_type: str,
    file_bytes: bytes,
    user_id: str,
) -> BatchExtractAndMapResponse:
    if content_type.startswith("image/"):
        if not can_consume(user_id, 1):
            return BatchExtractAndMapResponse(
                split_decision="single",
                chunk_count=1,
                chunks=[],
                summary={"ok": 0, "failed": 1, "split_stats": {}, "billable_invoice_count": 0},
            )
        result = await _extract_and_map_from_bytes(
            target=target,
            flow_mode=flow_mode,
            filename=filename,
            content_type=content_type,
            file_bytes=file_bytes,
        )
        result.meta.trace = [
            "L0 split(batch) decision=single stats={}",
            "L0 split(batch) chunk_index=1/1",
            *result.meta.trace,
        ]
        consume_invoices(user_id, 1)
        return BatchExtractAndMapResponse(
            split_decision="single",
            chunk_count=1,
            chunks=[ExtractAndMapChunkResult(chunk_index=1, result=result)],
            summary={"ok": 1, "failed": 0, "split_stats": {}, "billable_invoice_count": 1},
        )

    split_chunks = [file_bytes]
    split_decision: Literal["single", "split"] = "single"
    split_stats: dict[str, Any] = {}
    if filename.lower().endswith(".pdf"):
        split_chunks, split_decision_raw, split_stats = split_pdf_auto(file_bytes)
        split_decision = cast(Literal["single", "split"], split_decision_raw)

    chunks: list[ExtractAndMapChunkResult] = []
    ok = 0
    failed = 0
    for i, chunk_bytes in enumerate(split_chunks, start=1):
        if not can_consume(user_id, 1):
            failed += (len(split_chunks) - i + 1)
            break
        uf = UploadFile(filename=f"{Path(filename).stem}__chunk{i}.pdf", file=BytesIO(chunk_bytes), headers=None)
        try:
            result = await _extract_and_map_core(target=target, flow_mode=flow_mode, file=uf)
            result.meta.trace = [
                f"L0 split(batch) decision={split_decision} stats={split_stats}",
                f"L0 split(batch) chunk_index={i}/{len(split_chunks)}",
                *result.meta.trace,
            ]
            chunks.append(ExtractAndMapChunkResult(chunk_index=i, result=result))
            ok += 1
            consume_invoices(user_id, 1)
        except Exception:
            failed += 1

    return BatchExtractAndMapResponse(
        split_decision=split_decision,
        chunk_count=len(split_chunks),
        chunks=chunks,
        summary={
            "ok": ok,
            "failed": failed,
            "split_stats": split_stats,
            "billable_invoice_count": ok,
        },
    )


async def _process_payload(item: dict[str, Any], finalize_queue: bool) -> dict[str, Any]:
    payload = item.get("payload", {})
    job_id = item["job_id"]
    tenant_id = item["tenant_id"]
    user_id = item["user_id"]

    # jobs_create currently has no input artifact yet; keep deterministic completion.
    source = str(payload.get("source", ""))
    if source == "jobs_create":
        result_payload: dict[str, Any] = {
            "kind": "queued-worker-placeholder",
            "job_id": job_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "payload": payload,
            "note": "Waiting for upload artifact wiring (Blob/SAS) before full extraction",
        }
        upsert_job_result_owned(
            tenant_id=tenant_id,
            user_id=user_id,
            job_id=job_id,
            status="completed",
            result=result_payload,
        )
        set_job_status_owned(tenant_id, user_id, job_id, "completed")
        if finalize_queue:
            mark_job_queue_done(job_id, "completed")
        row = get_job_result_owned(tenant_id=tenant_id, user_id=user_id, job_id=job_id)
        return {"job_id": job_id, "status": "completed", "has_result": bool(row), "mode": "placeholder"}

    target = str(payload.get("target", "xero"))
    flow_mode = str(payload.get("flow_mode", "auto"))
    filename = str(payload.get("filename", "upload.pdf"))
    content_type = str(payload.get("content_type", "application/pdf"))

    storage = StorageBackend(payload.get("artifact_backend"))
    artifact_ref = str(payload.get("artifact_ref", "") or "")
    if artifact_ref:
        file_bytes = storage.get_bytes(artifact_ref)
    else:
        file_data_b64 = payload.get("file_data_b64")
        if file_data_b64:
            file_bytes = base64.b64decode(file_data_b64)
        else:
            raise RuntimeError("missing_input_artifact")

    try:
        br = await _extract_and_map_batch_core_worker(
            target=cast(Literal["xero", "myob"], target),
            flow_mode=cast(Literal["auto", "ap", "ar"], flow_mode),
            filename=filename,
            content_type=content_type,
            file_bytes=file_bytes,
            user_id=user_id,
        )

        result_payload = {
            "kind": "worker-batch-result",
            "job_id": job_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "batch_result": br.model_dump(),
        }
        extracted_count = int(br.summary.get("ok", 0) or 0)
        total_amount = 0.0
        for ch in br.chunks:
            try:
                v = ch.result.invoice.total
                if v is not None:
                    total_amount += float(v)
            except Exception:
                pass

        upsert_job_result_owned(
            tenant_id=tenant_id,
            user_id=user_id,
            job_id=job_id,
            status="completed",
            result=result_payload,
        )
        set_job_extraction_metrics_owned(
            tenant_id=tenant_id,
            user_id=user_id,
            job_id=job_id,
            invoice_extracted_count=extracted_count,
            extracted_total_amount=round(total_amount, 2),
        )
        set_job_status_owned(tenant_id, user_id, job_id, "completed")
        if finalize_queue:
            mark_job_queue_done(job_id, "completed")

        row = get_job_result_owned(tenant_id=tenant_id, user_id=user_id, job_id=job_id)
        return {
            "job_id": job_id,
            "status": "completed",
            "has_result": bool(row),
            "ok_chunks": br.summary.get("ok", 0),
            "mode": "extract",
        }
    finally:
        if artifact_ref:
            storage.delete(artifact_ref)


def process_claimed_item(item: dict[str, Any], finalize_queue: bool = True) -> dict[str, Any]:
    tenant_id = item["tenant_id"]
    user_id = item["user_id"]
    job_id = item["job_id"]

    job = get_job_owned(tenant_id=tenant_id, user_id=user_id, job_id=job_id)
    if not job:
        if finalize_queue:
            mark_job_queue_done(job_id, "failed")
        return {"job_id": job_id, "status": "failed", "reason": "job_not_found"}

    set_job_status_owned(tenant_id, user_id, job_id, "running")

    try:
        return asyncio.run(_process_payload(item, finalize_queue=finalize_queue))
    except Exception as e:  # noqa: BLE001
        attempts = get_queue_attempts(job_id)
        upsert_job_result_owned(
            tenant_id=tenant_id,
            user_id=user_id,
            job_id=job_id,
            status="failed",
            result={"error": str(e), "job_id": job_id, "attempts": attempts},
        )
        set_job_status_owned(tenant_id, user_id, job_id, "failed")
        if finalize_queue:
            if attempts >= int(settings.max_job_attempts):
                mark_job_queue_failed(job_id, error=str(e))
            else:
                mark_job_queue_retry(job_id, error=str(e), backoff_seconds=int(settings.retry_backoff_seconds))
        return {"job_id": job_id, "status": "failed", "reason": str(e), "attempts": attempts}


def process_one_queued_job() -> Optional[dict[str, Any]]:
    item = dequeue_next_job(_worker_id())
    if not item:
        return None
    return process_claimed_item(item, finalize_queue=True)
