"""funnel.py — Privacy-safe design-partner activation funnel metrics (issue #4).

Design contract:
- The funnel_events schema can record ONLY: internal tenant id, event name,
  coarse source label, timestamp. There are no columns for payloads, emails,
  tokens, URLs, or request data — sensitive fields cannot be recorded.
- Events are restricted to a fixed vocabulary; unknown names are rejected.
- Reporting is aggregate-only (counts + stage conversion rates). There is
  intentionally no per-tenant event-history query anywhere in this module.
"""

import logging
import os
from datetime import datetime, timezone, timedelta

from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse

from database import get_db

logger = logging.getLogger("perseus_cloud.funnel")

# Ordered funnel stages (order defines the conversion-rate pairs).
FUNNEL_STAGES = [
    "signup_created",
    "email_verified",
    "login_succeeded",
    "dashboard_opened",
    "plutus_audit_link_clicked",
    "first_usage_recorded",
    "savings_available",
    "optional_donation_started",
    "optional_donation_completed",
]
VALID_EVENTS = frozenset(FUNNEL_STAGES)

# Coarse source labels only — never URLs, user agents, or request data.
VALID_SOURCES = frozenset({"web", "api", "email", "stripe", "system"})

OPERATOR_TOKEN = os.getenv("OPERATOR_TOKEN", "")


async def record_event(tenant_id: str, event: str, source: str = "system") -> bool:
    """Record one funnel event. Raises ValueError on unknown event/source.

    The signature is the privacy boundary: there is no parameter through
    which a payload, email, token, or URL could enter the table.
    """
    if not tenant_id or not isinstance(tenant_id, str):
        raise ValueError("tenant_id is required")
    if event not in VALID_EVENTS:
        raise ValueError(f"unknown funnel event: {event!r}")
    if source not in VALID_SOURCES:
        raise ValueError(f"unknown funnel source: {source!r}")
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO funnel_events (tenant_id, event, source) VALUES (?, ?, ?)",
            (tenant_id, event, source),
        )
        await db.commit()
        return True
    finally:
        await db.close()


async def try_record(tenant_id: str | None, event: str, source: str = "system") -> None:
    """Best-effort emission: funnel recording must never break a request."""
    if not tenant_id:
        return
    try:
        await record_event(tenant_id, event, source)
    except Exception as exc:  # noqa: BLE001 — intentionally swallow-all
        logger.warning("funnel_record_failed", extra={
            "event": event, "error_type": type(exc).__name__,
        })


async def funnel_report(days: int = 7) -> dict:
    """Aggregate conversion counts and rates over the trailing window.

    Returns per-stage distinct-tenant counts and stage-to-stage conversion
    rates. Aggregate only — no tenant identifiers are returned.
    """
    days = max(1, min(int(days), 90))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT event, COUNT(DISTINCT tenant_id) AS tenants, COUNT(*) AS events "
            "FROM funnel_events WHERE created_at >= ? GROUP BY event",
            (since,),
        )
        rows = {r["event"]: dict(r) for r in await cursor.fetchall()}
    finally:
        await db.close()

    stages = []
    for name in FUNNEL_STAGES:
        row = rows.get(name)
        stages.append({
            "event": name,
            "tenants": row["tenants"] if row else 0,
            "events": row["events"] if row else 0,
        })

    conversions = []
    for prev, cur in zip(stages, stages[1:]):
        rate = (cur["tenants"] / prev["tenants"]) if prev["tenants"] else None
        conversions.append({
            "from": prev["event"],
            "to": cur["event"],
            "rate": round(rate, 4) if rate is not None else None,
        })

    return {
        "window_days": days,
        "since": since,
        "stages": stages,
        "conversions": conversions,
    }


async def handle_funnel_report(request: Request) -> JSONResponse:
    """GET /api/operator/funnel — operator-only aggregate funnel report."""
    if not OPERATOR_TOKEN:
        raise HTTPException(status_code=503, detail="Operator reporting is not configured")
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    if not token or token != OPERATOR_TOKEN:
        raise HTTPException(status_code=401, detail="Operator token required")
    try:
        days = int(request.query_params.get("days", "7"))
    except ValueError:
        raise HTTPException(status_code=400, detail="days must be an integer")
    return JSONResponse(await funnel_report(days))
