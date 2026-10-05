# Xero Marketplace Readiness Checklist (LedgerSnaps)

## Identity & OAuth
- [ ] Sign in with Xero entry is visible on auth page.
- [ ] OAuth flow uses exact registered redirect URI.
- [ ] `state` is generated server-side and validated on callback.
- [ ] Uses OIDC scopes for identity: `openid profile email`.
- [ ] Additional accounting scopes are justified and minimal.

## Account Linking Behavior
- [ ] Existing bound Xero identity signs in directly.
- [ ] Existing local email can be safely linked to Xero identity.
- [ ] New Xero user goes to profile completion (phone + OTP) before trial grant.
- [ ] Trial grant blocked by phone/IP anti-abuse rules.

## Security & Data Handling
- [ ] Xero client secret is server-side only.
- [ ] Refresh tokens stored server-side only.
- [ ] Session token created only after successful callback or signup completion.
- [ ] Error responses are actionable and non-sensitive.

## App Listing Compliance Artifacts
- [ ] Public Terms & Conditions URL available.
- [ ] Public Privacy Policy URL available.
- [ ] App website URL and support contact URL available.
- [ ] Clear app name/branding consistent with LedgerSnaps listing.

## UX/Failure Handling
- [ ] OAuth cancel/failure returns user to auth UI with clear message.
- [ ] Missing/expired oauth signup session returns stable error.
- [ ] Tenant info captured if returned by Xero connections endpoint.

## Regression Checks
- [ ] Existing API auth flow still works (dev-token, /auth/me).
- [ ] Xero draft upload endpoints unaffected for connected users.
- [ ] Core health endpoint remains green post deploy.
