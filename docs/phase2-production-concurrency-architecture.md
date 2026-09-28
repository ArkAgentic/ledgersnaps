# LedgerSnaps Phase 2 — Production Concurrency Architecture (100 concurrent users)

## Goal
Build a production-ready extraction pipeline where:
- 100 users can submit jobs concurrently
- user data is strictly isolated (`tenant_id + user_id + job_id`)
- API tier is never blocked by heavy extraction work
- failures are visible and recoverable

## Non-negotiable isolation rules
1. Every object (job, result, blob path, queue message) must include:
   - `tenant_id`
   - `user_id`
   - `job_id`
2. Every read query and fetch must enforce owner scope.
3. Any cross-user access returns 404 (not found) or 403.
4. No global/shared temp output namespace.

## Target production topology (Azure)
1. **Frontend (Web)**
   - Requests upload URL + creates job
   - Uploads file directly to Blob using SAS (no file passthrough via API)
   - Polls job status/result APIs

2. **API Service (FastAPI on ACA)**
   - AuthN/AuthZ + quota checks
   - Creates job records in PostgreSQL
   - Enqueues job message to Azure Service Bus Queue
   - Returns `202 accepted` + `job_id`

3. **Worker Service (FastAPI worker or Python worker on ACA)**
   - Pulls Service Bus messages
   - Downloads file from Blob
   - Runs extract/map pipeline
   - Writes results + status to PostgreSQL
   - Deletes temp blobs after completion (or after failure retention window)

4. **Storage / State**
   - PostgreSQL: users, jobs, job_files, job_results, billing usage
   - Blob: temporary uploads only
   - Service Bus: durable queue + retry + DLQ

## API contract (production)
- `POST /api/v1/jobs/submit`
  - input: file metadata, target, flow_mode
  - output: `{ job_id, status: "queued" }`
- `GET /api/v1/jobs/{job_id}`
  - output: queued/running/completed/failed + timestamps + counters
- `GET /api/v1/jobs/{job_id}/result`
  - output: extraction payload (owner-only)

## Queue/worker behavior
- API writes one message per job to Service Bus
- Worker uses lock/visibility semantics from Service Bus
- Retry policy: e.g. max 3 attempts then DLQ
- Idempotency key = `job_id` (replays must not duplicate rows)

## Current implementation status (2026-09)
- Producer path:
  - `QueueBackend(kind=sqlite|servicebus)` implemented.
  - `POST /api/v1/jobs` enqueues via backend abstraction.
  - `servicebus` mode hard-fails if env is missing (no silent drop).
- Consumer path:
  - `scripts/run_worker.py` supports both backends.
  - `sqlite` mode consumes local `job_queue` via `process_one_queued_job()`.
  - `servicebus` mode receives one message and processes it through `process_claimed_item(...)`, then `complete_message` on success / dead-letter on failure.
- Processing path:
  - Owner-scoped status transitions are enforced (`queued -> running -> completed|failed`).
  - Placeholder completion is still used for `jobs_create` without input artifact.
  - Real extract execution path is available when payload carries input artifact reference.

### Newly landed in this iteration
- Added `POST /api/v1/jobs/{job_id}/submit`:
  - owner checks + pre-extract invoice cap validation
  - persists temp artifact via storage backend (`localfs` now, `azureblob` when configured)
  - enqueues queue message with `artifact_ref` + `artifact_backend`
- Worker now resolves artifact by reference:
  - download/read artifact bytes from backend
  - run extraction pipeline
  - write owner-scoped result
  - delete temp artifact in `finally` (best effort cleanup)

## Capacity baseline for 100 concurrent users
- API replicas: start 2-3
- Worker replicas: autoscale by queue length / active messages
- Backpressure: reject or queue with clear status when quota/rate limits exceeded

## Data lifecycle (privacy)
- Temp blob path: `tenant/{tenant_id}/user/{user_id}/jobs/{job_id}/...`
- On completed job: immediate delete temp source files
- Blob lifecycle rule: hard-delete leftovers after 24h
- Persist only structured result + audit metadata (unless user opts in to retention)

## Implementation phases
### Phase 2.1 (now)
- Finalize auth-backed owner scope in all extraction/result endpoints
- Migrate from in-process extraction call path to submit+poll contract
- Add job status transitions with strict FSM: `created -> queued -> running -> completed|failed`

### Phase 2.2
- Integrate Azure Service Bus queue producer/consumer
- Add retry + DLQ handling + poison message observability

### Phase 2.3
- Integrate Blob SAS upload flow and worker-side download/delete
- Remove direct multipart file extraction from heavy endpoints in production mode

### Phase 2.4
- Load/concurrency tests (100 concurrent submitters)
- Verify no cross-user leakage with adversarial tests

## Acceptance criteria
1. 100 concurrent job submissions return `queued` without API timeout spikes.
2. No cross-user read is possible (tests must prove B cannot read A job/result).
3. Worker failures are retried; unrecoverable jobs move to DLQ and are marked failed.
4. Blob temp files are deleted on completion and lifecycle-purged by policy.
5. End-to-end traces available by `job_id` across API, queue, worker, DB.
