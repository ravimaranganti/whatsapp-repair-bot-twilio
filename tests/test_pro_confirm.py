"""Natural name replies and the profile confirmation step."""

import asyncio

from app.models import MODE_PRO_ONBOARDING
from app.services import onboarding as ob
from app.services.pro_profile import read_pro_name, split_pro_name


def test_plain_name_replies_do_not_need_a_slash():
    assert split_pro_name("Roberto from ABC Lawn") == ("Roberto", "ABC Lawn")
    assert split_pro_name("Roberto / ABC Lawn") == ("Roberto", "ABC Lawn")
    assert split_pro_name("I'm Roberto") == ("Roberto", "")
    assert split_pro_name("Roberto") == ("Roberto", "")
    assert split_pro_name("Roberto, Jr.") == ("Roberto, Jr.", "")


def test_sentence_name_uses_structured_parse():
    async def run():
        async def fake_parse(message: str) -> dict:
            assert "ABC Lawn" in message
            return {"contact_name": "Roberto", "business_name": "ABC Lawn"}

        from app.services import gemini as gemini_mod

        original = gemini_mod.gemini_service.parse_pro_name
        gemini_mod.gemini_service.parse_pro_name = fake_parse
        try:
            parsed = await read_pro_name(
                "I run ABC Lawn and my name is Roberto"
            )
        finally:
            gemini_mod.gemini_service.parse_pro_name = original
        assert parsed == ("Roberto", "ABC Lawn")

    asyncio.run(run())


def test_solo_name_is_stored_as_the_business_too():
    async def run():
        assert await read_pro_name("Roberto") == ("Roberto", "Roberto")

    asyncio.run(run())


def test_confirmation_card_matches_the_summary_format():
    text = ob.profile_confirmation_text(
        {
            "name": "Roberto",
            "business_name": "ABC Lawn",
            "trades": ["handyman"],
            "service_areas": ["katy"],
            "accepts_emergency": True,
            "standard_lead_time": "same_day",
            "free_photo_estimate": True,
            "dispatch_fee_tier": "free_estimate",
            "languages": ["en", "es"],
            "insured": False,
            "profile_notes": "15 years on lawns",
        }
    )
    assert text.startswith(
        "Here's your profile summary — does everything look good?\n\n"
        "Roberto — ABC Lawn\n"
    )
    assert "Trades: Handyman" in text
    assert "Areas: Katy" in text
    assert "Urgent calls: yes. Typical lead time: Same day." in text
    assert "Free photo estimate: yes" in text
    assert "Service call: Free estimate" in text
    assert "Languages: English and Spanish" in text
    assert "Insured: no" in text
    assert "About: 15 years on lawns" in text
    assert all(len(title) <= 20 for _, title in ob._CONFIRM_BUTTONS)


def test_about_note_waits_for_looks_good_before_going_live():
    async def run():
        sent: list[tuple] = []
        saved: dict = {}
        state = {
            "mode": MODE_PRO_ONBOARDING,
            "context": {
                "step": 10,
                "draft": {
                    "name": "Roberto",
                    "business_name": "ABC Lawn",
                    "trades": ["handyman"],
                    "service_areas": ["katy"],
                    "accepts_emergency": False,
                    "standard_lead_time": "next_48_hours",
                    "free_photo_estimate": False,
                    "dispatch_fee_tier": "tier_50_99",
                    "languages": ["en"],
                    "insured": None,
                },
            },
        }

        async def set_state(phone, mode, ctx):
            state["mode"] = mode
            state["context"] = ctx

        async def clear(phone):
            state["mode"] = "idle"
            state["context"] = {}

        async def upsert(phone, **fields):
            saved.update(fields)
            return saved

        async def send_text(phone, message):
            sent.append(("text", message))
            return {}

        async def send_buttons(phone, body, buttons):
            sent.append(("buttons", body, list(buttons)))
            return {}

        async def log_event(**kwargs):
            return None

        originals = (
            ob.conversation_state_store.set,
            ob.conversation_state_store.clear,
            ob.pro_store.upsert,
            ob.whatsapp_service.send_text_message,
            ob.whatsapp_service.send_session_buttons,
            ob.log_event,
        )
        ob.conversation_state_store.set = set_state
        ob.conversation_state_store.clear = clear
        ob.pro_store.upsert = upsert
        ob.whatsapp_service.send_text_message = send_text
        ob.whatsapp_service.send_session_buttons = send_buttons
        ob.log_event = log_event
        try:
            handled = await ob.continue_onboarding(
                "17135550000", "15 years on lawns", state, ""
            )
            assert handled is True
            assert state["context"]["step"] == 11
            assert state["context"]["draft"]["profile_notes"] == "15 years on lawns"
            assert saved == {}
            kind, body, buttons = sent[-1]
            assert kind == "buttons"
            assert body.startswith("Here's your profile summary")
            assert buttons == [
                ("confirm_profile", "Looks Good!"),
                ("edit_about", "Edit About"),
                ("restart_onboarding", "Start Over"),
            ]

            sent.clear()
            await ob.continue_onboarding(
                "17135550000", "Edit About", state, "edit_about"
            )
            assert state["context"]["step"] == 10
            assert state["context"]["editing_about"] is True
            assert "updated note" in sent[-1][1]

            sent.clear()
            await ob.continue_onboarding(
                "17135550000", "Licensed and local since 2011", state, ""
            )
            assert state["context"]["step"] == 11
            assert "2011" in sent[-1][1]
            assert saved == {}

            sent.clear()
            await ob.continue_onboarding(
                "17135550000", "Looks Good!", state, "confirm_profile"
            )
            assert saved["opted_in"] is True
            assert saved["status"] == "active"
            assert saved["name"] == "Roberto"
            assert saved["business_name"] == "ABC Lawn"
            assert saved["profile_notes"] == "Licensed and local since 2011"
            assert state["mode"] == "idle"
            assert sent[-1] == (
                "text",
                ob._ACTIVATED,
            )
            assert "Here's your summary" not in sent[-1][1]
        finally:
            (
                ob.conversation_state_store.set,
                ob.conversation_state_store.clear,
                ob.pro_store.upsert,
                ob.whatsapp_service.send_text_message,
                ob.whatsapp_service.send_session_buttons,
                ob.log_event,
            ) = originals

    asyncio.run(run())


def test_start_over_returns_to_the_name_question():
    async def run():
        sent: list[str] = []
        state = {
            "mode": MODE_PRO_ONBOARDING,
            "context": {"step": 11, "draft": {"name": "Roberto"}},
        }

        async def set_state(phone, mode, ctx):
            state["mode"] = mode
            state["context"] = ctx

        async def upsert(phone, **fields):
            return {}

        async def send_text(phone, message):
            sent.append(message)
            return {}

        async def log_event(**kwargs):
            return None

        originals = (
            ob.conversation_state_store.set,
            ob.pro_store.upsert,
            ob.whatsapp_service.send_text_message,
            ob.log_event,
        )
        ob.conversation_state_store.set = set_state
        ob.pro_store.upsert = upsert
        ob.whatsapp_service.send_text_message = send_text
        ob.log_event = log_event
        try:
            await ob.continue_onboarding(
                "17135550000", "Start Over", state, "restart_onboarding"
            )
            assert state["context"]["step"] == 1
            assert state["context"]["draft"] == {}
            assert "Roberto from ABC Lawn" in sent[-1]
            assert "/" not in sent[-1].split("e.g.,", 1)[-1]
        finally:
            (
                ob.conversation_state_store.set,
                ob.pro_store.upsert,
                ob.whatsapp_service.send_text_message,
                ob.log_event,
            ) = originals

    asyncio.run(run())
