# LedgerSnaps Frontend Wiring Blueprint (v1)

This document maps tested backend functions to concrete web-app buttons/pages so frontend can move fast without changing API contracts.

## 1) Global principles

- Keep backend API contracts stable; frontend should be thin wiring.
- Use machine-readable error codes to drive UI behavior.
- Do not ask users to enter Xero credentials inside LedgerSnaps UI; OAuth page handles login.

## 2) Page-to-endpoint mapping

## A. Upload / Extract page

Primary action: `Extract + Preview`

- Endpoint: `POST /api/v1/extract-and-map/batch/multi?target=xero&flow_mode=auto`
- Input: `files[]` + `Authorization: Bearer ...`
- Success:
  - Render per-file/chunk result list
  - Show `invoice` fields and validation warnings
  - Show ABR fields: `abr_available`, `abr_abn`, `abr_entity_name`, `abr_gst_registered`
- Error handling:
  - `401 missing_or_invalid_bearer_token` -> redirect login
  - `400 invoice_count_exceeded` -> show limit warning

Secondary action: `Export Excel`

- Endpoint: `POST /api/v1/extract-and-map/unified/export.xlsx?target=xero&flow_mode=auto`
- Success: download xlsx directly
- Note: xlsx now includes ABR + GST registration columns and xero_draft tab

## B. Xero connection panel (settings or upload page side panel)

On page load:

- Endpoint: `GET /api/v1/xero/connection`
- Render:
  - `connected`
  - active `tenant_id`
  - tenant list from `tenants` (if available)

Connect button:

- Endpoint: `GET /api/v1/xero/connect`
- Open `url` in popup/new tab
- After callback/exchange success, refetch `/api/v1/xero/connection`

Disconnect button (switch account flow):

- Endpoint: `POST /api/v1/xero/disconnect`
- Success: set UI status to disconnected

Switch company button/dropdown:

- Endpoint: `POST /api/v1/xero/switch-tenant?tenant_id=...`
- Success: active tenant updated; subsequent uploads go to selected tenant

## C. Upload to Xero button

Primary action: `Upload to Xero Draft`

- Endpoint: `POST /api/v1/xero/drafts?target=xero&flow_mode=auto`
- Input: one file per request

Success:

- Show `xero.Invoices[0].InvoiceID`
- Show status (expect `DRAFT`)

Auth-required UX contract:

- If response is `409` with
  - `error = xero_auth_required` OR `xero_reauth_required`
  - and `connect.url`
- Frontend should:
  1. Show modal: "Authorize Xero to continue"
  2. Open `connect.url`
  3. Retry upload automatically after auth completes

Payload-not-ready contract:

- If `400` and `error = xero_payload_not_ready`
- Show missing fields list and warnings

## 3) Error code table for frontend

- `xero_auth_required` -> show OAuth modal + open connect URL
- `xero_reauth_required` -> show re-auth modal + open connect URL
- `xero_payload_not_ready` -> show mapping validation issues
- `topup_requires_paid_plan` -> show upgrade CTA
- `quota_exceeded` / `invoice_count_exceeded` -> show quota/limit message
- `missing_or_invalid_bearer_token` -> require login

## 4) Recommended frontend sequence (minimal)

1. Upload page with extract preview
2. Xero connection panel (connect/disconnect/switch tenant)
3. Upload to Xero draft action
4. Billing/quota indicators

This keeps implementation fast while reusing already-tested backend behavior.
