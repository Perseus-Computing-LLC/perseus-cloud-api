"""test_funnel.py — Privacy-safe activation funnel metrics (issue #4) and
founder welcome email (issue #7).

Run with: pytest test_funnel.py -v
"""

import json
import os
import secrets
import tempfile

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch

import database
import funnel
import accounts
from database import init_db


@pytest_asyncio.fixture()
async def tmp_db():
    """Point the database layer at a throwaway SQLite file."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    old = database.DATABASE_PATH
    database.DATABASE_PATH = path
    await init_db()
    yield path
    database.DATABASE_PATH = old
    os.unlink(path)


class FakeRequest:
    def __init__(self, body=None, headers=None, query=None, cookies=None):
        self._body = body or {}
        self.headers = headers or {}
        self.query_params = query or {}
        self.cookies = cookies or {}

    async def json(self):
        return self._body


# ── Schema privacy boundary (#4 acceptance) ──────────────────────────────────

class TestSchemaPrivacy:
    @pytest.mark.asyncio
    async def test_funnel_table_has_no_sensitive_columns(self, tmp_db):
        """The table can physically store only tenant id, event, source, ts."""
        db = await database.get_db()
        try:
            cursor = await db.execute("PRAGMA table_info(funnel_events)")
            cols = {row["name"] for row in await cursor.fetchall()}
        finally:
            await db.close()
        assert cols == {"id", "tenant_id", "event", "source", "created_at"}
        forbidden = {"email", "password", "password_hash", "token", "session",
                     "api_key", "url", "body", "payload", "content", "ip",
                     "user_agent", "verification_url"}
        assert not (cols & forbidden)

    @pytest.mark.asyncio
    async def test_record_event_signature_cannot_accept_payloads(self, tmp_db):
        """record_event has no parameter for payloads/emails/tokens."""
        import inspect
        params = set(inspect.signature(funnel.record_event).parameters)
        assert params == {"tenant_id", "event", "source"}

    @pytest.mark.asyncio
    async def test_unknown_event_rejected(self, tmp_db):
        with pytest.raises(ValueError, match="unknown funnel event"):
            await funnel.record_event("t_abc", "page_viewed")
        with pytest.raises(ValueError, match="unknown funnel event"):
            await funnel.record_event("t_abc", "password_reset")
        with pytest.raises(ValueError, match="unknown funnel source"):
            await funnel.record_event("t_abc", "signup_created", "https://evil.example/x")
        with pytest.raises(ValueError, match="tenant_id"):
            await funnel.record_event("", "signup_created")


# ── Recording and correlation (#4 acceptance) ────────────────────────────────

class TestRecording:
    @pytest.mark.asyncio
    async def test_full_funnel_correlated_by_tenant(self, tmp_db):
        t = f"t_{secrets.token_hex(8)}"
        for event in funnel.FUNNEL_STAGES:
            await funnel.record_event(t, event, "web")
        report = await funnel.funnel_report(days=7)
        for stage in report["stages"]:
            assert stage["tenants"] == 1, stage
        for conv in report["conversions"]:
            assert conv["rate"] == 1.0, conv

    @pytest.mark.asyncio
    async def test_conversion_rates_drop_off(self, tmp_db):
        for i in range(4):
            await funnel.record_event(f"t_a{i}", "signup_created", "web")
        for i in range(2):
            await funnel.record_event(f"t_a{i}", "email_verified", "email")
        await funnel.record_event("t_a0", "login_succeeded", "web")
        report = await funnel.funnel_report(days=7)
        stages = {s["event"]: s["tenants"] for s in report["stages"]}
        assert stages["signup_created"] == 4
        assert stages["email_verified"] == 2
        assert stages["login_succeeded"] == 1
        conv = {f"{c['from']}->{c['to']}": c["rate"] for c in report["conversions"]}
        assert conv["signup_created->email_verified"] == 0.5
        assert conv["email_verified->login_succeeded"] == 0.5

    @pytest.mark.asyncio
    async def test_report_is_aggregate_only(self, tmp_db):
        """Serialized report must contain no tenant identifiers (#4)."""
        t = f"t_{secrets.token_hex(8)}"
        await funnel.record_event(t, "signup_created", "web")
        report = await funnel.funnel_report(days=7)
        assert t not in json.dumps(report)

    @pytest.mark.asyncio
    async def test_window_excludes_old_events(self, tmp_db):
        db = await database.get_db()
        try:
            await db.execute(
                "INSERT INTO funnel_events (tenant_id, event, source, created_at) "
                "VALUES ('t_old', 'signup_created', 'web', datetime('now', '-30 days'))"
            )
            await db.commit()
        finally:
            await db.close()
        report = await funnel.funnel_report(days=7)
        stage = next(s for s in report["stages"] if s["event"] == "signup_created")
        assert stage["tenants"] == 0
        report30 = await funnel.funnel_report(days=30)
        stage30 = next(s for s in report30["stages"] if s["event"] == "signup_created")
        assert stage30["tenants"] == 1


# ── Operator endpoint auth (#4: operator-only) ───────────────────────────────

class TestOperatorEndpoint:
    @pytest.mark.asyncio
    async def test_unconfigured_is_503(self, tmp_db):
        with patch.object(funnel, "OPERATOR_TOKEN", ""):
            with pytest.raises(Exception) as exc:
                await funnel.handle_funnel_report(FakeRequest())
            assert getattr(exc.value, "status_code", None) == 503

    @pytest.mark.asyncio
    async def test_wrong_token_is_401(self, tmp_db):
        with patch.object(funnel, "OPERATOR_TOKEN", "secret-op-token"):
            with pytest.raises(Exception) as exc:
                await funnel.handle_funnel_report(
                    FakeRequest(headers={"Authorization": "Bearer nope"}))
            assert getattr(exc.value, "status_code", None) == 401

    @pytest.mark.asyncio
    async def test_valid_token_returns_report(self, tmp_db):
        with patch.object(funnel, "OPERATOR_TOKEN", "secret-op-token"):
            resp = await funnel.handle_funnel_report(FakeRequest(
                headers={"Authorization": "Bearer secret-op-token"}))
            assert resp.status_code == 200
            body = json.loads(resp.body)
            assert body["window_days"] == 7
            assert len(body["stages"]) == len(funnel.FUNNEL_STAGES)


# ── Handler emission points (#4) ─────────────────────────────────────────────

class TestEmissions:
    @pytest.mark.asyncio
    async def test_register_emits_signup_created(self, tmp_db):
        with patch.object(accounts, "send_email", new=AsyncMock(return_value=True)):
            req = FakeRequest(body={"email": f"u-{secrets.token_hex(4)}@example.com",
                                    "password": "test-password-123"})
            resp = await accounts.handle_register(req)
            assert resp.status_code == 201
        report = await funnel.funnel_report(days=7)
        stage = next(s for s in report["stages"] if s["event"] == "signup_created")
        assert stage["tenants"] == 1

    @pytest.mark.asyncio
    async def test_onboarding_emits_dashboard_opened_only_when_authed(self, tmp_db):
        from database import create_user
        email = f"u-{secrets.token_hex(4)}@example.com"
        uid = await create_user(email, accounts.hash_password("test-password-123"),
                                accounts.make_tenant_id())
        # Unauthenticated → 401, no event
        with pytest.raises(Exception) as exc:
            await accounts.handle_onboarding(FakeRequest())
        assert getattr(exc.value, "status_code", None) == 401
        report = await funnel.funnel_report(days=7)
        assert next(s for s in report["stages"]
                    if s["event"] == "dashboard_opened")["tenants"] == 0
        # Authenticated → event fires
        token = accounts.make_jwt(uid, email)
        resp = await accounts.handle_onboarding(
            FakeRequest(headers={"Authorization": f"Bearer {token}"}))
        assert resp.status_code == 200
        report = await funnel.funnel_report(days=7)
        assert next(s for s in report["stages"]
                    if s["event"] == "dashboard_opened")["tenants"] == 1


# ── Founder welcome email (#7) ───────────────────────────────────────────────

class TestFounderWelcome:
    @pytest.mark.asyncio
    async def test_welcome_email_shape(self):
        with patch.object(accounts, "send_email", new=AsyncMock(return_value=True)) as mock_send:
            sent = await accounts.send_founder_welcome("newuser@example.com")
            assert sent is True
            mock_send.assert_awaited_once()
            args, kwargs = mock_send.await_args
            to, subject, body = args[0], args[1], args[2]
            assert to == "newuser@example.com"
            assert "what are you building?" in subject.lower()
            # Empty-name fallback: never a broken "Hi ," merge field (#7 pitfall)
            assert body.startswith("Hi there,")
            assert "Hi ," not in body
            # Reply goes to a human; founder-signed
            assert kwargs.get("reply_to") == accounts.FOUNDER_REPLY_TO
            assert kwargs.get("from_email") == accounts.FOUNDER_FROM_EMAIL
            # Short and human: 3-4 sentences' worth of body, no HTML
            assert "<" not in body and ">" not in body
            assert len(body) < 1200

    @pytest.mark.asyncio
    async def test_welcome_email_can_be_disabled(self):
        with patch.object(accounts, "WELCOME_EMAIL_ENABLED", False):
            with patch.object(accounts, "send_email", new=AsyncMock()) as mock_send:
                assert await accounts.send_founder_welcome("x@example.com") is False
                mock_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_register_sends_welcome_and_verification(self, tmp_db):
        with patch.object(accounts, "send_email", new=AsyncMock(return_value=True)) as mock_send:
            req = FakeRequest(body={"email": f"u-{secrets.token_hex(4)}@example.com",
                                    "password": "test-password-123"})
            resp = await accounts.handle_register(req)
            assert resp.status_code == 201
            assert mock_send.await_count == 2  # verification + founder welcome
            subjects = [c.args[1] for c in mock_send.await_args_list]
            assert any("Verify" in s for s in subjects)
            assert any("what are you building?" in s.lower() for s in subjects)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
