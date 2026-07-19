# Perseus Cloud API

Hosted Perseus Vault with Stripe subscriptions and user account management.
Production: `https://cloud-api.perseus.observer` (Docker, port 8080, see `docker-compose.production.yml`).

## Health endpoint contract

| | |
|---|---|
| URL | `GET /api/health` |
| Auth | **None required** (safe for public uptime probes) |
| Success | HTTP 200 |
| Body | `{"status": "healthy" \| "degraded", "mimir": "connected" \| "disconnected", "version": "1.0.0"}` |
| `status` | `healthy` when the embedded Vault (mimir) client is connected, else `degraded` — HTTP stays 200 either way |
| Side effects | One serialized Vault reconnect attempt if the connection was lost |

Monitoring guidance: expect HTTP 200 and match `\"status\":\"healthy\"` in the body.
Note: `/` and `/health` return 404 by design — do not point probes there.

## Authenticated endpoints

`POST /api/v1/remember`, `POST /api/v1/recall`, `POST /api/v1/search`, `GET /api/v1/entities/{id}`,
`POST /webhook/stripe` — all require their respective credentials.

---
*Health contract documented 2026-07-19 after live verification against production.*
