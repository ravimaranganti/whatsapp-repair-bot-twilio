"""WhatsApp Flow options stay on the existing trade and area ids."""

import json

from app.services.onboarding import TRADE_CHOICES
from app.services.pro_flow import (
    FLOW_NAME,
    OTHER_TRADE,
    TRADE_OPTIONS,
    ZONE_OPTIONS,
    flow_pages,
    normalize_saved_trades,
    parse_flow_submission,
)
from app.services.service_areas import AREA_CHOICES


def test_flow_options_match_the_onboarding_lists():
    assert [key for key, _label in TRADE_OPTIONS[:-1]] == [key for key, _label in TRADE_CHOICES]
    assert TRADE_OPTIONS[-1] == (OTHER_TRADE, "Other")
    assert [key for key, _label in ZONE_OPTIONS] == [key for key, _label, _note in AREA_CHOICES]
    assert len(TRADE_OPTIONS) <= 20
    assert len(ZONE_OPTIONS) <= 20
    pages = flow_pages()
    assert pages[0]["id"] == "SELECT_TRADES"
    assert pages[1]["id"] == "SELECT_ZONES"
    assert pages[0]["layout"][-1]["label"] == "Next: Service Areas"
    assert pages[1]["layout"][-1]["label"] == "Save Profile"
    assert FLOW_NAME == "pro_onboarding_profile_flow"


def test_flow_reply_keeps_other_for_the_follow_up():
    raw = json.dumps(
        {
            "selected_trades": ["plumbing", "other", "painting"],
            "selected_zones": ["katy", "all"],
        }
    )
    pick = parse_flow_submission(raw)
    assert pick is not None and pick.ok
    assert pick.wants_other is True
    assert pick.trades == ["plumbing", "handyman"]
    assert pick.zones == ["all"]


def test_page_payload_uses_the_same_ids():
    raw = {
        "pages": [
            {
                "id": "SELECT_TRADES",
                "items": [{"name": "selected_trades", "value": ["hvac", "trade:other"]}],
            },
            {
                "id": "SELECT_ZONES",
                "items": [{"name": "selected_zones", "value": ["sugar_land", "the_woodlands"]}],
            },
        ]
    }
    pick = parse_flow_submission(raw)
    assert pick is not None
    assert pick.trades == ["hvac"]
    assert pick.wants_other is True
    assert pick.zones == ["sugar_land", "the_woodlands"]
    assert parse_flow_submission("not json") is None


def test_dashboard_trades_keep_custom_text_on_the_array():
    saved = normalize_saved_trades(["plumbing", "other"], "roofing")
    assert saved == (["plumbing", "roofing"], "roofing")
    pool = normalize_saved_trades(["handyman"], "pool and lawn")
    assert pool[0] == ["handyman", "pool", "lawn"]
    assert normalize_saved_trades(["other"], "") == "Tell us the other trade in a few words."
    assert normalize_saved_trades([], "") == "Choose at least one trade."
