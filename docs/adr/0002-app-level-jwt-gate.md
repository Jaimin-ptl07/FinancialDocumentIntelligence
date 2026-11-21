---
status: accepted
---

# App-level JWT authentication as the gate, not proxy-level auth

For the gated demo, we chose application-level JWT auth in FastAPI (users table, short-lived access token + rotating refresh token in an httpOnly cookie) over zero-code alternatives (reverse-proxy basic auth, Cloudflare Access), despite the latter being cheaper to build. The reason: the gate is designed to be *removed and replaced by growth* — the same deployment is intended to become a public demo or a product — and an in-app identity layer upgrades to signup, roles, and per-user quotas additively, while a proxy-level wall gets torn out and rebuilt when that day comes.

## Considered Options

- **App-level JWT** (chosen) — most code now (users table, `/auth/*` routes, frontend login), zero re-architecture later.
- **Caddy basic auth** — no code, per-person credentials, but a dead end for the product path and no per-user semantics for rate limiting.
- **Cloudflare Access** — real identities, free ≤50 users, but adds a third vendor in the request path and moves the gate somewhere the app can't see.

## Consequences

- Caddy remains in the deployment, but only as TLS terminator and same-origin path router (`/` → frontend, `/api/*` → backend) — not as the authenticator. Same-origin routing also eliminates the CORS allowlist (`FDI_CORS_ORIGINS`) as a prod concern.
- Rate limiting keys off authenticated user identity rather than IP, so the concurrency cap on LLM routes is enforced per user at job-enqueue time.
- Presigned MinIO artifact URLs must be routed through the same public origin (MinIO stays network-internal), which is build-time work in the proxy config, not an app change.
