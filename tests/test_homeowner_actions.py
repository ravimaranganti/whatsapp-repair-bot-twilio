"""Homeowner buttons name the job and the quote."""

from app.services.homeowner_actions import (
    parse_homeowner_payload,
    quote_choice_message,
    typed_choice,
)


def test_quote_buttons_carry_job_and_quote():
    book = parse_homeowner_payload("book_quote:job123:hash456")
    passed = parse_homeowner_payload("decline_quote:job123:hash456")
    ask = parse_homeowner_payload("ask_quote:job123:hash456")
    assert book.action == "book_quote" and book.job_id == "job123" and book.quote_id == "hash456"
    assert passed.action == "pass_quote"
    again = parse_homeowner_payload("pass_quote:job123:hash456")
    assert again.action == "pass_quote" and again.quote_id == "hash456"
    assert parse_homeowner_payload("keep_looking:job123").action == "keep_looking"
    assert parse_homeowner_payload("close_job:job123").action == "close_job"
    assert parse_homeowner_payload("retry_pros:job123").action == "retry_pros"
    assert ask.action == "ask_quote"


def test_job_buttons_and_legacy_pick():
    assert parse_homeowner_payload("confirm_job:job123").action == "confirm_job"
    assert parse_homeowner_payload("cancel_job:job123").job_id == "job123"
    assert parse_homeowner_payload("show_options:job123").action == "show_options"
    assert parse_homeowner_payload("pick:job123:hash456").action == "book_quote"
    share = parse_homeowner_payload("share:yes:job123:hash456")
    assert share.action == "share_yes" and share.quote_id == "hash456"
    assert parse_homeowner_payload("new_issue:no:job123").action == "new_issue_no"
    assert parse_homeowner_payload("rating_great:job123").action == "rating_great"


def test_generic_book_does_not_look_like_a_sentence():
    assert typed_choice("Sounds good") == "book"
    assert typed_choice("Yes") == "yes"
    assert typed_choice("Pass") == "pass"
    assert typed_choice("Yes I can come tomorrow") is None
    message = quote_choice_message(
        [{"name": "Mike", "job_id": "aaa"}, {"name": "Ana", "job_id": "bbb"}]
    )
    assert "2 open quotes" in message
    assert "#aaa" in message and "#bbb" in message
