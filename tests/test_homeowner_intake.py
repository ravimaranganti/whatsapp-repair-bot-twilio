"""ZIP and first name captured during a homeowner request, not before it."""

import asyncio

from app.services.homeowner_profile import (
    first_time_zip_prompt,
    parse_visit_reply,
    parse_zip_and_name,
    read_zip_and_name,
    returning_zip_message,
    zip_only_update,
)


def test_zip_and_first_name_parse_without_a_form():
    assert parse_zip_and_name("77008") == ("77008", "")
    assert parse_zip_and_name("Roberto 77008") == ("77008", "Roberto")
    assert parse_zip_and_name("I'm Roberto, 77008") == ("77008", "Roberto")
    assert parse_zip_and_name("77008-1234") == ("77008", "")
    assert zip_only_update("different zip 77024") == "77024"
    assert zip_only_update("my kitchen sink is leaking in 77008") == ""


def test_prompts_match_the_intake_copy():
    first = first_time_zip_prompt("plumbing")
    assert "Got it—I can check with local plumbing pros" in first
    assert "5-digit ZIP code" in first
    assert "street" not in first.lower()
    back = returning_zip_message("Roberto", "hvac", "77008")
    assert back.startswith("On it, Roberto! Checking with local HVAC pros near *77008*")
    assert "different ZIP" in back
    solo = returning_zip_message("", "plumbing", "77494")
    assert solo.startswith("On it! Checking with local plumbing pros")


def test_sentence_zip_uses_structured_parse():
    async def run():
        async def fake_parse(message: str) -> dict:
            assert "Roberto" in message
            return {"zip_code": "77008", "first_name": "Roberto"}

        from app.services import gemini as gemini_mod

        original = gemini_mod.gemini_service.parse_homeowner_intro
        gemini_mod.gemini_service.parse_homeowner_intro = fake_parse
        try:
            parsed = await read_zip_and_name(
                "I live near the heights and my name is Roberto"
            )
        finally:
            gemini_mod.gemini_service.parse_homeowner_intro = original
        assert parsed == ("77008", "Roberto")

    asyncio.run(run())


def test_visit_details_stay_off_the_first_message():
    assert parse_visit_reply("Yes") == {"confirm": True}
    assert parse_visit_reply("Skip") == {"skip_notes": True}
    assert parse_visit_reply("4102 Oak St\nGate code 4455") == {
        "street_address": "4102 Oak St",
        "saved_access_notes": "Gate code 4455",
    }
    parsed = parse_visit_reply("4102 Oak St, gate 4455")
    assert parsed["street_address"] == "4102 Oak St"
    assert "4455" in parsed["saved_access_notes"]
