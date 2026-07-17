import pytest

import database


def test_free_onboarding_contract():
    spec = database.onboarding_plan("free", 10)
    assert spec["max_seats"] == 10
    assert spec["monthly_seat_charge_usd"] == 0.0
    assert spec["donation_bps"] == 500
    with pytest.raises(ValueError, match="up to 10"):
        database.onboarding_plan("free", 11)


def test_team_onboarding_contract():
    spec = database.onboarding_plan("team", 11)
    assert spec["monthly_seat_charge_usd"] == 220.0
    with pytest.raises(ValueError, match="at least 11"):
        database.onboarding_plan("team", 10)


def test_enterprise_onboarding_contract():
    spec = database.onboarding_plan("enterprise", 100)
    assert spec["savings_share_bps"] == 1000
    assert spec["audit_access"] is True
