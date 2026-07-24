# Perseus Cloud API

Hosted Perseus Vault persistent-memory API with Stripe subscriptions.
Production: `https://cloud-api.perseus.observer` (FastAPI/uvicorn behind Cloudflare).

## Health endpoint contract (stable, unauthenticated)

`GET /api/health`

- **Auth:** none required — safe for external uptime probes.
- **Status codes:** always `200` when the process is serving; a `5xx`/timeout
  means the service (or its front proxy) is down.
- **Body:**

```json
{"status": "healthy", "mimir": "connected", "version": "1.0.0"}
```

| Field | Values | Meaning |
|---|---|---|
| `status` | `healthy` / `degraded` | `degraded` when the Vault backend is disconnected |
| `mimir` | `connected` / `disconnected` | live state of the Vault connection (legacy response-field name) |
| `version` | semver string | API version |

- **Side effect:** when the Vault connection is down, one serialized reconnect
  attempt is made (async lock) before answering — probes double as self-healing.
- **Monitoring guidance:** liveness = HTTP 200. Deep health = 200 **and**
  body contains `"status": "healthy"` (a `degraded` body still returns 200 by
  design, so a plain 200 check alone will not catch Vault-backend outages).
- Root (`/`) and `/health` return `404` by design — do not point monitors there.

## Perseus public health conventions (all four production endpoints)

| Endpoint | Expected | Notes |
|---|---|---|
| `https://cloud-api.perseus.observer/api/health` | 200 + `"status": "healthy"` | this contract |
| `https://plutus.perseus.observer/healthz` | 200 + `"ok": true` | minimal liveness `{ok, version, demo}`; no data/balances/orgs |
| `https://perseus.observer/cloud/signup` | 301 → `/cloud/signup/` → 200 | follow redirects, expect final 200 |
| `https://vault.perseus.observer/message` | 401 unauthenticated | bearer-protected by design; monitor with an authenticated JSON-RPC `tools/call` (`perseus_vault_health`; legacy aliases remain supported) or explicitly accept 401 — never weaken auth for monitoring |
Hosted Perseus Vault with Stripe subscriptions and user account management.
Production: `https://cloud-api.perseus.observer` (Docker, port 8080, see `docker-compose.production.yml`).

## Health endpoint contract

| | |
|---|---|
| URL | `GET /api/health` |
| Auth | **None required** (safe for public uptime probes) |
| Success | HTTP 200 |
| Body | `{"status": "healthy" \| "degraded", "mimir": "connected" \| "disconnected", "version": "1.0.0"}` |
| `status` | `healthy` when the embedded Vault client is connected, else `degraded` — HTTP stays 200 either way |
| Side effects | One serialized Vault reconnect attempt if the connection was lost |

Monitoring guidance: expect HTTP 200 and match `\"status\":\"healthy\"` in the body.
Note: `/` and `/health` return 404 by design — do not point probes there.

## Authenticated endpoints

`POST /api/v1/remember`, `POST /api/v1/recall`, `POST /api/v1/search`, `GET /api/v1/entities/{id}`,
`POST /webhook/stripe` — all require their respective credentials.

---
*Health contract documented 2026-07-19 after live verification against production.*
