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
    assert spec["seat_price_usd"] == 10.0
    assert spec["monthly_seat_charge_usd"] == 110.0

    # Small teams pay the $49/mo floor (CI-anchored 2026-07-20: $8-12/seat
    # add-on band with a $49-99 small-team tier)
    small = database.onboarding_plan("team", 3)
    assert small["monthly_seat_charge_usd"] == 49.0

    # Once seats outprice the floor, per-seat math wins
    edge = database.onboarding_plan("team", 5)
    assert edge["monthly_seat_charge_usd"] == 50.0


def test_enterprise_onboarding_contract():
    spec = database.onboarding_plan("enterprise", 100)
    assert spec["savings_share_bps"] == 1000
    assert spec["audit_access"] is True
