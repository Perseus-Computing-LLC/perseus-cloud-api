"""
database.py — SQLite database for API keys, usage tracking, and subscription state.

Tables:
  - api_keys:    key, tier, stripe_customer_id, stripe_subscription_id, created_at
  - usage:       api_key, entity_count, workspace_count, last_reset
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
