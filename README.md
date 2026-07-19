# Perseus Cloud API

Hosted Mimir persistent-memory API with Stripe subscriptions.
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
| `status` | `healthy` / `degraded` | `degraded` when the Vault (Mimir) backend is disconnected |
| `mimir` | `connected` / `disconnected` | live state of the Vault connection |
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
| `https://vault.perseus.observer/message` | 401 unauthenticated | bearer-protected by design; monitor with an authenticated JSON-RPC `tools/call` (`mimir_health`) or explicitly accept 401 — never weaken auth for monitoring |
