"""Unit tests for pro ranking."""

from app.services.matching import public_pro_card, rank_pros


def _pro(name, avail, rate, rating, phone="1"):
    return {
        "phone": phone,
        "name": name,
        "business_name": name,
        "availability_text": avail,
        "rate_text": rate,
        "rating_avg": rating,
        "trades": ["plumbing"],
    }


def test_high_severity_prefers_fast():
    pros = [
        _pro("Slow", "Within a week", "$50-60/hr", 5.0, "1"),
        _pro("Fast", "Same day", "$150-200/hr", 4.0, "2"),
    ]
    ranked = rank_pros(pros, "emergency")
    assert ranked[0]["name"] == "Fast"


def test_low_severity_prefers_cheap():
    pros = [
        _pro("Pricey", "Same day", "$200/hr", 5.0, "1"),
        _pro("Budget", "Within a week", "$65/hr", 4.0, "2"),
    ]
    ranked = rank_pros(pros, "low")
    assert ranked[0]["name"] == "Budget"


def test_public_card_hides_phone():
    card = public_pro_card(_pro("Mike", "Same day", "$100", 4.5, "17135550100"))
    assert "phone" not in card
    assert card["name"] == "Mike"
