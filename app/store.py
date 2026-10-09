from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid

import psycopg
from psycopg.rows import dict_row
from datetime import datetime, timezone
from typing import Any, Optional

_DB_LOCK = threading.Lock()
_CONN: Optional[sqlite3.Connection] = None
_PG_CONN: Optional[psycopg.Connection] = None
_PG_SCHEMA_READY = False


def _pg_dsn() -> Optional[str]:
    dsn = (os.getenv("LEDGERSNAPS_PG_DSN") or os.getenv("DATABASE_URL") or "").strip()
    return dsn or None


def _pg_enabled() -> bool:
    return _pg_dsn() is not None


def _ensure_pg_conn() -> psycopg.Connection:
    global _PG_CONN
    if _PG_CONN is not None and not _PG_CONN.closed:
        return _PG_CONN
    dsn = _pg_dsn()
    if not dsn:
        raise RuntimeError("postgres_dsn_missing")
    conn = psycopg.connect(dsn, autocommit=False, row_factory=dict_row)
    _PG_CONN = conn
    _ensure_pg_auth_schema(conn)
    return conn


def _ensure_pg_auth_schema(conn: psycopg.Connection) -> None:
    global _PG_SCHEMA_READY
    if _PG_SCHEMA_READY:
        return
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
              user_id TEXT PRIMARY KEY,
              email TEXT NOT NULL,
              phone_e164 TEXT NOT NULL,
              full_name TEXT NOT NULL,
              signup_ip TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_entitlements (
              user_id TEXT PRIMARY KEY,
              phone_e164 TEXT,
              phone_hash TEXT,
              device_fingerprint TEXT,
              signup_ip TEXT,
              trial_granted INTEGER NOT NULL DEFAULT 0,
              granted_at TEXT,
              updated_at TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS local_credentials (
              user_id TEXT PRIMARY KEY,
              password_salt TEXT NOT NULL,
              password_hash TEXT NOT NULL,
              algo TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS password_reset_tokens (
              token_hash TEXT PRIMARY KEY,
              user_id TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              status TEXT NOT NULL,
              created_at TEXT NOT NULL,
              used_at TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS signup_audit (
              id TEXT PRIMARY KEY,
              user_id TEXT NOT NULL,
              email TEXT NOT NULL,
              phone_e164 TEXT NOT NULL,
              accepted_terms INTEGER NOT NULL,
              terms_version TEXT NOT NULL,
              accepted_at TEXT NOT NULL,
              signup_ip TEXT,
              user_agent TEXT,
              created_at TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS pending_signup_tokens (
              token_hash TEXT PRIMARY KEY,
              email TEXT NOT NULL,
              full_name TEXT NOT NULL,
              phone_e164 TEXT NOT NULL,
              password_salt TEXT NOT NULL,
              password_hash TEXT NOT NULL,
              terms_version TEXT NOT NULL,
              accepted_at TEXT NOT NULL,
              signup_ip TEXT,
              user_agent TEXT,
              expires_at TEXT NOT NULL,
              status TEXT NOT NULL,
              created_at TEXT NOT NULL,
              used_at TEXT
            )
        """)
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_unique ON users(email)")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_phone_unique ON users(phone_e164)")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_signup_ip_unique ON users(signup_ip)")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_user_entitlements_phone_hash_unique ON user_entitlements(phone_hash)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_user_entitlements_trial ON user_entitlements(trial_granted, updated_at DESC)")
    conn.commit()
    _PG_SCHEMA_READY = True


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
    if _pg_enabled():
        conn = _ensure_pg_conn()
        _ensure_pg_auth_schema(conn)
        with conn.cursor() as cur:
            cur.execute("""
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
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS job_results (
                  job_id TEXT PRIMARY KEY,
                  tenant_id TEXT NOT NULL,
                  user_id TEXT NOT NULL,
                  status TEXT NOT NULL,
                  result_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS job_queue (
                  id BIGSERIAL PRIMARY KEY,
                  job_id TEXT NOT NULL UNIQUE,
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
                  last_error TEXT,
                  last_error_at TEXT,
                  last_attempt_at TEXT
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_jobs_owner_created ON jobs(tenant_id, user_id, created_at DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_jobs_owner_status ON jobs(tenant_id, user_id, status)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_job_results_owner ON job_results(tenant_id, user_id, updated_at DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_job_queue_status_available ON job_queue(status, available_at)")
        conn.commit()
        return

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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_entitlements (
              user_id TEXT PRIMARY KEY,
              phone_e164 TEXT,
              phone_hash TEXT,
              device_fingerprint TEXT,
              signup_ip TEXT,
              trial_granted INTEGER NOT NULL DEFAULT 0,
              granted_at TEXT,
              updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
              user_id TEXT PRIMARY KEY,
              email TEXT NOT NULL,
              phone_e164 TEXT NOT NULL,
              full_name TEXT NOT NULL,
              signup_ip TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS xero_connections (
              user_id TEXT PRIMARY KEY,
              tenant_id TEXT NOT NULL,
              access_token TEXT NOT NULL,
              refresh_token TEXT NOT NULL,
              token_type TEXT,
              scope TEXT,
              expires_at TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS oauth_identities (
              id TEXT PRIMARY KEY,
              user_id TEXT NOT NULL,
              provider TEXT NOT NULL,
              provider_subject_id TEXT NOT NULL,
              provider_email TEXT,
              provider_tenant_id TEXT,
              provider_tenant_name TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS local_credentials (
              user_id TEXT PRIMARY KEY,
              password_salt TEXT NOT NULL,
              password_hash TEXT NOT NULL,
              algo TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS password_reset_tokens (
              token_hash TEXT PRIMARY KEY,
              user_id TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              status TEXT NOT NULL,
              created_at TEXT NOT NULL,
              used_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS signup_audit (
              id TEXT PRIMARY KEY,
              user_id TEXT NOT NULL,
              email TEXT NOT NULL,
              phone_e164 TEXT NOT NULL,
              accepted_terms INTEGER NOT NULL,
              terms_version TEXT NOT NULL,
              accepted_at TEXT NOT NULL,
              signup_ip TEXT,
              user_agent TEXT,
              created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_signup_tokens (
              token_hash TEXT PRIMARY KEY,
              email TEXT NOT NULL,
              full_name TEXT NOT NULL,
              phone_e164 TEXT NOT NULL,
              password_salt TEXT NOT NULL,
              password_hash TEXT NOT NULL,
              terms_version TEXT NOT NULL,
              accepted_at TEXT NOT NULL,
              signup_ip TEXT,
              user_agent TEXT,
              expires_at TEXT NOT NULL,
              status TEXT NOT NULL,
              created_at TEXT NOT NULL,
              used_at TEXT
            )
            """
        )
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_unique ON users(email)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_phone_unique ON users(phone_e164)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_signup_ip_unique ON users(signup_ip)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_user_entitlements_phone_hash_unique ON user_entitlements(phone_hash)")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_oauth_provider_subject_unique ON oauth_identities(provider, provider_subject_id)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_oauth_user_id ON oauth_identities(user_id)")
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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_user_entitlements_trial ON user_entitlements(trial_granted, updated_at DESC)")
        conn.commit()
    if _CONN is None:
        conn.close()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_job(tenant_id: str, user_id: str, *, file_count: int = 0, invoice_estimated: int = 0, metadata_json: str = "{}") -> dict[str, Any]:
    now = _now_iso()
    job_id = str(uuid.uuid4())
    if _pg_enabled():
        conn = _ensure_pg_conn()
        with _DB_LOCK:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO jobs(job_id, tenant_id, user_id, status, created_at, updated_at, file_count, invoice_estimated, metadata_json)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (job_id, tenant_id, user_id, "created", now, now, int(file_count), int(invoice_estimated), metadata_json),
                )
            conn.commit()
        job = get_job_owned(tenant_id=tenant_id, user_id=user_id, job_id=job_id)
        if not job:
            raise RuntimeError("job_create_readback_failed")
        return job

    conn = _ensure_conn()
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
    metadata_json: str,
) -> None:
    now = _now_iso()
    if _pg_enabled():
        conn = _ensure_pg_conn()
        with _DB_LOCK:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE jobs
                    SET file_count=%s, invoice_estimated=%s, metadata_json=%s, updated_at=%s
                    WHERE tenant_id=%s AND user_id=%s AND job_id=%s
                    """,
                    (int(file_count), int(invoice_estimated), metadata_json, now, tenant_id, user_id, job_id),
                )
            conn.commit()
        return

    conn = _ensure_conn()
    with _DB_LOCK:
        conn.execute(
            """
            UPDATE jobs
            SET file_count=?, invoice_estimated=?, metadata_json=?, updated_at=?
            WHERE tenant_id=? AND user_id=? AND job_id=?
            """,
            (int(file_count), int(invoice_estimated), metadata_json, now, tenant_id, user_id, job_id),
        )
        conn.commit()

def set_job_status_owned(tenant_id: str, user_id: str, job_id: str, status: str) -> None:
    now = _now_iso()
    if _pg_enabled():
        conn = _ensure_pg_conn()
        with _DB_LOCK:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE jobs
                    SET status=%s, updated_at=%s
                    WHERE tenant_id=%s AND user_id=%s AND job_id=%s
                    """,
                    (status, now, tenant_id, user_id, job_id),
                )
            conn.commit()
        return

    conn = _ensure_conn()
    with _DB_LOCK:
        conn.execute(
            """
            UPDATE jobs
            SET status=?, updated_at=?
            WHERE tenant_id=? AND user_id=? AND job_id=?
            """,
            (status, now, tenant_id, user_id, job_id),
        )
        conn.commit()

def set_job_extraction_metrics_owned(
    tenant_id: str,
    user_id: str,
    job_id: str,
    *,
    invoice_extracted_count: int,
    extracted_total_amount: float,
) -> None:
    now = _now_iso()
    if _pg_enabled():
        conn = _ensure_pg_conn()
        with _DB_LOCK:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE jobs
                    SET invoice_extracted_count=%s, extracted_total_amount=%s, updated_at=%s
                    WHERE tenant_id=%s AND user_id=%s AND job_id=%s
                    """,
                    (int(invoice_extracted_count), float(extracted_total_amount), now, tenant_id, user_id, job_id),
                )
            conn.commit()
        return

    conn = _ensure_conn()
    with _DB_LOCK:
        conn.execute(
            """
            UPDATE jobs
            SET invoice_extracted_count=?, extracted_total_amount=?, updated_at=?
            WHERE tenant_id=? AND user_id=? AND job_id=?
            """,
            (int(invoice_extracted_count), float(extracted_total_amount), now, tenant_id, user_id, job_id),
        )
        conn.commit()

def upsert_job_result_owned(
    tenant_id: str,
    user_id: str,
    job_id: str,
    *,
    status: str,
    result: dict[str, Any],
) -> None:
    now = _now_iso()
    payload = json.dumps(result, ensure_ascii=False)
    if _pg_enabled():
        conn = _ensure_pg_conn()
        with _DB_LOCK:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO job_results(job_id, tenant_id, user_id, status, result_json, created_at, updated_at)
                    VALUES(%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(job_id) DO UPDATE SET
                      tenant_id=excluded.tenant_id,
                      user_id=excluded.user_id,
                      status=excluded.status,
                      result_json=excluded.result_json,
                      updated_at=excluded.updated_at
                    """,
                    (job_id, tenant_id, user_id, status, payload, now, now),
                )
            conn.commit()
        return

    conn = _ensure_conn()
    with _DB_LOCK:
        conn.execute(
            """
            INSERT INTO job_results(job_id, tenant_id, user_id, status, result_json, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(job_id) DO UPDATE SET
              tenant_id=excluded.tenant_id,
              user_id=excluded.user_id,
              status=excluded.status,
              result_json=excluded.result_json,
              updated_at=excluded.updated_at
            """,
            (job_id, tenant_id, user_id, status, payload, now, now),
        )
        conn.commit()

def enqueue_job_owned(
    tenant_id: str,
    user_id: str,
    job_id: str,
    *,
    payload: dict[str, Any],
    available_at: Optional[str] = None,
) -> None:
    now = _now_iso()
    avail = available_at or now
    payload_json = json.dumps(payload, ensure_ascii=False)
    if _pg_enabled():
        conn = _ensure_pg_conn()
        with _DB_LOCK:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO job_queue(job_id, tenant_id, user_id, status, attempts, available_at, payload_json, created_at, updated_at)
                    VALUES(%s,%s,%s,'queued',0,%s,%s,%s,%s)
                    ON CONFLICT(job_id) DO UPDATE SET
                      tenant_id=excluded.tenant_id,
                      user_id=excluded.user_id,
                      status='queued',
                      available_at=excluded.available_at,
                      payload_json=excluded.payload_json,
                      updated_at=excluded.updated_at
                    """,
                    (job_id, tenant_id, user_id, avail, payload_json, now, now),
                )
            conn.commit()
        return

    conn = _ensure_conn()
    with _DB_LOCK:
        conn.execute(
            """
            INSERT INTO job_queue(job_id, tenant_id, user_id, status, attempts, available_at, payload_json, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(job_id) DO UPDATE SET
              tenant_id=excluded.tenant_id,
              user_id=excluded.user_id,
              status='queued',
              available_at=excluded.available_at,
              payload_json=excluded.payload_json,
              updated_at=excluded.updated_at
            """,
            (job_id, tenant_id, user_id, 'queued', 0, avail, payload_json, now, now),
        )
        conn.commit()

def mark_job_queue_running(job_id: str, *, worker_id: str) -> None:
    now = _now_iso()
    if _pg_enabled():
        conn = _ensure_pg_conn()
        with _DB_LOCK:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE job_queue
                    SET status='running', locked_at=%s, worker_id=%s, attempts=attempts+1, updated_at=%s, last_attempt_at=%s
                    WHERE job_id=%s
                    """,
                    (now, worker_id, now, now, job_id),
                )
            conn.commit()
        return

    conn = _ensure_conn()
    with _DB_LOCK:
        conn.execute(
            """
            UPDATE job_queue
            SET status='running', locked_at=?, worker_id=?, attempts=attempts+1, updated_at=?, last_attempt_at=?
            WHERE job_id=?
            """,
            (now, worker_id, now, now, job_id),
        )
        conn.commit()

def mark_job_queue_done(job_id: str, status: str = "completed") -> None:
    now = _now_iso()
    if _pg_enabled():
        conn = _ensure_pg_conn()
        with _DB_LOCK:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE job_queue
                    SET status=%s, updated_at=%s, locked_at=NULL, worker_id=NULL
                    WHERE job_id=%s
                    """,
                    (status, now, job_id),
                )
            conn.commit()
        return

    conn = _ensure_conn()
    with _DB_LOCK:
        conn.execute(
            """
            UPDATE job_queue
            SET status=?, updated_at=?, locked_at=NULL, worker_id=NULL
            WHERE job_id=?
            """,
            (status, now, job_id),
        )
        conn.commit()

def mark_job_queue_retry(job_id: str, *, error: str, backoff_seconds: int) -> None:
    now = _now_iso()
    avail = datetime.now(timezone.utc).timestamp() + max(0, int(backoff_seconds))
    available_at = datetime.fromtimestamp(avail, tz=timezone.utc).isoformat()
    if _pg_enabled():
        conn = _ensure_pg_conn()
        with _DB_LOCK:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE job_queue
                    SET status='queued', available_at=%s, updated_at=%s, locked_at=NULL, worker_id=NULL,
                        last_error=%s, last_error_at=%s
                    WHERE job_id=%s
                    """,
                    (available_at, now, error, now, job_id),
                )
            conn.commit()
        return

    conn = _ensure_conn()
    with _DB_LOCK:
        conn.execute(
            """
            UPDATE job_queue
            SET status='queued', available_at=?, updated_at=?, locked_at=NULL, worker_id=NULL,
                last_error=?, last_error_at=?
            WHERE job_id=?
            """,
            (available_at, now, error, now, job_id),
        )
        conn.commit()

def mark_job_queue_failed(job_id: str, *, error: str) -> None:
    now = _now_iso()
    if _pg_enabled():
        conn = _ensure_pg_conn()
        with _DB_LOCK:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE job_queue
                    SET status='failed', updated_at=%s, locked_at=NULL, worker_id=NULL,
                        last_error=%s, last_error_at=%s
                    WHERE job_id=%s
                    """,
                    (now, error, now, job_id),
                )
            conn.commit()
        return

    conn = _ensure_conn()
    with _DB_LOCK:
        conn.execute(
            """
            UPDATE job_queue
            SET status='failed', updated_at=?, locked_at=NULL, worker_id=NULL,
                last_error=?, last_error_at=?
            WHERE job_id=?
            """,
            (now, error, now, job_id),
        )
        conn.commit()

def list_queue_counts() -> dict[str, int]:
    if _pg_enabled():
        conn = _ensure_pg_conn()
        out = {"queued": 0, "running": 0, "failed": 0, "completed": 0}
        with conn.cursor() as cur:
            cur.execute("SELECT status, COUNT(*) c FROM job_queue GROUP BY status")
            for row in cur.fetchall() or []:
                st = str(row[0])
                if st in out:
                    out[st] = int(row[1])
        return out

    conn = _ensure_conn()
    out = {"queued": 0, "running": 0, "failed": 0, "completed": 0}
    cur = conn.execute("SELECT status, COUNT(*) c FROM job_queue GROUP BY status")
    for row in cur.fetchall():
        st = str(row["status"])
        if st in out:
            out[st] = int(row["c"])
    return out

def get_queue_item(job_id: str) -> Optional[dict[str, Any]]:
    if _pg_enabled():
        conn = _ensure_pg_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, job_id, tenant_id, user_id, status, attempts, available_at, locked_at, worker_id, payload_json, last_error, last_error_at, last_attempt_at, created_at, updated_at
                FROM job_queue
                WHERE job_id=%s
                LIMIT 1
                """,
                (job_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            d = dict(row)
            try:
                d["payload"] = json.loads(d.get("payload_json") or "{}")
            except Exception:
                d["payload"] = {}
            return d

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
        d["payload"] = json.loads(d.get("payload_json") or "{}")
    except Exception:
        d["payload"] = {}
    return d

def get_queue_attempts(job_id: str) -> int:
    if _pg_enabled():
        conn = _ensure_pg_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT attempts FROM job_queue WHERE job_id=%s LIMIT 1", (job_id,))
            row = cur.fetchone()
            if not row:
                return 0
            return int(row[0])

    conn = _ensure_conn()
    cur = conn.execute("SELECT attempts FROM job_queue WHERE job_id=? LIMIT 1", (job_id,))
    row = cur.fetchone()
    if not row:
        return 0
    return int(row["attempts"])

def dequeue_next_job(worker_id: str) -> Optional[dict[str, Any]]:
    now = _now_iso()
    if _pg_enabled():
        conn = _ensure_pg_conn()
        with _DB_LOCK:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, job_id, tenant_id, user_id, payload_json
                    FROM job_queue
                    WHERE status='queued' AND available_at <= %s
                    ORDER BY id ASC
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                    """,
                    (now,),
                )
                row = cur.fetchone()
                if not row:
                    conn.commit()
                    return None
                cur.execute(
                    """
                    UPDATE job_queue
                    SET status='running', locked_at=%s, worker_id=%s, attempts=attempts+1, updated_at=%s, last_attempt_at=%s
                    WHERE id=%s
                    """,
                    (now, worker_id, now, now, row["id"]),
                )
            conn.commit()
        payload_json = row.get("payload_json") or "{}"
        try:
            payload = json.loads(payload_json)
        except Exception:
            payload = {}
        return {
            "job_id": row["job_id"],
            "tenant_id": row["tenant_id"],
            "user_id": row["user_id"],
            "payload": payload,
        }

    conn = _ensure_conn()
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
        conn.execute(
            """
            UPDATE job_queue
            SET status='running', locked_at=?, worker_id=?, attempts=attempts+1, updated_at=?, last_attempt_at=?
            WHERE id=?
            """,
            (now, worker_id, now, now, int(row["id"])),
        )
        conn.commit()
    payload_json = row["payload_json"]
    try:
        payload = json.loads(payload_json)
    except Exception:
        payload = {}
    return {
        "job_id": row["job_id"],
        "tenant_id": row["tenant_id"],
        "user_id": row["user_id"],
        "payload": payload,
    }

def get_job_result_owned(tenant_id: str, user_id: str, job_id: str) -> Optional[dict[str, Any]]:
    if _pg_enabled():
        conn = _ensure_pg_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT job_id, tenant_id, user_id, status, result_json, created_at, updated_at
                FROM job_results
                WHERE tenant_id=%s AND user_id=%s AND job_id=%s
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
    if _pg_enabled():
        conn = _ensure_pg_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT job_id, tenant_id, user_id, status, created_at, updated_at, file_count, invoice_estimated, invoice_extracted_count, extracted_total_amount, metadata_json
                FROM jobs
                WHERE tenant_id=%s AND user_id=%s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (tenant_id, user_id, int(limit)),
            )
            rows = cur.fetchall() or []
            return [dict(r) for r in rows]

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
    if _pg_enabled():
        conn = _ensure_pg_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT job_id, tenant_id, user_id, status, created_at, updated_at, file_count, invoice_estimated, invoice_extracted_count, extracted_total_amount, metadata_json
                FROM jobs
                WHERE tenant_id=%s AND user_id=%s AND job_id=%s
                LIMIT 1
                """,
                (tenant_id, user_id, job_id),
            )
            row = cur.fetchone()
            return dict(row) if row else None

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

def upsert_user_entitlement(
    user_id: str,
    *,
    phone_e164: Optional[str],
    phone_hash: Optional[str],
    device_fingerprint: Optional[str],
    signup_ip: Optional[str],
    trial_granted: bool,
) -> None:
    now = _now_iso()
    if _pg_enabled():
        conn = _ensure_pg_conn()
        with _DB_LOCK:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO user_entitlements(
                      user_id, phone_e164, phone_hash, device_fingerprint, signup_ip, trial_granted, granted_at, updated_at
                    )
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(user_id) DO UPDATE SET
                      phone_e164=excluded.phone_e164,
                      phone_hash=excluded.phone_hash,
                      device_fingerprint=excluded.device_fingerprint,
                      signup_ip=excluded.signup_ip,
                      trial_granted=excluded.trial_granted,
                      granted_at=CASE WHEN user_entitlements.granted_at IS NULL THEN excluded.granted_at ELSE user_entitlements.granted_at END,
                      updated_at=excluded.updated_at
                    """,
                    (
                        user_id,
                        phone_e164,
                        phone_hash,
                        device_fingerprint,
                        signup_ip,
                        1 if trial_granted else 0,
                        now if trial_granted else None,
                        now,
                    ),
                )
            conn.commit()
        return

    conn = _ensure_conn()
    with _DB_LOCK:
        conn.execute(
            """
            INSERT INTO user_entitlements(
              user_id, phone_e164, phone_hash, device_fingerprint, signup_ip, trial_granted, granted_at, updated_at
            )
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
              phone_e164=excluded.phone_e164,
              phone_hash=excluded.phone_hash,
              device_fingerprint=excluded.device_fingerprint,
              signup_ip=excluded.signup_ip,
              trial_granted=excluded.trial_granted,
              granted_at=CASE WHEN user_entitlements.granted_at IS NULL THEN excluded.granted_at ELSE user_entitlements.granted_at END,
              updated_at=excluded.updated_at
            """,
            (
                user_id,
                phone_e164,
                phone_hash,
                device_fingerprint,
                signup_ip,
                1 if trial_granted else 0,
                now if trial_granted else None,
                now,
            ),
        )
        conn.commit()

def has_trial_claim_for_phone_hash(phone_hash: str) -> bool:
    if _pg_enabled():
        conn = _ensure_pg_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM user_entitlements WHERE phone_hash=%s AND trial_granted=1 LIMIT 1",
                (phone_hash,),
            )
            return cur.fetchone() is not None
    conn = _ensure_conn()
    cur = conn.execute(
        "SELECT 1 FROM user_entitlements WHERE phone_hash=? AND trial_granted=1 LIMIT 1",
        (phone_hash,),
    )
    return cur.fetchone() is not None

def upsert_user_profile(
    user_id: str,
    *,
    email: Optional[str],
    phone_e164: Optional[str],
    full_name: Optional[str],
    signup_ip: Optional[str],
) -> None:
    now = _now_iso()
    e = (email or "").strip()
    p = (phone_e164 or "").strip()
    n = (full_name or "").strip()
    if not e:
        raise ValueError("email_required")
    if not p:
        raise ValueError("phone_required")
    if not n:
        raise ValueError("full_name_required")

    if _pg_enabled():
        conn = _ensure_pg_conn()
        with _DB_LOCK:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO users(user_id, email, phone_e164, full_name, signup_ip, created_at, updated_at)
                    VALUES(%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(user_id) DO UPDATE SET
                      email=excluded.email,
                      phone_e164=excluded.phone_e164,
                      full_name=excluded.full_name,
                      signup_ip=COALESCE(excluded.signup_ip, users.signup_ip),
                      updated_at=excluded.updated_at
                    """,
                    (user_id, e, p, n, signup_ip, now, now),
                )
            conn.commit()
        return

    conn = _ensure_conn()
    with _DB_LOCK:
        conn.execute(
            """
            INSERT INTO users(user_id, email, phone_e164, full_name, signup_ip, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
              email=excluded.email,
              phone_e164=excluded.phone_e164,
              full_name=excluded.full_name,
              signup_ip=COALESCE(excluded.signup_ip, users.signup_ip),
              updated_at=excluded.updated_at
            """,
            (user_id, e, p, n, signup_ip, now, now),
        )
        conn.commit()

def ip_already_registered(signup_ip: str) -> bool:
    if _pg_enabled():
        conn = _ensure_pg_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM users WHERE signup_ip=%s LIMIT 1", (signup_ip,))
            return cur.fetchone() is not None
    conn = _ensure_conn()
    cur = conn.execute("SELECT 1 FROM users WHERE signup_ip=? LIMIT 1", (signup_ip,))
    return cur.fetchone() is not None

def upsert_xero_connection(
    user_id: str,
    *,
    tenant_id: str,
    access_token: str,
    refresh_token: str,
    token_type: Optional[str],
    scope: Optional[str],
    expires_at: str,
) -> None:
    conn = _ensure_conn()
    now = _now_iso()
    with _DB_LOCK:
        conn.execute(
            """
            INSERT INTO xero_connections(user_id, tenant_id, access_token, refresh_token, token_type, scope, expires_at, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
              tenant_id=excluded.tenant_id,
              access_token=excluded.access_token,
              refresh_token=excluded.refresh_token,
              token_type=excluded.token_type,
              scope=excluded.scope,
              expires_at=excluded.expires_at,
              updated_at=excluded.updated_at
            """,
            (user_id, tenant_id, access_token, refresh_token, token_type, scope, expires_at, now, now),
        )
        conn.commit()


def get_xero_connection(user_id: str) -> Optional[dict[str, Any]]:
    conn = _ensure_conn()
    cur = conn.execute(
        """
        SELECT user_id, tenant_id, access_token, refresh_token, token_type, scope, expires_at, created_at, updated_at
        FROM xero_connections
        WHERE user_id=?
        LIMIT 1
        """,
        (user_id,),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def delete_xero_connection(user_id: str) -> None:
    conn = _ensure_conn()
    with _DB_LOCK:
        conn.execute("DELETE FROM xero_connections WHERE user_id=?", (user_id,))
        conn.commit()


def upsert_oauth_identity(
    *,
    user_id: str,
    provider: str,
    provider_subject_id: str,
    provider_email: Optional[str],
    provider_tenant_id: Optional[str],
    provider_tenant_name: Optional[str],
) -> None:
    conn = _ensure_conn()
    now = _now_iso()
    row_id = str(uuid.uuid4())
    with _DB_LOCK:
        conn.execute(
            """
            INSERT INTO oauth_identities(
              id, user_id, provider, provider_subject_id, provider_email, provider_tenant_id, provider_tenant_name, created_at, updated_at
            )
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(provider, provider_subject_id) DO UPDATE SET
              user_id=excluded.user_id,
              provider_email=excluded.provider_email,
              provider_tenant_id=excluded.provider_tenant_id,
              provider_tenant_name=excluded.provider_tenant_name,
              updated_at=excluded.updated_at
            """,
            (
                row_id,
                user_id,
                provider,
                provider_subject_id,
                provider_email,
                provider_tenant_id,
                provider_tenant_name,
                now,
                now,
            ),
        )
        conn.commit()


def get_oauth_identity(*, provider: str, provider_subject_id: str) -> Optional[dict[str, Any]]:
    conn = _ensure_conn()
    cur = conn.execute(
        """
        SELECT id, user_id, provider, provider_subject_id, provider_email, provider_tenant_id, provider_tenant_name, created_at, updated_at
        FROM oauth_identities
        WHERE provider=? AND provider_subject_id=?
        LIMIT 1
        """,
        (provider, provider_subject_id),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def upsert_local_credential(user_id: str, *, password_salt: str, password_hash: str, algo: str = "pbkdf2_sha256") -> None:
    now = _now_iso()
    if _pg_enabled():
        conn = _ensure_pg_conn()
        with _DB_LOCK:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO local_credentials(user_id, password_salt, password_hash, algo, created_at, updated_at)
                    VALUES(%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(user_id) DO UPDATE SET
                      password_salt=excluded.password_salt,
                      password_hash=excluded.password_hash,
                      algo=excluded.algo,
                      updated_at=excluded.updated_at
                    """,
                    (user_id, password_salt, password_hash, algo, now, now),
                )
            conn.commit()
        return

    conn = _ensure_conn()
    with _DB_LOCK:
        conn.execute(
            """
            INSERT INTO local_credentials(user_id, password_salt, password_hash, algo, created_at, updated_at)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
              password_salt=excluded.password_salt,
              password_hash=excluded.password_hash,
              algo=excluded.algo,
              updated_at=excluded.updated_at
            """,
            (user_id, password_salt, password_hash, algo, now, now),
        )
        conn.commit()

def get_local_credential(user_id: str) -> Optional[dict[str, Any]]:
    if _pg_enabled():
        conn = _ensure_pg_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT user_id, password_salt, password_hash, algo, created_at, updated_at
                FROM local_credentials
                WHERE user_id=%s
                LIMIT 1
                """,
                (user_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    conn = _ensure_conn()
    cur = conn.execute(
        """
        SELECT user_id, password_salt, password_hash, algo, created_at, updated_at
        FROM local_credentials
        WHERE user_id=?
        LIMIT 1
        """,
        (user_id,),
    )
    row = cur.fetchone()
    return dict(row) if row else None

def get_user_by_phone(phone_e164: str) -> Optional[dict[str, Any]]:
    if _pg_enabled():
        conn = _ensure_pg_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT user_id, email, phone_e164, full_name, signup_ip, created_at, updated_at
                FROM users
                WHERE phone_e164=%s
                LIMIT 1
                """,
                (str(phone_e164 or "").strip(),),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    conn = _ensure_conn()
    cur = conn.execute(
        """
        SELECT user_id, email, phone_e164, full_name, signup_ip, created_at, updated_at
        FROM users
        WHERE phone_e164=?
        LIMIT 1
        """,
        (str(phone_e164 or "").strip(),),
    )
    row = cur.fetchone()
    return dict(row) if row else None

def get_user_by_email(email: str) -> Optional[dict[str, Any]]:
    if _pg_enabled():
        conn = _ensure_pg_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT user_id, email, phone_e164, full_name, signup_ip, created_at, updated_at
                FROM users
                WHERE email=%s
                LIMIT 1
                """,
                (str(email or "").strip(),),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    conn = _ensure_conn()
    cur = conn.execute(
        """
        SELECT user_id, email, phone_e164, full_name, signup_ip, created_at, updated_at
        FROM users
        WHERE email=?
        LIMIT 1
        """,
        (str(email or "").strip(),),
    )
    row = cur.fetchone()
    return dict(row) if row else None

def get_user_by_id(user_id: str) -> Optional[dict[str, Any]]:
    if _pg_enabled():
        conn = _ensure_pg_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT user_id, email, phone_e164, full_name, signup_ip, created_at, updated_at
                FROM users
                WHERE user_id=%s
                LIMIT 1
                """,
                (str(user_id or "").strip(),),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    conn = _ensure_conn()
    cur = conn.execute(
        """
        SELECT user_id, email, phone_e164, full_name, signup_ip, created_at, updated_at
        FROM users
        WHERE user_id=?
        LIMIT 1
        """,
        (str(user_id or "").strip(),),
    )
    row = cur.fetchone()
    return dict(row) if row else None

def create_password_reset_token(*, token_hash: str, user_id: str, expires_at: str) -> None:
    now = _now_iso()
    if _pg_enabled():
        conn = _ensure_pg_conn()
        with _DB_LOCK:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO password_reset_tokens(token_hash, user_id, expires_at, status, created_at, used_at)
                    VALUES(%s,%s,%s,%s,%s,NULL)
                    """,
                    (token_hash, user_id, expires_at, "pending", now),
                )
            conn.commit()
        return
    conn = _ensure_conn()
    with _DB_LOCK:
        conn.execute(
            """
            INSERT INTO password_reset_tokens(token_hash, user_id, expires_at, status, created_at, used_at)
            VALUES(?,?,?,?,?,NULL)
            """,
            (token_hash, user_id, expires_at, "pending", now),
        )
        conn.commit()

def get_password_reset_token(token_hash: str) -> Optional[dict[str, Any]]:
    if _pg_enabled():
        conn = _ensure_pg_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT token_hash, user_id, expires_at, status, created_at, used_at
                FROM password_reset_tokens
                WHERE token_hash=%s
                LIMIT 1
                """,
                (token_hash,),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    conn = _ensure_conn()
    cur = conn.execute(
        """
        SELECT token_hash, user_id, expires_at, status, created_at, used_at
        FROM password_reset_tokens
        WHERE token_hash=?
        LIMIT 1
        """,
        (token_hash,),
    )
    row = cur.fetchone()
    return dict(row) if row else None

def mark_password_reset_token_used(token_hash: str) -> None:
    now = _now_iso()
    if _pg_enabled():
        conn = _ensure_pg_conn()
        with _DB_LOCK:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE password_reset_tokens
                    SET status='used', used_at=%s
                    WHERE token_hash=%s
                    """,
                    (now, token_hash),
                )
            conn.commit()
        return
    conn = _ensure_conn()
    with _DB_LOCK:
        conn.execute(
            """
            UPDATE password_reset_tokens
            SET status='used', used_at=?
            WHERE token_hash=?
            """,
            (now, token_hash),
        )
        conn.commit()

def append_signup_audit(
    *,
    user_id: str,
    email: str,
    phone_e164: str,
    accepted_terms: bool,
    terms_version: str,
    accepted_at: str,
    signup_ip: Optional[str],
    user_agent: Optional[str],
) -> None:
    if _pg_enabled():
        conn = _ensure_pg_conn()
        with _DB_LOCK:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO signup_audit(id, user_id, email, phone_e164, accepted_terms, terms_version, accepted_at, signup_ip, user_agent, created_at)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        str(uuid.uuid4()),
                        user_id,
                        email,
                        phone_e164,
                        1 if accepted_terms else 0,
                        terms_version,
                        accepted_at,
                        signup_ip,
                        user_agent,
                        _now_iso(),
                    ),
                )
            conn.commit()
        return
    conn = _ensure_conn()
    with _DB_LOCK:
        conn.execute(
            """
            INSERT INTO signup_audit(id, user_id, email, phone_e164, accepted_terms, terms_version, accepted_at, signup_ip, user_agent, created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                str(uuid.uuid4()),
                user_id,
                email,
                phone_e164,
                1 if accepted_terms else 0,
                terms_version,
                accepted_at,
                signup_ip,
                user_agent,
                _now_iso(),
            ),
        )
        conn.commit()

def create_pending_signup_token(
    *,
    token_hash: str,
    email: str,
    full_name: str,
    phone_e164: str,
    password_salt: str,
    password_hash: str,
    terms_version: str,
    accepted_at: str,
    signup_ip: Optional[str],
    user_agent: Optional[str],
    expires_at: str,
) -> None:
    now = _now_iso()
    if _pg_enabled():
        conn = _ensure_pg_conn()
        with _DB_LOCK:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO pending_signup_tokens(
                      token_hash, email, full_name, phone_e164, password_salt, password_hash,
                      terms_version, accepted_at, signup_ip, user_agent, expires_at, status, created_at, used_at
                    )
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL)
                    """,
                    (
                        token_hash,
                        email,
                        full_name,
                        phone_e164,
                        password_salt,
                        password_hash,
                        terms_version,
                        accepted_at,
                        signup_ip,
                        user_agent,
                        expires_at,
                        "pending",
                        now,
                    ),
                )
            conn.commit()
        return

    conn = _ensure_conn()
    with _DB_LOCK:
        conn.execute(
            """
            INSERT INTO pending_signup_tokens(
              token_hash, email, full_name, phone_e164, password_salt, password_hash,
              terms_version, accepted_at, signup_ip, user_agent, expires_at, status, created_at, used_at
            )
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)
            """,
            (
                token_hash,
                email,
                full_name,
                phone_e164,
                password_salt,
                password_hash,
                terms_version,
                accepted_at,
                signup_ip,
                user_agent,
                expires_at,
                "pending",
                now,
            ),
        )
        conn.commit()

def get_pending_signup_token(token_hash: str) -> Optional[dict[str, Any]]:
    if _pg_enabled():
        conn = _ensure_pg_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT token_hash, email, full_name, phone_e164, password_salt, password_hash,
                       terms_version, accepted_at, signup_ip, user_agent, expires_at, status, created_at, used_at
                FROM pending_signup_tokens
                WHERE token_hash=%s
                LIMIT 1
                """,
                (token_hash,),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    conn = _ensure_conn()
    cur = conn.execute(
        """
        SELECT token_hash, email, full_name, phone_e164, password_salt, password_hash,
               terms_version, accepted_at, signup_ip, user_agent, expires_at, status, created_at, used_at
        FROM pending_signup_tokens
        WHERE token_hash=?
        LIMIT 1
        """,
        (token_hash,),
    )
    row = cur.fetchone()
    return dict(row) if row else None

def get_latest_pending_signup_by_email(email: str, *, status: str = "pending") -> Optional[dict[str, Any]]:
    if _pg_enabled():
        conn = _ensure_pg_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT token_hash, email, full_name, phone_e164, password_salt, password_hash,
                       terms_version, accepted_at, signup_ip, user_agent, expires_at, status, created_at, used_at
                FROM pending_signup_tokens
                WHERE email=%s AND status=%s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (email, status),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    conn = _ensure_conn()
    cur = conn.execute(
        """
        SELECT token_hash, email, full_name, phone_e164, password_salt, password_hash,
               terms_version, accepted_at, signup_ip, user_agent, expires_at, status, created_at, used_at
        FROM pending_signup_tokens
        WHERE email=? AND status=?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (email, status),
    )
    row = cur.fetchone()
    return dict(row) if row else None

def mark_pending_signup_token_email_verified(token_hash: str) -> None:
    conn = _ensure_conn()
    with _DB_LOCK:
        conn.execute(
            """
            UPDATE pending_signup_tokens
            SET status='email_verified'
            WHERE token_hash=? AND status='pending'
            """,
            (token_hash,),
        )
        conn.commit()


def mark_pending_signup_token_used(token_hash: str) -> None:
    now = _now_iso()
    if _pg_enabled():
        conn = _ensure_pg_conn()
        with _DB_LOCK:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE pending_signup_tokens
                    SET status='used', used_at=%s
                    WHERE token_hash=%s
                    """,
                    (now, token_hash),
                )
            conn.commit()
        return
    conn = _ensure_conn()
    with _DB_LOCK:
        conn.execute(
            """
            UPDATE pending_signup_tokens
            SET status='used', used_at=?
            WHERE token_hash=?
            """,
            (now, token_hash),
        )
        conn.commit()

def reset_all_jobs_for_tests() -> None:
    if _pg_enabled():
        conn = _ensure_pg_conn()
        with _DB_LOCK:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM oauth_identities")
                cur.execute("DELETE FROM xero_connections")
                cur.execute("DELETE FROM local_credentials")
                cur.execute("DELETE FROM password_reset_tokens")
                cur.execute("DELETE FROM signup_audit")
                cur.execute("DELETE FROM pending_signup_tokens")
                cur.execute("DELETE FROM users")
                cur.execute("DELETE FROM user_entitlements")
                cur.execute("DELETE FROM job_queue")
                cur.execute("DELETE FROM job_results")
                cur.execute("DELETE FROM jobs")
            conn.commit()
        return

    conn = _ensure_conn()
    with _DB_LOCK:
        conn.execute("DELETE FROM oauth_identities")
        conn.execute("DELETE FROM xero_connections")
        conn.execute("DELETE FROM local_credentials")
        conn.execute("DELETE FROM password_reset_tokens")
        conn.execute("DELETE FROM signup_audit")
        conn.execute("DELETE FROM pending_signup_tokens")
        conn.execute("DELETE FROM users")
        conn.execute("DELETE FROM user_entitlements")
        conn.execute("DELETE FROM job_queue")
        conn.execute("DELETE FROM job_results")
        conn.execute("DELETE FROM jobs")
        conn.commit()

