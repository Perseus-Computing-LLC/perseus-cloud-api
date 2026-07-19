import pytest

import database


@pytest.mark.asyncio
async def test_funnel_event_records_only_allowlisted_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATABASE_PATH", str(tmp_path / "funnel.db"))
    await database.init_db()

    await database.record_funnel_event(
        tenant_id="t_test",
        event_name="signup_created",
        source="design_partner",
    )

    metrics = await database.get_funnel_metrics(days=7)
    assert metrics == {"signup_created": 1}


@pytest.mark.asyncio
async def test_funnel_event_rejects_unknown_event_name(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATABASE_PATH", str(tmp_path / "funnel.db"))
    await database.init_db()

    with pytest.raises(ValueError, match="unsupported funnel event"):
        await database.record_funnel_event(
            tenant_id="t_test",
            event_name="password=never-store-this",
            source="design_partner",
        )
