"""
main.py — Perseus Cloud API: Hosted Mimir Persistent Memory

Endpoints:
  POST /api/checkout        - Create Stripe Checkout session
  POST /api/portal          - Create Stripe Customer Portal session
  POST /webhook/stripe       - Handle Stripe webhooks
  GET  /api/health           - Health check
  POST /api/v1/remember     - Proxy to mimir_remember (auth required)
  POST /api/v1/recall       - Proxy to mimir_recall (auth required)
  POST /api/v1/search       - Proxy to mimir_recall (alias, auth required)
  GET  /api/v1/entities/{id} - Proxy to mimir_get_entity (auth required)
"""

import json
import logging
import os
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import JSONResponse

import stripe_handler
from auth import authenticate, check_usage_limits
from database import (
    init_db,
    create_api_key,
    update_subscription,
    cancel_subscription,
    increment_entity_count,
    set_workspace_count,
    get_usage,
    check_tier_limits,
    TIER_LIMITS,
)
from mimir_client import mimir_client
import accounts

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("perseus_cloud")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: init DB and Mimir client. Shutdown: stop Mimir."""
    logger.info("Initializing database…")
    await init_db()
    logger.info("Starting Mimir client…")
    await mimir_client.start()
    logger.info("Perseus Cloud API ready")
    yield
    logger.info("Shutting down Mimir client…")
    await mimir_client.stop()


app = FastAPI(
    title="Perseus Cloud API",
    description="Hosted Mimir Persistent Memory — paid API with Stripe subscriptions",
    version="1.0.0",
    lifespan=lifespan,
)


# ── Billing Endpoints ────────────────────────────────────────────────────────


@app.post("/api/checkout")
async def create_checkout(request: Request) -> JSONResponse:
    """Create a Stripe Checkout session. Plans: pro, team. Starter is free."""
    body = await request.json()
    plan = body.get("plan", "pro")
    email = body.get("email")

    if plan == "starter":
        # Starter is free — generate API key directly
        api_key = "pcs_" + secrets.token_hex(24)
        await create_api_key(api_key, tier="starter")
        return JSONResponse({
            "plan": "starter",
            "api_key": api_key,
            "message": "Starter plan activated. Save your API key!",
        })

    try:
        session = stripe_handler.create_checkout_session(plan, email)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    return JSONResponse(session)


@app.post("/api/portal")
async def create_portal(request: Request) -> JSONResponse:
    """Create a Stripe Customer Portal session for subscription management."""
    body = await request.json()
    session_id = body.get("session_id", "")
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")

    try:
        portal = stripe_handler.create_portal_session(session_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    return JSONResponse(portal)


@app.post("/webhook/stripe")
async def stripe_webhook(request: Request) -> JSONResponse:
    """Handle Stripe webhook events."""
    payload = await request.body()
    signature = request.headers.get("stripe-signature", "")

    try:
        result = stripe_handler.handle_webhook(payload, signature)
    except stripe_handler.stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Update database based on webhook event
    event = result.get("event", "")
    if event == "checkout.session.completed":
        # Re-parse the event directly from webhook to get full data
        import stripe as _stripe
        try:
            event_data = _stripe.Webhook.construct_event(
                payload, signature, os.getenv("STRIPE_WEBHOOK_SECRET", "")
            )
            data = event_data["data"]["object"]
            customer_id = data.get("customer")
            subscription_id = data.get("subscription")
            plan = data.get("metadata", {}).get("plan", "pro")
            email = data.get("customer_details", {}).get("email", "unknown")

            if customer_id:
                # Generate API key for the customer
                api_key = "pcs_" + secrets.token_hex(24)
                await create_api_key(
                    api_key,
                    tier=plan,
                    stripe_customer_id=customer_id,
                    stripe_subscription_id=subscription_id,
                )
                logger.info(
                    "api_key_created",
                    extra={"customer": customer_id, "plan": plan, "email": email},
                )
        except Exception as e:
            logger.error("webhook_db_update_failed", extra={"error": str(e)})

    elif event == "customer.subscription.deleted":
        import stripe as _stripe
        try:
            event_data = _stripe.Webhook.construct_event(
                payload, signature, os.getenv("STRIPE_WEBHOOK_SECRET", "")
            )
            data = event_data["data"]["object"]
            customer_id = data.get("customer")
            if customer_id:
                await cancel_subscription(customer_id)
        except Exception as e:
            logger.error("webhook_cancel_failed", extra={"error": str(e)})

    return JSONResponse(result)


# ── Health Check ──────────────────────────────────────────────────────────────


@app.get("/api/health")
async def health() -> dict:
    """Health check endpoint."""
    mimir_healthy = mimir_client.is_connected
    return {
        "status": "healthy" if mimir_healthy else "degraded",
        "mimir": "connected" if mimir_healthy else "disconnected",
        "version": "1.0.0",
    }


# ── Mimir API v1 Endpoints ───────────────────────────────────────────────────


@app.post("/api/v1/remember")
async def remember(request: Request, key_info: dict = Depends(authenticate)) -> JSONResponse:
    """Store or update an entity. Wraps mimir_remember."""
    await check_usage_limits(key_info)
    body = await request.json()

    category = body.get("category", "default")
    key = body.get("key", "")
    content = body.get("content", "")
    entity_type = body.get("type", body.get("memory_type", "insight"))
    tags = body.get("tags", [])
    workspace_hash = body.get("workspace_hash", "")
    importance = body.get("importance", 0.5)
    topic_path = body.get("topic_path", "")
    summary = body.get("summary", "")
    status = body.get("status", "active")

    # Build body_json as a JSON string with content + summary
    body_json_obj = {"content": content}
    if summary:
        body_json_obj["summary"] = summary
    body_json_str = json.dumps(body_json_obj)

    # Ensure tags is a list of strings
    if isinstance(tags, dict):
        tags = [f"{k}:{v}" for k, v in tags.items()]
    elif not isinstance(tags, list):
        tags = []

    result, err = await mimir_client.call_tool("mimir_remember", {
        "category": category,
        "key": key,
        "body_json": body_json_str,
        "type": entity_type,
        "tags": tags,
        "workspace_hash": workspace_hash or "",
        "importance": importance,
        "topic_path": topic_path or "",
        "status": status,
    })

    if err:
        raise HTTPException(status_code=500, detail=f"Mimir error: {err}")

    # Track usage
    await increment_entity_count(key_info["key"], 1)

    return JSONResponse(result or {"status": "stored"})


@app.post("/api/v1/recall")
async def recall(request: Request, key_info: dict = Depends(authenticate)) -> JSONResponse:
    """Search entities with FTS5. Wraps mimir_recall."""
    await check_usage_limits(key_info)
    body = await request.json()

    query = body.get("query", "")
    limit = body.get("limit", body.get("max_results", 10))
    entity_type = body.get("type", body.get("memory_type", None))
    workspace_hash = body.get("workspace_hash", "")
    min_decay = body.get("min_decay", body.get("min_decay_score", 0.0))
    topic_path = body.get("topic_path", "")
    mode = body.get("mode", "hybrid")
    category = body.get("category", None)
    offset = body.get("offset", 0)
    preview_cap = body.get("preview_cap", None)

    args = {
        "query": query,
        "limit": min(limit, 1000),
        "mode": mode,
        "workspace_hash": workspace_hash or "",
        "min_decay": min_decay,
        "topic_path": topic_path or "",
        "offset": offset,
    }
    if entity_type:
        args["type"] = entity_type
    if category:
        args["category"] = category
    if preview_cap:
        args["preview_cap"] = preview_cap

    result, err = await mimir_client.call_tool("mimir_recall", args)

    if err:
        raise HTTPException(status_code=500, detail=f"Mimir error: {err}")

    return JSONResponse(result or {"entities": [], "total": 0})


@app.post("/api/v1/search")
async def search(request: Request, key_info: dict = Depends(authenticate)) -> JSONResponse:
    """Alias for /api/v1/recall — search entities via FTS5."""
    return await recall(request, key_info)


@app.get("/api/v1/entities/{entity_id}")
async def get_entity(
    entity_id: str,
    request: Request,
    key_info: dict = Depends(authenticate),
) -> JSONResponse:
    """Get an entity by ID with full body. Wraps mimir_get_entity."""
    await check_usage_limits(key_info)

    result, err = await mimir_client.call_tool("mimir_get_entity", {
        "id": entity_id,
    })

    if err:
        raise HTTPException(status_code=500, detail=f"Mimir error: {err}")

    if not result:
        raise HTTPException(status_code=404, detail="Entity not found")

    return JSONResponse(result)


@app.get("/api/v1/usage")
async def usage(request: Request, key_info: dict = Depends(authenticate)) -> JSONResponse:
    """Get current usage and tier limits for the API key."""
    key_usage = await get_usage(key_info["key"])
    tier = key_info["tier"]
    limits = TIER_LIMITS.get(tier, TIER_LIMITS["starter"])
    return JSONResponse({
        "tier": tier,
        "usage": key_usage,
        "limits": {
            "max_entities": limits["max_entities"],
            "max_workspaces": limits["max_workspaces"],
            "encryption": limits["encryption"],
            "priority": limits["priority"],
        },
    })


# ── Account Management Endpoints ──────────────────────────────────────────────


@app.post("/api/accounts/register")
async def register(request: Request) -> JSONResponse:
    """Register a new user account. Sends verification email."""
    return await accounts.handle_register(request)


@app.get("/api/accounts/verify")
async def verify_email(request: Request) -> JSONResponse:
    """Verify email address with token."""
    return await accounts.handle_verify_email(request)


@app.post("/api/accounts/login")
async def login(request: Request) -> JSONResponse:
    """Login with email and password. Returns session token + sets cookie."""
    return await accounts.handle_login(request)


@app.post("/api/accounts/logout")
async def logout(request: Request) -> JSONResponse:
    """Logout and invalidate session."""
    return await accounts.handle_logout(request)


@app.get("/api/accounts/me")
async def me(request: Request) -> JSONResponse:
    """Get current authenticated user."""
    return await accounts.handle_me(request)


@app.post("/api/accounts/password-reset")
async def password_reset_request(request: Request) -> JSONResponse:
    """Request a password reset email."""
    return await accounts.handle_password_reset_request(request)


@app.post("/api/accounts/password-reset/confirm")
async def password_reset_confirm(request: Request) -> JSONResponse:
    """Confirm password reset with token."""
    return await accounts.handle_password_reset_confirm(request)


@app.get("/api/accounts/onboarding")
async def account_onboarding(request: Request) -> JSONResponse:
    """Return the authenticated tenant's canonical Cloud onboarding contract."""
    return await accounts.handle_onboarding(request)


@app.post("/api/accounts/api-keys")
async def create_user_api_key(request: Request) -> JSONResponse:
    """Create an API key for the authenticated user."""
    return await accounts.handle_api_key_create(request)


# ── Entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
