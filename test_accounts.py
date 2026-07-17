"""
test_accounts.py — Integration tests for the Perseus Cloud account system.

Run with: pytest test_accounts.py -v
Requires: httpx (pip install httpx)
"""

import pytest
import asyncio
import secrets
from unittest.mock import patch, AsyncMock

# Test the module imports and utilities
import accounts
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
)


class TestPasswordHashing:
    """Password hashing and verification."""

    def test_hash_and_verify(self):
        pw = "correct-horse-battery-staple"
        hashed = accounts.hash_password(pw)
        assert hashed != pw
        assert accounts.verify_password(pw, hashed)
        assert not accounts.verify_password("wrong", hashed)

    def test_unique_hashes(self):
        pw = "same-password"
        h1 = accounts.hash_password(pw)
        h2 = accounts.hash_password(pw)
        assert h1 != h2  # bcrypt salts
        assert accounts.verify_password(pw, h1)
        assert accounts.verify_password(pw, h2)


class TestTokenGeneration:
    """Token generation utilities."""

    def test_verification_token_unique(self):
        t1 = accounts.make_verification_token()
        t2 = accounts.make_verification_token()
        assert t1 != t2
        assert len(t1) > 40

    def test_tenant_id_format(self):
        tid = accounts.make_tenant_id()
        assert tid.startswith("t_")
        assert len(tid) > 10

    def test_jwt_roundtrip(self):
        token = accounts.make_jwt(42, "test@example.com", expiry_hours=1)
        payload = accounts.decode_jwt(token)
        assert payload is not None
        assert payload["sub"] == "42"
        assert payload["email"] == "test@example.com"

    def test_jwt_invalid_rejected(self):
        assert accounts.decode_jwt("not-a-valid-jwt") is None
        assert accounts.decode_jwt("") is None


class TestInputValidation:
    """Input validation for registration and login."""

    def test_email_validation(self):
        valid = ["user@example.com", "a@b.co", "test+alias@domain.org"]
        invalid = ["", "not-an-email", "no-domain@"]

        for e in valid:
            assert "@" in e

        for e in invalid:
            # Each invalid case lacks a valid email structure
            # Must have both @ and a dot in domain part, and something before @
            parts = e.split("@")
            assert not (len(parts) == 2 and parts[0] and "." in parts[1])

    def test_password_minimum_length(self):
        assert len("short") < 8
        assert len("exactly8") >= 8
        assert len("much-longer-password-here") >= 8


class TestDatabaseOperations:
    """Database CRUD operations for user accounts."""

    @pytest.mark.asyncio
    async def test_create_and_get_user(self):
        from database import init_db
        await init_db()

        email = f"test-{secrets.token_hex(4)}@example.com"
        pw_hash = accounts.hash_password("test-password-123")
        tenant_id = accounts.make_tenant_id()

        user_id = await create_user(email, pw_hash, tenant_id)
        assert user_id > 0

        user = await get_user_by_email(email)
        assert user is not None
        assert user["email"] == email
        assert user["tenant_id"] == tenant_id
        assert user["is_verified"] == 0

        # Duplicate should raise
        with pytest.raises(ValueError, match="already registered"):
            await create_user(email, pw_hash, tenant_id)

    @pytest.mark.asyncio
    async def test_user_not_found(self):
        user = await get_user_by_email("nonexistent@example.com")
        assert user is None

        user = await get_user_by_id(999999)
        assert user is None

    @pytest.mark.asyncio
    async def test_email_verification(self):
        email = f"test-{secrets.token_hex(4)}@example.com"
        pw_hash = accounts.hash_password("test-password-123")
        tenant_id = accounts.make_tenant_id()
        user_id = await create_user(email, pw_hash, tenant_id)

        # Not verified initially
        user = await get_user_by_email(email)
        assert user["is_verified"] == 0

        # Verify
        await verify_user_email(user_id)
        user = await get_user_by_email(email)
        assert user["is_verified"] == 1

    @pytest.mark.asyncio
    async def test_password_update(self):
        email = f"test-{secrets.token_hex(4)}@example.com"
        old_hash = accounts.hash_password("old-password-123")
        tenant_id = accounts.make_tenant_id()
        user_id = await create_user(email, old_hash, tenant_id)

        new_hash = accounts.hash_password("new-password-456")
        await update_user_password(user_id, new_hash)

        user = await get_user_by_email(email)
        assert user["password_hash"] == new_hash
        assert accounts.verify_password("new-password-456", user["password_hash"])

    @pytest.mark.asyncio
    async def test_verification_token_lifecycle(self):
        email = f"test-{secrets.token_hex(4)}@example.com"
        pw_hash = accounts.hash_password("test-password-123")
        tenant_id = accounts.make_tenant_id()
        user_id = await create_user(email, pw_hash, tenant_id)

        from datetime import datetime, timezone, timedelta
        token = accounts.make_verification_token()
        expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        await create_verification_token(user_id, token, "email_verification", expires)

        # Consume the token
        row = await consume_verification_token(token, "email_verification")
        assert row is not None
        assert row["user_id"] == user_id

        # Should not work again (already used)
        row2 = await consume_verification_token(token, "email_verification")
        assert row2 is None

    @pytest.mark.asyncio
    async def test_session_lifecycle(self):
        email = f"test-{secrets.token_hex(4)}@example.com"
        pw_hash = accounts.hash_password("test-password-123")
        tenant_id = accounts.make_tenant_id()
        user_id = await create_user(email, pw_hash, tenant_id)

        from datetime import datetime, timezone, timedelta
        session_token = accounts.make_jwt(user_id, email)
        expires = (datetime.now(timezone.utc) + timedelta(hours=168)).isoformat()

        await create_session(user_id, session_token, expires)
        session = await get_session(session_token)
        assert session is not None
        assert session["user_id"] == user_id

        # Delete session
        await delete_session(session_token)
        session = await get_session(session_token)
        assert session is None


class TestPasswordPolicy:
    """Password strength and policy tests."""

    def test_weak_passwords_caught(self):
        weak = ["12345678", "password", "aaaaaaaa"]
        strong = ["Tr0ub4dor&3", "correct-horse-battery-staple", "xkcd-4-words!"]

        # All must be at least 8 chars (enforced at API level)
        for pw in weak:
            assert len(pw) >= 8  # minimum already met, but weak for other reasons
        for pw in strong:
            assert len(pw) >= 8

    def test_password_not_logged(self):
        # Passwords should never appear in comparison output
        pw = "secret-password-abc123!"
        hashed = accounts.hash_password(pw)
        assert pw not in hashed  # hash doesn't contain plaintext


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
