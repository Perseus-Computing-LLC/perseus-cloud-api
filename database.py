"""
database.py — SQLite database for API keys, usage tracking, subscription state, and user accounts.

Tables:
  - api_keys:    key, tier, stripe_customer_id, stripe_subscription_id, created_at
  - usage:       api_key, entity_count, workspace_count, last_reset
  - users:       id, email, password_hash, is_verified, tenant_id, created_at, updated_at
  - verification_tokens:  id, user_id, token, token_type, expires_at, used
"""

import aiosqlite
import os
import time
from datetime import datetime, timezone

DATABASE_PATH = os.getenv("DATABASE_PATH", os.path.join(os.path.dirname(__file__), "perseus_cloud.db"))


async def get_db() -> aiosqlite.Connection:
    """Get a database connection (caller must close)."""
    db = await aiosqlite.connect(DATABASE_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def init_db() -> None:
    """Create tables if they don't exist."""
    db = await get_db()
    try:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS api_keys (
                key TEXT PRIMARY KEY,
                tier TEXT NOT NULL DEFAULT 'starter',
                stripe_customer_id TEXT,
                stripe_subscription_id TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                is_active INTEGER NOT NULL DEFAULT 1
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS usage (
                api_key TEXT PRIMARY KEY,
                entity_count INTEGER NOT NULL DEFAULT 0,
                workspace_count INTEGER NOT NULL DEFAULT 0,
                last_reset TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (api_key) REFERENCES api_keys(key)
            )
        """)
        # User accounts
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                is_verified INTEGER NOT NULL DEFAULT 0,
                tenant_id TEXT UNIQUE NOT NULL,
                stripe_customer_id TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS verification_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                token TEXT UNIQUE NOT NULL,
                token_type TEXT NOT NULL DEFAULT 'email_verification',
                expires_at TEXT NOT NULL,
                used INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                session_token TEXT UNIQUE NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)
        # Additive onboarding columns for existing deployments.
        for column, definition in (
            ("plan", "TEXT NOT NULL DEFAULT 'free'"),
            ("seat_count", "INTEGER NOT NULL DEFAULT 1"),
        ):
            try:
                await db.execute(f"ALTER TABLE users ADD COLUMN {column} {definition}")
            except Exception:
                pass  # already migrated
        await db.commit()
    finally:
        await db.close()


# Tier limits
TIER_LIMITS = {
    "starter": {
        "max_entities": 1000,
        "max_workspaces": 1,
        "encryption": False,
        "priority": False,
    },
    "pro": {
        "max_entities": 10000,
        "max_workspaces": 5,
        "encryption": True,
        "priority": False,
    },
    "team": {
        "max_entities": 100000,
        "max_workspaces": None,  # unlimited
        "encryption": True,
        "priority": True,
    },
}


# Canonical Cloud onboarding contract. Billing remains in Plutus; this metadata
# lets signup and the Cloud API render the same rules without reimplementing math.
#
# Pricing anchored 2026-07-20 per competitive-intel sweep (Vault:
# competitive-intel/landscape-2026-07-20): GitHub bundles team memory +
# governance free on the $19 Copilot Business seat, and Kiro charges zero
# premium for team plans — dashboards/SSO/analytics are table stakes, so Team
# is priced as an add-on band (~25-50% of a base assistant seat) with a
# small-team monthly floor, and margin lives at Enterprise.
CLOUD_PLAN_CONTRACT = {
    "free": {"max_seats": 10, "seat_price_usd": 0.0,
             "donation_bps": 500, "savings_share_bps": 0,
             "audit_access": True},
    "team": {"min_seats": 1, "seat_price_usd": 10.0, "min_monthly_usd": 49.0,
             "donation_bps": 0, "savings_share_bps": 0,
             "audit_access": True},
    "enterprise": {"seat_price_usd": None, "donation_bps": 0,
                    "savings_share_bps": 1000, "audit_access": True},
}


def onboarding_plan(plan: str = "free", seats: int = 1) -> dict:
    plan = (plan or "free").lower()
    if plan not in CLOUD_PLAN_CONTRACT:
        raise ValueError(f"unsupported onboarding plan: {plan}")
    seats = int(seats)
    if seats < 1:
        raise ValueError("seat_count must be at least 1")
    spec = dict(CLOUD_PLAN_CONTRACT[plan])
    if plan == "free" and seats > spec["max_seats"]:
        raise ValueError("Free supports up to 10 seats; larger teams need Team")
    if plan == "team" and seats < spec["min_seats"]:
        raise ValueError(f"Team requires at least {spec['min_seats']} seat(s)")
    spec.update({"plan": plan, "seat_count": seats})
    if spec["seat_price_usd"] is None:
        spec["monthly_seat_charge_usd"] = None
    else:
        charge = round(seats * spec["seat_price_usd"], 2)
        floor = spec.get("min_monthly_usd")
        if floor is not None:
            charge = max(charge, floor)
        spec["monthly_seat_charge_usd"] = charge
    return spec


# ── API Key Operations ──────────────────────────────────────────────────────

async def get_api_key_info(key: str) -> dict | None:
    """Get API key details or None if not found/inactive."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT key, tier, stripe_customer_id, stripe_subscription_id, created_at, is_active "
            "FROM api_keys WHERE key = ? AND is_active = 1",
            (key,),
        )
        row = await cursor.fetchone()
        if row:
            return dict(row)
        return None
    finally:
        await db.close()


async def create_api_key(
    key: str,
    tier: str = "starter",
    stripe_customer_id: str | None = None,
    stripe_subscription_id: str | None = None,
) -> None:
    """Insert a new API key."""
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR REPLACE INTO api_keys (key, tier, stripe_customer_id, stripe_subscription_id) "
            "VALUES (?, ?, ?, ?)",
            (key, tier, stripe_customer_id, stripe_subscription_id),
        )
        await db.execute(
            "INSERT OR IGNORE INTO usage (api_key) VALUES (?)",
            (key,),
        )
        await db.commit()
    finally:
        await db.close()


async def update_subscription(
    stripe_customer_id: str, tier: str, stripe_subscription_id: str
) -> None:
    """Update tier for a customer (called from webhook)."""
    db = await get_db()
    try:
        await db.execute(
            "UPDATE api_keys SET tier = ?, stripe_subscription_id = ? "
            "WHERE stripe_customer_id = ?",
            (tier, stripe_subscription_id, stripe_customer_id),
        )
        await db.commit()
    finally:
        await db.close()


async def cancel_subscription(stripe_customer_id: str) -> None:
    """Downgrade to starter on subscription cancel."""
    db = await get_db()
    try:
        await db.execute(
            "UPDATE api_keys SET tier = 'starter', stripe_subscription_id = NULL "
            "WHERE stripe_customer_id = ?",
            (stripe_customer_id,),
        )
        await db.commit()
    finally:
        await db.close()


async def get_usage(key: str) -> dict:
    """Get current usage for an API key."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT entity_count, workspace_count, last_reset FROM usage WHERE api_key = ?",
            (key,),
        )
        row = await cursor.fetchone()
        if row:
            return dict(row)
        return {"entity_count": 0, "workspace_count": 0, "last_reset": None}
    finally:
        await db.close()


async def increment_entity_count(key: str, delta: int = 1) -> dict:
    """Increment entity count and return updated usage with limit check."""
    db = await get_db()
    try:
        await db.execute(
            "UPDATE usage SET entity_count = entity_count + ? WHERE api_key = ?",
            (delta, key),
        )
        await db.commit()
        return await get_usage(key)
    finally:
        await db.close()


async def set_workspace_count(key: str, count: int) -> None:
    """Set workspace count for an API key."""
    db = await get_db()
    try:
        await db.execute(
            "UPDATE usage SET workspace_count = ? WHERE api_key = ?",
            (count, key),
        )
        await db.commit()
    finally:
        await db.close()


def check_tier_limits(tier: str, usage: dict) -> tuple[bool, str | None]:
    """Check if usage is within tier limits. Returns (allowed, error_message)."""
    limits = TIER_LIMITS.get(tier, TIER_LIMITS["starter"])
    entity_count = usage.get("entity_count", 0)
    workspace_count = usage.get("workspace_count", 0)

    max_entities = limits["max_entities"]
    max_workspaces = limits["max_workspaces"]

    if max_entities and entity_count >= max_entities:
        return False, f"Entity limit reached ({entity_count}/{max_entities}). Upgrade your plan."
    if max_workspaces and workspace_count >= max_workspaces:
        return False, f"Workspace limit reached ({workspace_count}/{max_workspaces}). Upgrade your plan."
    return True, None


# ── User Account Operations ─────────────────────────────────────────────────

async def create_user(email: str, password_hash: str, tenant_id: str,
                     plan: str = "free", seat_count: int = 1) -> int:
    """Create a new user and initialize its Cloud onboarding contract."""
    onboarding = onboarding_plan(plan, seat_count)
    db = await get_db()
    try:
        try:
            cursor = await db.execute(
                "INSERT INTO users (email, password_hash, tenant_id, plan, seat_count) VALUES (?, ?, ?, ?, ?)",
                (email, password_hash, tenant_id, onboarding["plan"], onboarding["seat_count"]),
            )
            await db.commit()
            return cursor.lastrowid
        except aiosqlite.IntegrityError:
            raise ValueError(f"Email already registered: {email}")
    finally:
        await db.close()


async def get_user_by_email(email: str) -> dict | None:
    """Get user by email or None."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, email, password_hash, is_verified, tenant_id, stripe_customer_id, "
            "plan, seat_count, created_at, updated_at FROM users WHERE email = ?",
            (email,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def get_user_by_id(user_id: int) -> dict | None:
    """Get user by ID or None."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, email, password_hash, is_verified, tenant_id, stripe_customer_id, "
            "plan, seat_count, created_at, updated_at FROM users WHERE id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def verify_user_email(user_id: int) -> None:
    """Mark a user's email as verified."""
    db = await get_db()
    try:
        await db.execute(
            "UPDATE users SET is_verified = 1, updated_at = datetime('now') WHERE id = ?",
            (user_id,),
        )
        await db.commit()
    finally:
        await db.close()


async def update_user_password(user_id: int, password_hash: str) -> None:
    """Update a user's password hash."""
    db = await get_db()
    try:
        await db.execute(
            "UPDATE users SET password_hash = ?, updated_at = datetime('now') WHERE id = ?",
            (password_hash, user_id),
        )
        await db.commit()
    finally:
        await db.close()


# ── Verification Token Operations ───────────────────────────────────────────

async def create_verification_token(user_id: int, token: str, token_type: str,
                                    expires_at: str) -> None:
    """Create a verification/reset token."""
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO verification_tokens (user_id, token, token_type, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, token, token_type, expires_at),
        )
        await db.commit()
    finally:
        await db.close()


async def consume_verification_token(token: str, token_type: str) -> dict | None:
    """Consume a verification token and return the token row if valid/unexpired/unused.
    Returns None if invalid/expired/already-used."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, user_id, token, token_type, expires_at, used FROM verification_tokens "
            "WHERE token = ? AND token_type = ? AND used = 0 AND expires_at > datetime('now')",
            (token, token_type),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        # Mark as used
        await db.execute(
            "UPDATE verification_tokens SET used = 1 WHERE id = ?", (row["id"],)
        )
        await db.commit()
        return dict(row)
    finally:
        await db.close()


# ── Session Operations ──────────────────────────────────────────────────────

async def create_session(user_id: int, session_token: str, expires_at: str) -> None:
    """Create a new session."""
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO sessions (user_id, session_token, expires_at) VALUES (?, ?, ?)",
            (user_id, session_token, expires_at),
        )
        await db.commit()
    finally:
        await db.close()


async def get_session(session_token: str) -> dict | None:
    """Get session by token. Returns None if expired or not found."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, user_id, session_token, expires_at, created_at FROM sessions "
            "WHERE session_token = ? AND expires_at > datetime('now')",
            (session_token,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def delete_session(session_token: str) -> None:
    """Delete a session (logout)."""
    db = await get_db()
    try:
        await db.execute("DELETE FROM sessions WHERE session_token = ?", (session_token,))
        await db.commit()
    finally:
        await db.close()
