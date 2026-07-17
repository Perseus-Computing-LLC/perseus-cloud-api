"""
accounts.py — User account management: registration, login, email verification, password reset.

Uses bcrypt for password hashing and JWT for session tokens.
"""

import logging
import os
import secrets
import uuid
from datetime import datetime, timezone, timedelta

import bcrypt
from fastapi import Request, HTTPException, Depends
from fastapi.responses import JSONResponse
from jose import jwt, JWTError

from database import (
    create_user,
    get_user_by_email,
    get_user_by_id,
    verify_user_email,
    update_user_password,
    create_verification_token,
    consume_verification_token,
    create_session,
    get_session,
    delete_session,
    onboarding_plan,
)
from database import create_api_key as db_create_api_key

logger = logging.getLogger("perseus_cloud.accounts")

# ── Configuration ────────────────────────────────────────────────────────────

JWT_SECRET = os.getenv("JWT_SECRET", secrets.token_hex(32))
JWT_ALGORITHM = "HS256"
SESSION_EXPIRY_HOURS = int(os.getenv("SESSION_EXPIRY_HOURS", "168"))  # 7 days
VERIFICATION_EXPIRY_HOURS = int(os.getenv("VERIFICATION_EXPIRY_HOURS", "24"))

SIGNUP_ENABLED = os.getenv("SIGNUP_ENABLED", "true").lower() in ("1", "true", "yes")

# ── Email sending (pluggable) ────────────────────────────────────────────────

SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY", "")
FROM_EMAIL = os.getenv("FROM_EMAIL", "noreply@perseus.observer")
BASE_URL = os.getenv("API_BASE_URL", "https://perseus-cloud-api.run.app")


async def send_email(to: str, subject: str, body: str) -> bool:
    """Send email. Uses SendGrid if configured, otherwise logs to stdout."""
    if SENDGRID_API_KEY:
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    "https://api.sendgrid.com/v3/mail/send",
                    headers={
                        "Authorization": f"Bearer {SENDGRID_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "personalizations": [{"to": [{"email": to}]}],
                        "from": {"email": FROM_EMAIL},
                        "subject": subject,
                        "content": [{"type": "text/plain", "value": body}],
                    },
                    timeout=10,
                )
                return resp.status_code == 202
        except Exception as e:
            logger.error("send_email_failed", extra={"error": str(e)})
            return False
    else:
        logger.info("email_would_send", extra={"to": to, "subject": subject, "body": body[:200]})
        return True


# ── Token helpers ────────────────────────────────────────────────────────────

def make_jwt(user_id: int, email: str, expiry_hours: int = SESSION_EXPIRY_HOURS) -> str:
    """Create a JWT session token."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "email": email,
        "iat": now,
        "exp": now + timedelta(hours=expiry_hours),
        "jti": secrets.token_hex(8),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_jwt(token: str) -> dict | None:
    """Decode and validate a JWT. Returns payload or None."""
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError:
        return None


def make_verification_token() -> str:
    """Generate a secure random verification token."""
    return secrets.token_urlsafe(48)


def hash_password(password: str) -> str:
    """Hash a password with bcrypt."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a password against its bcrypt hash."""
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def make_tenant_id() -> str:
    """Generate a unique tenant ID."""
    return f"t_{uuid.uuid4().hex[:20]}"


# ── Auth dependency ──────────────────────────────────────────────────────────

async def authenticate_user(request: Request) -> dict:
    """Validate session cookie/header and return user dict."""
    # Try cookie first, then Authorization header
    token = request.cookies.get("perseus_session")
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]

    if not token:
        raise HTTPException(status_code=401, detail="Authentication required")

    # Check DB session first (opaque token), then JWT
    session = await get_session(token)
    if session:
        user = await get_user_by_id(session["user_id"])
        if user:
            return user

    # Try JWT
    payload = decode_jwt(token)
    if payload and payload.get("sub"):
        user = await get_user_by_id(int(payload["sub"]))
        if user:
            return user

    raise HTTPException(status_code=401, detail="Invalid or expired session")


# ── Routes ───────────────────────────────────────────────────────────────────

async def handle_register(request: Request) -> JSONResponse:
    """POST /api/accounts/register — Create a new user account."""
    if not SIGNUP_ENABLED:
        raise HTTPException(status_code=403, detail="Signup is currently disabled")

    body = await request.json()
    email = (body.get("email", "") or "").strip().lower()
    password = body.get("password", "") or ""
    requested_seats = body.get("seats", 1)
    requested_plan = (body.get("plan", "free") or "free").lower()

    # Validation
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Valid email is required")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    try:
        onboarding = onboarding_plan(requested_plan, requested_seats)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=402, detail=str(exc))
    if onboarding["plan"] != "free":
        raise HTTPException(
            status_code=402,
            detail="Paid plans require checkout before activation; Free signup supports up to 10 seats.",
            headers={"X-Upgrade-Required": "true"},
        )

    # Check for duplicate
    existing = await get_user_by_email(email)
    if existing:
        if existing["is_verified"]:
            raise HTTPException(status_code=409, detail="An account with this email already exists")
        else:
            raise HTTPException(
                status_code=409,
                detail="An unverified account with this email exists. Check your inbox or request a new verification email.",
            )

    # Create user
    password_hash = hash_password(password)
    tenant_id = make_tenant_id()
    user_id = await create_user(
        email, password_hash, tenant_id,
        plan=onboarding["plan"], seat_count=onboarding["seat_count"],
    )

    # Generate verification token
    ver_token = make_verification_token()
    expires = (datetime.now(timezone.utc) + timedelta(hours=VERIFICATION_EXPIRY_HOURS)).isoformat()
    await create_verification_token(user_id, ver_token, "email_verification", expires)

    # Send verification email
    verify_url = f"{BASE_URL}/api/accounts/verify?token={ver_token}"
    email_sent = await send_email(
        email,
        "Verify your Perseus Cloud account",
        f"Welcome to Perseus Cloud!\n\n"
        f"Click this link to verify your email:\n{verify_url}\n\n"
        f"This link expires in {VERIFICATION_EXPIRY_HOURS} hours.\n\n"
        f"If you did not create this account, you can ignore this email.",
    )

    logger.info("user_registered", extra={"user_id": user_id, "email": email, "email_sent": email_sent})

    return JSONResponse({
        "message": "Account created. Check your email to verify your address.",
        "email_sent": email_sent,
        "tenant_id": tenant_id,
        "onboarding": {
            **onboarding,
            "audit_endpoint": "/api/v1/audit",
            "savings_tally": "enabled after the first verified usage event",
        },
    }, status_code=201)


async def handle_verify_email(request: Request) -> JSONResponse:
    """GET /api/accounts/verify?token=... — Verify an email address."""
    token = request.query_params.get("token", "")
    if not token:
        raise HTTPException(status_code=400, detail="Verification token is required")

    row = await consume_verification_token(token, "email_verification")
    if not row:
        raise HTTPException(status_code=400, detail="Invalid, expired, or already used verification token")

    await verify_user_email(row["user_id"])
    logger.info("email_verified", extra={"user_id": row["user_id"]})

    return JSONResponse({"message": "Email verified. You can now log in."})


async def handle_login(request: Request) -> JSONResponse:
    """POST /api/accounts/login — Authenticate and return session token."""
    body = await request.json()
    email = (body.get("email", "") or "").strip().lower()
    password = body.get("password", "") or ""

    if not email or not password:
        raise HTTPException(status_code=400, detail="Email and password are required")

    user = await get_user_by_email(email)
    if not user or not verify_password(password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not user["is_verified"]:
        raise HTTPException(status_code=403, detail="Email not verified. Check your inbox.")

    # Create session
    session_token = make_jwt(user["id"], user["email"])
    expires = (datetime.now(timezone.utc) + timedelta(hours=SESSION_EXPIRY_HOURS)).isoformat()
    await create_session(user["id"], session_token, expires)

    logger.info("user_login", extra={"user_id": user["id"], "email": email})

    resp = JSONResponse({
        "token": session_token,
        "user": {
            "id": user["id"],
            "email": user["email"],
            "tenant_id": user["tenant_id"],
            "is_verified": bool(user["is_verified"]),
            "created_at": user["created_at"],
        },
    })
    resp.set_cookie(
        key="perseus_session",
        value=session_token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=SESSION_EXPIRY_HOURS * 3600,
    )
    return resp


async def handle_logout(request: Request) -> JSONResponse:
    """POST /api/accounts/logout — Invalidate session."""
    token = request.cookies.get("perseus_session") or ""
    if token:
        await delete_session(token)
    resp = JSONResponse({"message": "Logged out"})
    resp.delete_cookie("perseus_session")
    return resp


async def handle_me(request: Request) -> JSONResponse:
    """GET /api/accounts/me — Return the current user."""
    user = await authenticate_user(request)
    return JSONResponse({
        "id": user["id"],
        "email": user["email"],
        "tenant_id": user["tenant_id"],
        "is_verified": bool(user["is_verified"]),
        "created_at": user["created_at"],
    })


async def handle_password_reset_request(request: Request) -> JSONResponse:
    """POST /api/accounts/password-reset — Request a password reset email."""
    body = await request.json()
    email = (body.get("email", "") or "").strip().lower()

    if not email:
        raise HTTPException(status_code=400, detail="Email is required")

    user = await get_user_by_email(email)
    if user:
        reset_token = make_verification_token()
        expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        await create_verification_token(user["id"], reset_token, "password_reset", expires)

        reset_url = f"{BASE_URL}/api/accounts/password-reset/confirm?token={reset_token}"
        await send_email(
            email,
            "Reset your Perseus Cloud password",
            f"A password reset was requested for your account.\n\n"
            f"Click this link to reset your password:\n{reset_url}\n\n"
            f"This link expires in 1 hour.\n\n"
            f"If you did not request this, you can ignore this email.",
        )

    return JSONResponse({
        "message": "If an account with that email exists, a reset link has been sent.",
    })


async def handle_password_reset_confirm(request: Request) -> JSONResponse:
    """POST /api/accounts/password-reset/confirm — Confirm password reset with token."""
    body = await request.json()
    token = (body.get("token", "") or "")
    new_password = body.get("password", "") or ""

    if not token:
        raise HTTPException(status_code=400, detail="Reset token is required")
    if len(new_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    row = await consume_verification_token(token, "password_reset")
    if not row:
        raise HTTPException(status_code=400, detail="Invalid, expired, or already used reset token")

    password_hash = hash_password(new_password)
    await update_user_password(row["user_id"], password_hash)

    logger.info("password_reset", extra={"user_id": row["user_id"]})

    return JSONResponse({"message": "Password reset successfully. You can now log in."})


async def handle_onboarding(request: Request) -> JSONResponse:
    """GET /api/accounts/onboarding — return the authenticated tenant contract."""
    user = await authenticate_user(request)
    plan = user.get("plan", "free")
    seats = int(user.get("seat_count", 1))
    onboarding = onboarding_plan(plan, seats)
    return JSONResponse({
        "tenant_id": user["tenant_id"],
        "email_verified": bool(user["is_verified"]),
        "onboarding": {
            **onboarding,
            "audit_endpoint": "/api/v1/audit",
            "savings_tally": "enabled after the first verified usage event",
        },
    })


async def handle_api_key_create(request: Request) -> JSONResponse:
    """POST /api/accounts/api-keys — Create a new API key for the authenticated user."""
    user = await authenticate_user(request)

    api_key = "pcs_" + secrets.token_hex(24)
    await db_create_api_key(api_key, tier="starter")

    logger.info("api_key_created", extra={"user_id": user["id"]})

    return JSONResponse({
        "api_key": api_key,
        "message": "API key created. Store it securely — it won't be shown again.",
    }, status_code=201)
