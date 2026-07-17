"""
stripe_handler.py — Stripe Checkout + Customer Portal + Webhooks.

Follows the stripe-subscriptions skill pattern exactly:
- NEVER use .get() on StripeObject (use attribute access)
- Validate price IDs start with 'price_'
- Use idempotency keys for checkout
"""

import os
import uuid
import logging

import stripe

logger = logging.getLogger("perseus_cloud.stripe")

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_BASE_URL = os.getenv("API_BASE_URL", "https://perseus-cloud-api.run.app")

PRICE_IDS = {
    "pro": os.getenv("STRIPE_PRICE_PRO", ""),
    "team": os.getenv("STRIPE_PRICE_TEAM", ""),
}

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


def create_checkout_session(plan: str, customer_email: str | None = None) -> dict:
    """Create a Stripe Checkout session for a subscription plan.

    Plan 'starter' is free — no Stripe checkout needed (handled in route).
    """
    if not STRIPE_SECRET_KEY:
        raise RuntimeError("STRIPE_SECRET_KEY not configured")

    if plan == "starter":
        raise ValueError(
            "Starter plan is free — no checkout session needed. "
            "Generate an API key directly."
        )

    price_id = PRICE_IDS.get(plan)
    if not price_id or not price_id.startswith("price_"):
        raise ValueError(
            f"Price ID for '{plan}' is not a valid Stripe price id. "
            "Create products in Stripe Dashboard and set "
            "STRIPE_PRICE_PRO/TEAM to the 'price_...' values."
        )

    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=f"{STRIPE_BASE_URL}/success.html?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{STRIPE_BASE_URL}/cancel.html",
        customer_email=customer_email,
        allow_promotion_codes=True,
        billing_address_collection="auto",
        metadata={"plan": plan},
        idempotency_key=str(uuid.uuid4()),
    )
    logger.info("checkout_session_created", extra={"plan": plan, "session_id": session.id})
    return {"url": session.url, "session_id": session.id}


def create_portal_session(session_id: str) -> dict:
    """Create a Stripe Customer Portal session for subscription management.

    PITFALL: retrieve() returns a StripeObject, NOT a dict.
    Use session.customer (attribute access), NOT session.get("customer").
    """
    if not STRIPE_SECRET_KEY:
        raise RuntimeError("STRIPE_SECRET_KEY not configured")

    try:
        checkout_session = stripe.checkout.Session.retrieve(session_id)
    except stripe.error.StripeError as e:
        logger.error("portal_retrieve_failed", extra={"session_id": session_id, "error": str(e)})
        raise ValueError(f"Could not retrieve checkout session: {e}")

    customer_id = checkout_session.customer  # attribute — NOT .get()
    if not customer_id:
        raise ValueError(
            "Checkout session has no customer — was the session completed?"
        )

    portal = stripe.billing_portal.Session.create(
        customer=customer_id,
        return_url=f"{STRIPE_BASE_URL}/",
    )
    logger.info("portal_session_created", extra={"customer": customer_id})
    return {"url": portal.url}


def handle_webhook(payload: bytes, signature: str) -> dict:
    """Process a Stripe webhook event."""
    if not STRIPE_WEBHOOK_SECRET:
        raise RuntimeError("STRIPE_WEBHOOK_SECRET not configured")

    try:
        event = stripe.Webhook.construct_event(
            payload, signature, STRIPE_WEBHOOK_SECRET
        )
    except stripe.error.SignatureVerificationError:
        logger.error("webhook_signature_invalid")
        raise

    event_type = event["type"]
    data = event["data"]["object"]  # webhook payloads are dicts, not StripeObjects

    if event_type == "checkout.session.completed":
        customer_id = data.get("customer")
        subscription_id = data.get("subscription")
        plan = data.get("metadata", {}).get("plan", "unknown")
        email = data.get("customer_details", {}).get("email", "unknown")
        logger.info(
            "subscription_started",
            extra={
                "customer": customer_id,
                "subscription": subscription_id,
                "plan": plan,
                "email": email,
            },
        )

    elif event_type == "customer.subscription.deleted":
        logger.info("subscription_cancelled", extra={"customer": data.get("customer")})

    elif event_type == "invoice.paid":
        customer_id = data.get("customer")
        amount = data.get("amount_paid", 0) / 100
        logger.info("invoice_paid", extra={"customer": customer_id, "amount": amount})

    elif event_type == "invoice.payment_failed":
        logger.warning("payment_failed", extra={"customer": data.get("customer")})

    return {"status": "processed", "event": event_type}
