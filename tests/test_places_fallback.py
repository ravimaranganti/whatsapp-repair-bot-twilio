"""Dispatch copy and the Places fallback when no partner pro matches."""

from app.services.admin_ops import job_bucket
from app.services.jobs import transition
from app.services.places import (
    dispatch_confirmation,
    no_partner_message,
    places_offer_message,
    specialists_from_places,
)


def test_dispatch_copy_hides_a_small_match_count():
    many = dispatch_confirmation(6, "plumbing", "77008")
    assert many == (
        "On it! Reaching out to 6 neighbor-referred plumbing pros near 77008"
        "—quotes usually start coming in within the hour."
    )
    few = dispatch_confirmation(2, "hvac", "77494")
    assert few == (
        "On it! Checking availability with a few trusted, neighbor-referred HVAC pros "
        "serving 77494—I'll message you as soon as a quote comes in."
    )
    assert "2" not in few


def test_places_results_require_rating_reviews_and_a_phone():
    found = specialists_from_places(
        {
            "places": [
                {
                    "displayName": {"text": "Quiet Plumbing"},
                    "formattedAddress": "1 Main St, Katy, TX 77494, USA",
                    "nationalPhoneNumber": "(281) 555-0101",
                    "rating": 4.2,
                    "userRatingCount": 400,
                },
                {
                    "displayName": {"text": "Thin Reviews"},
                    "formattedAddress": "3 Pine St, Katy, TX 77494, USA",
                    "nationalPhoneNumber": "(281) 555-0102",
                    "rating": 4.9,
                    "userRatingCount": 12,
                },
                {
                    "displayName": {"text": "No Phone Plumbing"},
                    "formattedAddress": "9 Side St, Katy, TX 77494, USA",
                    "rating": 4.9,
                    "userRatingCount": 80,
                },
                {
                    "displayName": {"text": "Bright Plumbing"},
                    "formattedAddress": "2 Oak St, Katy, TX 77494, USA",
                    "nationalPhoneNumber": "(281) 555-0199",
                    "rating": 4.8,
                    "userRatingCount": 30,
                },
                {
                    "displayName": {"text": "Solid Plumbing"},
                    "formattedAddress": "4 Elm St, Katy, TX 77494, USA",
                    "nationalPhoneNumber": "(281) 555-0144",
                    "rating": 4.6,
                    "userRatingCount": 40,
                },
            ]
        }
    )
    assert [place["name"] for place in found] == ["Bright Plumbing", "Solid Plumbing"]
    assert found[0]["address"] == "2 Oak St, Katy"
    assert found[0]["phone"] == "(281) 555-0199"


def test_places_message_and_the_empty_fallback():
    text = places_offer_message(
        "plumbing",
        "77008",
        [
            {
                "name": "Ada Plumbing",
                "phone": "(713) 555-0100",
                "address": "Houston, TX",
                "rating": 4.8,
                "reviews": 120,
            }
        ],
    )
    assert "friend- or neighbor-referred plumbing pro on standby in 77008" in text
    assert "4.6★+ with 30+ reviews" in text
    assert "*Ada Plumbing* (4.8★ · 120 reviews)" in text
    assert "📞 (713) 555-0100" in text
    assert "📍 Houston, TX" in text
    assert "flagged this specialty" in text
    empty = no_partner_message("pool", "77380")
    assert empty == (
        "I don't have a neighbor-referred pool pro on standby in 77380 "
        "just yet—give me a little time to ask around my local network and find someone solid for you!"
    )


def test_unmatched_jobs_are_a_separate_admin_bucket():
    assert job_bucket("UNMATCHED_FALLBACK", []) == "unmatched"
    assert transition("awaiting_consent", "UNMATCHED_FALLBACK") == "UNMATCHED_FALLBACK"
