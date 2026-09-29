# LedgerSnaps Registration & Entitlement Policy (v1)

Last updated: 2026-09-28

## 1) Purpose
This document defines the current registration, trial entitlement, quota usage, top-up gating, and isolation rules.
It is intended for:
- Product/design alignment
- Backend/frontend implementation consistency
- Future user-facing policy copy drafting

---

## 2) Registration Requirements (Current)
A new user trial claim requires all of the following:
- `email` (required)
- `phone_e164` (required, E.164 format, e.g. `+61400111222`)
- `full_name` (required)
- phone OTP verification pass

Stored user profile fields:
- `user_id`
- `email`
- `phone_e164`
- `full_name`
- `signup_ip`
- `created_at`
- `updated_at`

---

## 3) Anti-abuse Guards (Current)
### 3.1 One-phone-one-trial
- Trial claim is phone-bound.
- A phone hash can only claim trial once.
- Reuse returns: `trial_already_claimed_for_phone`.

### 3.2 One-IP-one-registration (strict)
- Trial registration checks whether `signup_ip` already exists.
- If exists, registration is blocked.
- Error: `ip_already_registered`.

### 3.3 OTP verification
- Endpoint to send code:
  - `POST /api/v1/auth/phone/send-code?phone_e164=...`
- Current provider modes:
  - `dev` (local test, returns `dev_code`)
  - `azure_sms` (Azure Communication Services SMS)

---

## 4) Plans & Quota (Current Pricing)
### Trial
- Duration: 7 days
- Quota: 15 invoices (one-off trial pool)

### Paid Plans
- `starter_14_95`: AUD 14.95 / 30 days / 100 invoices
- `pro_29_95`: AUD 29.95 / 30 days / 300 invoices

### Top-up
- Pack: AUD 5.95 / 50 invoices
- **Only available for paid users** (`starter_14_95` or `pro_29_95`)
- Trial/non-paid users are blocked:
  - `topup_requires_paid_plan`

---

## 5) Quota Consumption Semantics
Consumption priority:
1. Trial pool
2. Paid plan pool
3. Top-up pool

System behavior:
- If remaining quota is insufficient, extraction is blocked.
- Invoice counting is by extracted invoice units (not file count intent).

Important policy rule:
- `invoices != files`
- One file may contain multiple invoices.

---

## 6) Job Metrics Tracking (Current)
Per job, the system persists:
- `invoice_extracted_count`
- `extracted_total_amount`

These fields are updated after worker processing completes.

---

## 7) User Isolation & Concurrency (Current)
Isolation key:
- `tenant_id + user_id + job_id`

Rules:
- Cross-user job/job-result access is denied (404 owner-scope behavior).
- Queue/worker flow supports concurrent submissions and owner-scoped reads.

---

## 8) API References (Current)
- `GET /api/v1/auth/dev-token` (dev flow)
- `POST /api/v1/auth/phone/send-code`
- `GET /api/v1/billing/me`
- `POST /api/v1/billing/plan`
- `POST /api/v1/billing/topup`
- `POST /api/v1/jobs`
- `POST /api/v1/jobs/{job_id}/submit`
- `GET /api/v1/jobs/{job_id}`
- `GET /api/v1/jobs/{job_id}/result`

---

## 9) Can this be adapted for user-facing copy?
Yes.
This file is currently technical/internal. It can be converted into user-facing pages by:
- simplifying language
- adding legal/compliance wording
- adding support/contact and refund notes
- localizing to target language

Recommended split later:
- internal source of truth: this file
- user-facing docs: Terms, Trial Policy, Pricing FAQ

---

## 10) Known Gaps Before Production Go-live
1. Password/auth account model is still dev-token centric (needs production auth flow).
2. One-IP-one-registration is strict and may over-block shared networks (office/co-working/mobile carrier NAT).
3. Stripe integration is not wired yet (top-up currently grant endpoint behavior).
4. OTP provider in prod should be Azure ACS SMS + sender compliance setup.

---

## 11) Next major milestone
Proceed to Xero API integration after keeping this policy stable.
Recommended order:
1. Xero OAuth + token storage
2. Contact/supplier matching
3. Draft bill/invoice create
4. idempotent sync status per job
5. reconciliation/error surface in UI
