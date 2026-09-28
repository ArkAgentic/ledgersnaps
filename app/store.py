from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

_DB_LOCK = threading.Lock()
_CONN: Optional[sqlite3.Connection] = None


def _db_path() -> str:
    return os.getenv("LEDGERSNAPS_DB_PATH", os.path.join(os.getcwd(), ".data", "ledgersnaps.sqlite3"))


def _ensure_conn() -> sqlite3.Connection:
    global _CONN
    if _CONN is not None:
        return _CONN
    path = _db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _CONN = conn
    init_db()
    return conn


def init_db() -> None:
    conn = _CONN or sqlite3.connect(_db_path(), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    with _DB_LOCK:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
              job_id TEXT PRIMARY KEY,
              tenant_id TEXT NOT NULL,
              user_id TEXT NOT NULL,
              status TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              file_count INTEGER NOT NULL DEFAULT 0,
              invoice_estimated INTEGER NOT NULL DEFAULT 0,
              metadata_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        jobs_cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        if "invoice_extracted_count" not in jobs_cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN invoice_extracted_count INTEGER NOT NULL DEFAULT 0")
        if "extracted_total_amount" not in jobs_cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN extracted_total_amount REAL NOT NULL DEFAULT 0")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_results (
              job_id TEXT PRIMARY KEY,
              tenant_id TEXT NOT NULL,
              user_id TEXT NOT NULL,
              status TEXT NOT NULL,
              result_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(job_id) REFERENCES jobs(job_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_queue (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              job_id TEXT NOT NULL,
              tenant_id TEXT NOT NULL,
              user_id TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'queued',
              attempts INTEGER NOT NULL DEFAULT 0,
              available_at TEXT NOT NULL,
              locked_at TEXT,
              worker_id TEXT,
              payload_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(job_id)
            )
            """
        )
        cols = {r[1] for r in conn.execute("PRAGMA table_info(job_queue)").fetchall()}
        if "last_error" not in cols:
            conn.execute("ALTER TABLE job_queue ADD COLUMN last_error TEXT")
        if "last_error_at" not in cols:
            conn.execute("ALTER TABLE job_queue ADD COLUMN last_error_at TEXT")
        if "last_attempt_at" not in cols:
            conn.execute("ALTER TABLE job_queue ADD COLUMN last_attempt_at TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_owner_created ON jobs(tenant_id, user_id, created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_owner_status ON jobs(tenant_id, user_id, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_job_results_owner ON job_results(tenant_id, user_id, updated_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_job_queue_status_available ON job_queue(status, available_at)")
        conn.commit()
    if _CONN is None:
        conn.close()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_job(tenant_id: str, user_id: str, *, file_count: int = 0, invoice_estimated: int = 0, metadata_json: str = "{}") -> dict[str, Any]:
    conn = _ensure_conn()
    now = _now_iso()
    job_id = str(uuid.uuid4())
    with _DB_LOCK:
        conn.execute(
            """
            INSERT INTO jobs(job_id, tenant_id, user_id, status, created_at, updated_at, file_count, invoice_estimated, metadata_json)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (job_id, tenant_id, user_id, "created", now, now, int(file_count), int(invoice_estimated), metadata_json),
        )
        conn.commit()
    job = get_job_owned(tenant_id=tenant_id, user_id=user_id, job_id=job_id)
    if not job:
        raise RuntimeError("job_create_readback_failed")
    return job


def update_job_submission_owned(
    tenant_id: str,
    user_id: str,
    job_id: str,
    *,
    file_count: int,
    invoice_estimated: int,
    metadata: dict[str, Any],
) -> bool:
    conn = _ensure_conn()
    now = _now_iso()
    metadata_json = json.dumps(metadata, ensure_ascii=False)
    with _DB_LOCK:
        cur = conn.execute(
            """
            UPDATE jobs
            SET file_count=?, invoice_estimated=?, metadata_json=?, updated_at=?
            WHERE tenant_id=? AND user_id=? AND job_id=?
            """,
            (int(file_count), int(invoice_estimated), metadata_json, now, tenant_id, user_id, job_id),
        )
        conn.commit()
    return cur.rowcount > 0


def set_job_status_owned(tenant_id: str, user_id: str, job_id: str, status: str) -> bool:
    conn = _ensure_conn()
    now = _now_iso()
    with _DB_LOCK:
        cur = conn.execute(
            """
            UPDATE jobs
            SET status=?, updated_at=?
            WHERE tenant_id=? AND user_id=? AND job_id=?
            """,
            (status, now, tenant_id, user_id, job_id),
        )
        conn.commit()
    return cur.rowcount > 0


def set_job_extraction_metrics_owned(
    tenant_id: str,
    user_id: str,
    job_id: str,
    *,
    invoice_extracted_count: int,
    extracted_total_amount: float,
) -> bool:
    conn = _ensure_conn()
    now = _now_iso()
    with _DB_LOCK:
        cur = conn.execute(
            """
            UPDATE jobs
            SET invoice_extracted_count=?, extracted_total_amount=?, updated_at=?
            WHERE tenant_id=? AND user_id=? AND job_id=?
            """,
            (
                int(max(0, invoice_extracted_count)),
                float(max(0.0, extracted_total_amount)),
                now,
                tenant_id,
                user_id,
                job_id,
            ),
        )
        conn.commit()
    return cur.rowcount > 0


def upsert_job_result_owned(tenant_id: str, user_id: str, job_id: str, status: str, result: dict[str, Any]) -> None:
    conn = _ensure_conn()
    now = _now_iso()
    payload = json.dumps(result, ensure_ascii=False)
    with _DB_LOCK:
        conn.execute(
            """
            INSERT INTO job_results(job_id, tenant_id, user_id, status, result_json, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(job_id) DO UPDATE SET
              status=excluded.status,
              result_json=excluded.result_json,
              updated_at=excluded.updated_at
            """,
            (job_id, tenant_id, user_id, status, payload, now, now),
        )
        conn.commit()


def enqueue_job_owned(tenant_id: str, user_id: str, job_id: str, payload: dict[str, Any]) -> None:
    conn = _ensure_conn()
    now = _now_iso()
    payload_json = json.dumps(payload, ensure_ascii=False)
    with _DB_LOCK:
        conn.execute(
            """
            INSERT INTO job_queue(job_id, tenant_id, user_id, status, attempts, available_at, payload_json, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(job_id) DO UPDATE SET
              status='queued',
              available_at=excluded.available_at,
              payload_json=excluded.payload_json,
              updated_at=excluded.updated_at
            """,
            (job_id, tenant_id, user_id, "queued", 0, now, payload_json, now, now),
        )
        conn.commit()


def mark_job_queue_running(job_id: str, worker_id: str) -> None:
    conn = _ensure_conn()
    now = _now_iso()
    with _DB_LOCK:
        conn.execute(
            """
            UPDATE job_queue
            SET status='running', locked_at=?, worker_id=?, attempts=attempts+1, updated_at=?
            WHERE job_id=?
            """,
            (now, worker_id, now, job_id),
        )
        conn.commit()


def mark_job_queue_done(job_id: str, status: str) -> None:
    conn = _ensure_conn()
    now = _now_iso()
    with _DB_LOCK:
        conn.execute(
            """
            UPDATE job_queue
            SET status=?, updated_at=?
            WHERE job_id=?
            """,
            (status, now, job_id),
        )
        conn.commit()


def mark_job_queue_retry(job_id: str, *, error: str, backoff_seconds: int) -> None:
    conn = _ensure_conn()
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    available_at = (now_dt.timestamp() + max(1, int(backoff_seconds)))
    available_at_iso = datetime.fromtimestamp(available_at, tz=timezone.utc).isoformat()
    with _DB_LOCK:
        conn.execute(
            """
            UPDATE job_queue
            SET status='queued',
                available_at=?,
                last_error=?,
                last_error_at=?,
                last_attempt_at=?,
                updated_at=?
            WHERE job_id=?
            """,
            (available_at_iso, str(error)[:2000], now, now, now, job_id),
        )
        conn.commit()


def mark_job_queue_failed(job_id: str, *, error: str) -> None:
    conn = _ensure_conn()
    now = _now_iso()
    with _DB_LOCK:
        conn.execute(
            """
            UPDATE job_queue
            SET status='failed',
                last_error=?,
                last_error_at=?,
                last_attempt_at=?,
                updated_at=?
            WHERE job_id=?
            """,
            (str(error)[:2000], now, now, now, job_id),
        )
        conn.commit()


def list_queue_counts() -> dict[str, int]:
    conn = _ensure_conn()
    out = {"queued": 0, "running": 0, "completed": 0, "failed": 0}
    cur = conn.execute("SELECT status, COUNT(*) as c FROM job_queue GROUP BY status")
    for row in cur.fetchall():
        st = str(row["status"])
        if st in out:
            out[st] = int(row["c"])
    return out


def get_queue_item(job_id: str) -> Optional[dict[str, Any]]:
    conn = _ensure_conn()
    cur = conn.execute(
        """
        SELECT id, job_id, tenant_id, user_id, status, attempts, available_at, locked_at, worker_id, payload_json, last_error, last_error_at, last_attempt_at, created_at, updated_at
        FROM job_queue
        WHERE job_id=?
        LIMIT 1
        """,
        (job_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["payload"] = json.loads(d.pop("payload_json"))
    except Exception:
        d["payload"] = {}
    return d


def get_queue_attempts(job_id: str) -> int:
    conn = _ensure_conn()
    cur = conn.execute("SELECT attempts FROM job_queue WHERE job_id=? LIMIT 1", (job_id,))
    row = cur.fetchone()
    if not row:
        return 0
    return int(row["attempts"])


def dequeue_next_job(worker_id: str) -> Optional[dict[str, Any]]:
    """Claim next queued job (FIFO by id) and mark running atomically under process lock."""
    conn = _ensure_conn()
    now = _now_iso()
    with _DB_LOCK:
        cur = conn.execute(
            """
            SELECT id, job_id, tenant_id, user_id, payload_json
            FROM job_queue
            WHERE status='queued' AND available_at <= ?
            ORDER BY id ASC
            LIMIT 1
            """,
            (now,),
        )
        row = cur.fetchone()
        if not row:
            return None
        job_id = str(row["job_id"])
        conn.execute(
            """
            UPDATE job_queue
            SET status='running', locked_at=?, worker_id=?, attempts=attempts+1, updated_at=?
            WHERE job_id=?
            """,
            (now, worker_id, now, job_id),
        )
        conn.commit()

    payload_raw = row["payload_json"]
    try:
        payload = json.loads(payload_raw)
    except Exception:
        payload = {}
    return {
        "id": int(row["id"]),
        "job_id": job_id,
        "tenant_id": str(row["tenant_id"]),
        "user_id": str(row["user_id"]),
        "payload": payload,
    }


def get_job_result_owned(tenant_id: str, user_id: str, job_id: str) -> Optional[dict[str, Any]]:
    conn = _ensure_conn()
    cur = conn.execute(
        """
        SELECT job_id, tenant_id, user_id, status, result_json, created_at, updated_at
        FROM job_results
        WHERE tenant_id=? AND user_id=? AND job_id=?
        LIMIT 1
        """,
        (tenant_id, user_id, job_id),
    )
    row = cur.fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["result"] = json.loads(d.pop("result_json"))
    except Exception:
        d["result"] = {}
    return d


def list_jobs_owned(tenant_id: str, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
    conn = _ensure_conn()
    cur = conn.execute(
        """
        SELECT job_id, tenant_id, user_id, status, created_at, updated_at, file_count, invoice_estimated, invoice_extracted_count, extracted_total_amount, metadata_json
        FROM jobs
        WHERE tenant_id=? AND user_id=?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (tenant_id, user_id, int(limit)),
    )
    rows = cur.fetchall()
    return [dict(r) for r in rows]


def get_job_owned(tenant_id: str, user_id: str, job_id: str) -> Optional[dict[str, Any]]:
    conn = _ensure_conn()
    cur = conn.execute(
        """
        SELECT job_id, tenant_id, user_id, status, created_at, updated_at, file_count, invoice_estimated, invoice_extracted_count, extracted_total_amount, metadata_json
        FROM jobs
        WHERE tenant_id=? AND user_id=? AND job_id=?
        LIMIT 1
        """,
        (tenant_id, user_id, job_id),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def reset_all_jobs_for_tests() -> None:
    conn = _ensure_conn()
    with _DB_LOCK:
        conn.execute("DELETE FROM job_queue")
        conn.execute("DELETE FROM job_results")
        conn.execute("DELETE FROM jobs")
        conn.commit()
