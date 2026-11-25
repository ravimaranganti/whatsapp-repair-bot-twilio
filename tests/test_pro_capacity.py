"""Urgent-call and lead-time taps during pro onboarding."""

from app.services.pro_capacity import (
    fee_label,
    interpret_dispatch_fee,
    interpret_emergency,
    interpret_lead_time,
)


def test_emergency_buttons_and_typed_answers():
    assert interpret_emergency("Yes", "emerg_yes") is True
    assert interpret_emergency("nope", "") is False
    assert interpret_emergency("maybe", "") is None


def test_fee_buttons_and_rejects_free_text():
    assert interpret_dispatch_fee("Free Estimate", "fee_free") == "free_estimate"
    assert interpret_dispatch_fee("$50 - $99 Fee", "") == "tier_50_99"
    assert interpret_dispatch_fee("$100+ Fee", "fee_100_plus") == "tier_100_plus"
    assert interpret_dispatch_fee("about 75 bucks", "") is None
    assert fee_label("tier_50_99") == "$50–$99 service call fee"
    assert fee_label("tier_100_plus") == "$100+ service call fee"


def test_lead_time_buttons_and_typed_answers():
    assert interpret_lead_time("Same Day", "lead_same_day") == "same_day"
    assert interpret_lead_time("next 48 hours", "") == "next_48_hours"
    assert interpret_lead_time("3+ days", "") == "three_plus_days"
    assert interpret_lead_time("whenever", "") is None
