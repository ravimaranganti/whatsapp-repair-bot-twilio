"""Free-form pro replies become a quote, a question, or a request for the missing piece."""

from app.services.pro_reply import (
    MISSING_INFO,
    QUESTION_ONLY,
    QUOTE_READY,
    QUOTE_WITH_QUESTION,
    experience_sentence,
    fallback_pro_reply,
    finalize_pro_reply,
    quote_card_text,
)


def test_quote_with_a_question_keeps_both():
    reply = finalize_pro_reply(
        {
            "intent": "QUOTE_WITH_QUESTION",
            "availability_text": "Today at 3:00 PM",
            "price_estimate": "$150",
            "caveats_or_notes": "May be slightly higher depending on the valve",
            "question_for_homeowner": "Is this gas or electric?",
        }
    )
    assert reply.intent == QUOTE_WITH_QUESTION
    assert reply.price_estimate == "$150"
    assert "valve" in reply.caveats_or_notes
    assert reply.question_for_homeowner.startswith("Is this")


def test_missing_price_stays_open_and_merges_the_next_reply():
    first = finalize_pro_reply(
        {"intent": "MISSING_INFO", "availability_text": "Tomorrow morning"}
    )
    assert first.intent == MISSING_INFO
    assert "estimate" in first.missing_field_prompt.lower()
    second = finalize_pro_reply(
        {"intent": "QUOTE_READY", "price_estimate": "$120-$150"},
        {
            "availability_text": first.availability_text,
            "price_estimate": "",
            "caveats_or_notes": "",
            "question_for_homeowner": "",
        },
    )
    assert second.intent == QUOTE_READY
    assert second.availability_text == "Tomorrow morning"
    assert second.price_estimate == "$120-$150"


def test_question_only_does_not_invent_a_quote():
    reply = finalize_pro_reply(
        {
            "intent": "MISSING_INFO",
            "question_for_homeowner": "Is the water heater gas or electric?",
            "availability_text": "",
            "price_estimate": "",
            "missing_field_prompt": "What's a rough price?",
        },
        message="Is the water heater gas or electric?",
    )
    assert reply.intent == QUESTION_ONLY
    assert reply.price_estimate == ""
    assert reply.missing_field_prompt == ""


def test_inspect_first_uses_the_saved_visit_fee_as_the_price():
    message = "I can come today at 3pm, but I need to inspect it before I quote the repair."
    free = finalize_pro_reply(
        {"intent": "MISSING_INFO", "availability_text": "Today at 3:00 PM"},
        pro={"dispatch_fee_tier": "free_estimate"},
        message=message,
    )
    assert free.intent == QUOTE_READY
    assert free.price_estimate == "Free in-person estimate"
    paid = finalize_pro_reply(
        {"intent": "MISSING_INFO", "availability_text": "Today at 3:00 PM"},
        pro={"dispatch_fee_tier": "tier_50_99"},
        message=message,
    )
    assert paid.price_estimate == "$50–$99 diagnostic fee, repair quoted on-site"


def test_a_stated_repair_price_wins_over_the_visit_fee():
    reply = finalize_pro_reply(
        {
            "intent": "QUOTE_READY",
            "availability_text": "Today at 3:00 PM",
            "price_estimate": "$150",
        },
        pro={"dispatch_fee_tier": "free_estimate"},
        message="Available today at 3pm. I should look first, but ballpark $150.",
    )
    assert reply.intent == QUOTE_READY
    assert reply.price_estimate == "$150"


def test_inspect_and_a_question_still_sends_the_visit_price():
    reply = finalize_pro_reply(
        {
            "intent": "QUESTION_ONLY",
            "availability_text": "Tomorrow morning",
            "question_for_homeowner": "Is it gas or electric?",
        },
        pro={"dispatch_fee_tier": "tier_100_plus"},
        message="Tomorrow morning. I need to see it first. Is it gas or electric?",
    )
    assert reply.intent == QUOTE_WITH_QUESTION
    assert reply.price_estimate == "$100+ diagnostic fee, repair quoted on-site"
    assert reply.question_for_homeowner == "Is it gas or electric?"


def test_fallback_asks_again_when_the_message_is_unclear():
    raw = fallback_pro_reply("sounds good I can help")
    reply = finalize_pro_reply(raw)
    assert reply.intent == MISSING_INFO


def test_fallback_accepts_an_obvious_time_and_price():
    raw = fallback_pro_reply("Available today at 3pm, rough estimate $150")
    reply = finalize_pro_reply(raw)
    assert reply.intent == QUOTE_READY
    assert "$150" in reply.price_estimate


def test_card_uses_years_from_the_profile_note():
    pro = {"profile_notes": "20 years of experience with pools", "dispatch_fee_tier": "free_estimate"}
    assert "20 years" in experience_sentence(pro)
    text = quote_card_text(
        name="Mike's plumbing",
        trade="plumbing",
        job_id="abc",
        availability="Today at 3:00 PM",
        price="$150",
        pro=pro,
        notes="May be slightly higher",
        question="Is it gas or electric?",
    )
    assert "20 years of experience" in text
    assert "$150" in text
    assert "Free estimate" in text
    assert "Is it gas or electric?" in text


def test_card_says_verified_local_pro_without_years():
    text = quote_card_text(
        name="Ada",
        trade="handyman",
        job_id="abc",
        availability="Tomorrow",
        price="$80",
        pro={"name": "Ada"},
    )
    assert "verified local pro" in text
