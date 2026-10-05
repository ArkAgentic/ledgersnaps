# LedgerSnaps OAuth Auth Flow Design (Xero + MYOB)

## Goal
Use **Xero / MYOB as first-step identity providers** for login, then complete local registration only when needed.

- Existing user: one-click login.
- New user: OAuth identity first, then complete required fields (phone + OTP + profile), then create account and grant trial.
- Keep anti-abuse controls for trial.

---

## Product UX (final behavior)

### Entry page (current right panel)
- `Continue with Xero`
- `Continue with MYOB`
- Email/password sign-in (optional fallback)
- Secondary copy: `Don’t have an account yet? Start your 7-day free trial`

### Xero/MYOB button click
1. Redirect to provider OAuth consent page.
2. Provider redirects back to LedgerSnaps callback.
3. Backend verifies OAuth response and resolves provider identity.
4. Branch:
   - **Matched local account** -> issue session/JWT -> go `/dashboard/upload`
   - **No local account** -> go `/signup/complete` (prefill what we got from provider)

### Signup complete page (for new OAuth users)
Collect:
- full name (prefilled if available)
- email (prefilled if available)
- Australian mobile number (`+61...`)
- OTP verify

Then create local account + bind provider identity + grant trial entitlement.

---

## Data model changes

## 1) New table: `oauth_identities`
```sql
CREATE TABLE IF NOT EXISTS oauth_identities (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  provider TEXT NOT NULL,               -- 'xero' | 'myob'
  provider_subject_id TEXT NOT NULL,    -- OIDC sub / stable account id
  provider_email TEXT,
  provider_tenant_id TEXT,              -- xero tenant id or myob businessId
  provider_tenant_name TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(provider, provider_subject_id),
  FOREIGN KEY(user_id) REFERENCES users(user_id)
);
```

## 2) Existing `users` table
Keep required fields:
- `email`
- `phone_e164`
- `full_name`
- `signup_ip`

Existing unique constraints should stay:
- unique email
- unique phone
- unique signup_ip (or policy-adjusted rate limit if later loosened)

---

## API contract

## A. Start OAuth

### `GET /api/v1/auth/xero/start`
Response:
```json
{ "authorize_url": "https://login.xero.com/..." }
```

### `GET /api/v1/auth/myob/start`
Response:
```json
{ "authorize_url": "https://secure.myob.com/..." }
```

Both endpoints must:
- generate and persist `state` (CSRF)
- generate and persist PKCE verifier/challenge (if required by provider flow)
- include required scopes
- include exact registered redirect URI

---

## B. OAuth callback handlers

### `GET /api/v1/auth/xero/callback`
### `GET /api/v1/auth/myob/callback`

Steps:
1. Validate `state`.
2. Exchange `code` for tokens.
3. Parse identity (`sub`, `email`, profile).
4. Resolve local account by:
   - `oauth_identities(provider, provider_subject_id)` first
   - fallback by verified email (if policy allows), then bind identity
5. Return one of two outcomes:

#### Existing user
```json
{
  "status": "signed_in",
  "user_id": "...",
  "redirect": "/dashboard/upload"
}
```

#### New user
```json
{
  "status": "signup_required",
  "provider": "xero",
  "prefill": {
    "email": "...",
    "full_name": "..."
  },
  "oauth_session_token": "short_lived_token",
  "redirect": "/signup/complete"
}
```

`oauth_session_token` stores temporary trusted provider identity context for signup completion.

---

## C. Complete signup for OAuth-new users

### `POST /api/v1/auth/oauth/complete-signup`
Request:
```json
{
  "oauth_session_token": "...",
  "email": "user@example.com",
  "full_name": "User Name",
  "phone_e164": "+61400111222",
  "phone_otp_code": "123456",
  "device_fingerprint": "..."
}
```

Server checks:
1. Validate `oauth_session_token`.
2. Validate required fields (`email`, `full_name`, `phone_e164`).
3. Validate phone format (`+61...`).
4. Verify OTP.
5. Trial anti-abuse checks:
   - phone not previously trial-claimed
   - IP policy check (`ip_already_registered` currently used)
6. Create user and bind `oauth_identities` row.
7. Grant trial entitlement.
8. Return signed-in session/JWT + redirect.

Response:
```json
{
  "status": "signed_in",
  "user_id": "...",
  "redirect": "/dashboard/upload"
}
```

---

## D. Link/unlink additional providers (post-login)

### `POST /api/v1/auth/oauth/link/start?provider=xero|myob`
### `GET /api/v1/auth/oauth/link/callback`

Allow existing users to connect additional provider identities safely.

---

## Error codes (stable)

Keep existing and add OAuth-specific stable codes:
- `invalid_state`
- `oauth_code_exchange_failed`
- `provider_identity_missing`
- `oauth_session_expired`
- `email_required`
- `full_name_required`
- `invalid_phone_e164`
- `phone_verification_required`
- `trial_already_claimed_for_phone`
- `ip_already_registered`
- `account_already_exists_with_different_identity`

---

## Security checklist

- Strict redirect URI allowlist (exact match).
- State nonce one-time use.
- PKCE where applicable.
- Encrypt refresh tokens at rest.
- Never expose provider secrets to frontend.
- Session fixation protection after callback.
- Rate limit on auth + OTP endpoints.
- Audit log for OAuth login/signup/link events.

---

## Marketplace readiness notes

## Xero
- Use official OAuth/OIDC flow (`openid profile email` for sign-in identity).
- Follow Xero branding for sign-in button and naming.
- Keep scope minimal for auth; request accounting scopes only when needed.

## MYOB
- OAuth 2.0 with HTTPS redirect URI.
- Use current SME scopes.
- Include `prompt=consent` when company file selection metadata is needed.
- Persist MYOB `businessId` per linked connection.

---

## Implementation plan (short)

1. **Backend schema + storage**
   - add `oauth_identities` table
   - migration + indexes

2. **Provider clients**
   - add Xero auth service
   - add MYOB auth service

3. **Auth endpoints**
   - `/start`, `/callback`, `/oauth/complete-signup`

4. **Frontend routing**
   - wire Xero/MYOB buttons to `/start`
   - add `/signup/complete` page for OAuth-prefilled flow

5. **Trial/risk integration**
   - reuse existing OTP + phone/IP anti-abuse checks

6. **Verification**
   - existing user OAuth login
   - new user OAuth + OTP completion
   - phone/IP abuse rejection paths
   - provider reconnect/link

---

## Decision to confirm before coding

1. Email collision policy:
   - If OAuth email equals existing local email without prior provider binding:
     - A) auto-link after user confirms signed-in session
     - B) block and require explicit account recovery

2. IP uniqueness strictness:
   - Keep hard one-IP-one-signup, or shift to risk-scored throttling.

3. Trial grant trigger:
   - Grant trial only after OTP complete (recommended).

