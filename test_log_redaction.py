import pytest

import accounts


def test_redact_email_preserves_domain_without_local_part():
    assert accounts._redact_email("member@example.com") == "m***@example.com"


def test_redact_email_rejects_malformed_value():
    assert accounts._redact_email("not-an-email") == "invalid-email"


@pytest.mark.asyncio
async def test_unconfigured_email_delivery_fails_closed(monkeypatch):
    monkeypatch.setattr(accounts, "SENDGRID_API_KEY", "")
    assert await accounts.send_email("member@example.com", "Subject", "secret body") is False
