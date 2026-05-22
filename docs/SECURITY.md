# SECURITY.md

The security model for PymtHouse. This is the contract PymtHouse owes to
its users and operator. Don't relax it without an explicit decision logged
in an exec-plan.

## Trust boundaries

```
  Internet
     │ HTTPS (TLS terminated at reverse proxy in prod, plain HTTP in dev)
     ▼
  pymthouse-gateway  ← app dev requests are authenticated here
     │
     ├── Postgres (TCP, password auth, internal network only)
     │
     ├── service-registry-daemon (UDS, no auth; filesystem trust)
     │
     └── payment-daemon          (UDS, no auth; filesystem trust)
             │
             └── V3 keystore (read once at daemon boot, decrypted in memory)
```

There are three explicit trust transitions:

1. **Internet → pymthouse-gateway:** authenticated. Either an API key
   (app-dev surface) or a web session cookie (portal/admin UI).
2. **pymthouse-gateway → Postgres:** TCP with username/password from env.
   The DB is on the same Docker network. Not exposed externally.
3. **pymthouse-gateway → daemons:** Unix socket. Shared volume
   (`livepeer-run`) with mode `0o660`. Trust is filesystem-mediated:
   only processes with matching uid/gid (`65532`) can connect.

The keystore is read by `payment-daemon` only, at boot. PymtHouse and the
keystore are not on the same trust path.

## Key custody

**PymtHouse never touches the Ethereum signing key.** The key is owned by
`payment-daemon`:

- Loaded from a go-ethereum V3 JSON keystore at boot via `--keystore-path`.
- Decrypted using a password supplied via `--keystore-password-file` or
  the `LIVEPEER_KEYSTORE_PASSWORD` environment variable (mutually exclusive).
- Held in memory as `*ecdsa.PrivateKey`. Never logged. Password buffer
  zeroed after decrypt.
- One process, one wallet, no multi-tenancy at the key layer.

PymtHouse's role is restricted to calling the daemon's `CreatePayment` RPC.
The key cannot be exfiltrated through PymtHouse; the daemon does not expose
a "sign arbitrary data" endpoint to the sender RPC surface.

**Operational requirements:**

- The keystore file and password file are mounted into `payment-daemon`
  with `:ro` and are not on a `pymthouse-gateway`-accessible mount.
- The password is never committed. In prod, it comes from a secret manager
  or orchestration platform; in dev, `.dev/keystore/keystore-password` is
  in `.gitignore`.
- Rotating the wallet means generating a new keystore, redeploying
  `payment-daemon` with the new files, and updating any on-chain bonded
  signing-address references (see `docs/references/payment-daemon.md`).

## API key handling

- API keys are generated server-side using `secrets.token_urlsafe(32)`.
- The key is shown to the user **exactly once**, at creation, in the
  portal UI. There is no "show key" button.
- The key is stored as `sha256(pepper || key)` in `api_keys.hash`. The
  pepper is a process-wide secret loaded from `API_KEY_HASH_PEPPER`.
- A `prefix` (first 8 chars of the key, plus a fixed identifier prefix
  like `pymth_live_`) is stored unhashed for display in the UI: "the key
  starting with `pymth_live_abcd1234…`".
- Lookup is by `prefix`, hash check is constant-time
  (`hmac.compare_digest`).
- Revocation flips `revoked_at` on the row. Revoked keys are kept for
  audit; they're not deleted.
- Rate-limiting on API key validation failures is in scope for MVP
  (configurable, default 30/min/IP).

## Sessions (portal + admin)

- Session cookies are HTTP-only, `Secure` (in prod), `SameSite=Lax`.
- Session value is a 256-bit random token; the server stores the hash and
  a session-state row in `user_sessions`.
- Expiry is 14 days for the portal, 8 hours for the admin console.
- Sign-out invalidates the session row.

## OAuth (Google, GitHub)

- Standard OAuth 2.0 / OIDC code flow. PKCE on both.
- Client ID and secret come from env (`GOOGLE_OAUTH_CLIENT_ID`,
  `GOOGLE_OAUTH_CLIENT_SECRET`, etc.). Never committed.
- State parameter is server-generated, single-use, expires in 5 minutes.
- On callback, the verified email becomes the user's primary identity.
  Linking an OAuth provider to an existing email-password account is
  possible only if the email matches.
- The OAuth provider's user ID is stored in `user_oauth_identities` so a
  user can re-sign-in even if they change their email later.

## Email verification

- On signup, a verification token (`secrets.token_urlsafe(32)`) is mailed
  to the user. Token is stored as a hash in `user_email_verifications`.
- The token is single-use. Verification flips `users.email_verified_at`.
- Tokens expire in 24 hours; expired tokens can be re-requested.
- An operator can see unverified users in the admin queue but cannot
  approve them until verification completes.

## Operator admin

- Operators have a separate `operators` table. There is no
  user-to-operator promotion path in MVP.
- The admin console uses `Authorization: Bearer <admin-token>` from
  `localStorage`. The token is a 256-bit random value supplied to the
  operator out-of-band on operator creation.
- A separate audit log captures every operator action that mutates user
  state: `operator_audit { operator_id, action, target_user_id?, params, created_at }`.

## Secrets

All secrets come from environment variables and are documented in
`.env.example`. The current secrets:

| Env var | What |
|---|---|
| `DATABASE_URL` | Postgres connection string (contains password) |
| `API_KEY_HASH_PEPPER` | Pepper for API key hashing |
| `SESSION_SECRET` | Signing key for session cookies |
| `RESEND_API_KEY` | Email provider credential (optional; falls back to a no-op `NullEmail` in dev) |
| `GOOGLE_OAUTH_CLIENT_ID` / `_SECRET` | Google OAuth |
| `GITHUB_OAUTH_CLIENT_ID` / `_SECRET` | GitHub OAuth |
| `METRICS_TOKEN` | Bearer token for `/metrics` endpoint |
| `ADMIN_BOOTSTRAP_TOKEN` | One-time token to create the first operator |
| `LIVEPEER_KEYSTORE_PASSWORD` | Passed to `payment-daemon`, never read by gateway |

`.env` is git-ignored. `.env.example` lives in the repo with dev-safe
defaults and clear "MUST CHANGE FOR PROD" comments where applicable.

## Inputs we don't trust

- Anything in a `Content-Type: application/json` body. Parse with Pydantic;
  do not `dict.get(...)` your way through it.
- Anything in headers (`Idempotency-Key`, `Authorization`, OAuth state).
  Validate format before use.
- The orchestrator-side response to a ticket. We don't see it directly,
  but if a future feature does (e.g., to capture redemption telemetry),
  parse it strictly.

## What we don't protect against (MVP)

- A malicious operator. PymtHouse trusts its operator implicitly. An
  operator who wants to over-charge users can do so via the admin UI.
- A compromised host. If `pymthouse-gateway` is rooted, the attacker can
  read the DB and forge API responses. Defense-in-depth at the host level
  is the operator's responsibility.
- A subpoenaed signing wallet. PymtHouse does not implement geographic
  fencing or compliance-driven blocking. The operator is responsible for
  policy.
- Side-channel timing attacks on the daemon's signing routine. The daemon
  is responsible; we treat it as a black box.

When in doubt about whether something is in PymtHouse's threat model, ask.
Log the answer in this file.
