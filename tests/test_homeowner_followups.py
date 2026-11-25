"""When a homeowner check-in is due, and which quote comes next after Pass."""

from datetime import datetime, timedelta, timezone

from app.services.homeowner_followups import (
    cron_request_ok,
    next_pending_quote,
    no_reply_is_due,
    quote_reminder_is_due,
)


NOW = datetime(2026, 11, 25, 18, 0, tzinfo=timezone.utc)


def _hours_ago(hours: int) -> str:
    return (NOW - timedelta(hours=hours)).isoformat()


def test_no_reply_waits_twenty_hours_and_fires_once():
    job = {"state": "dispatched", "created_at": _hours_ago(20)}
    assert no_reply_is_due(job, [], NOW) is True
    fresh = {"state": "dispatched", "created_at": _hours_ago(19)}
    assert no_reply_is_due(fresh, [], NOW) is False
    sent = {**job, "no_reply_followup_sent": True}
    assert no_reply_is_due(sent, [], NOW) is False
    quoted = [{"status": "details_given", "availability_text": "tomorrow", "estimate_text": "$80"}]
    assert no_reply_is_due(job, quoted, NOW) is False


def test_retry_anchor_restarts_the_wait():
    job = {
        "state": "dispatched",
        "created_at": _hours_ago(30),
        "no_reply_anchor_at": _hours_ago(2),
        "no_reply_followup_sent": False,
    }
    assert no_reply_is_due(job, [], NOW) is False


def test_quote_reminder_uses_the_oldest_pending_quote():
    job = {"state": "awaiting_pick"}
    responses = [
        {
            "pro_phone": "111",
            "status": "details_given",
            "availability_text": "today",
            "responded_at": _hours_ago(21),
            "estimate_text": "$80",
        },
        {
            "pro_phone": "222",
            "status": "details_given",
            "availability_text": "tomorrow",
            "responded_at": _hours_ago(5),
        },
    ]
    due = quote_reminder_is_due(job, responses, NOW)
    assert due["pro_phone"] == "111"
    reminded = {**job, "quote_reminder_sent": True}
    assert quote_reminder_is_due(reminded, responses, NOW) is None


def test_pass_shows_the_other_pending_quote():
    responses = [
        {"pro_phone": "111", "status": "homeowner_passed", "availability_text": "today"},
        {
            "pro_phone": "222",
            "status": "details_given",
            "availability_text": "tomorrow",
            "responded_at": _hours_ago(1),
            "estimate_text": "$90",
        },
    ]
    nxt = next_pending_quote(responses, "111")
    assert nxt["pro_phone"] == "222"
    assert next_pending_quote(responses, "222") is None


def test_cron_secret_accepts_bearer_or_header(monkeypatch):
    import app.services.homeowner_followups as followups

    monkeypatch.setattr(followups.settings, "cron_secret", "s3cret")
    assert cron_request_ok("Bearer s3cret", None) is True
    assert cron_request_ok(None, "s3cret") is True
    assert cron_request_ok("Bearer nope", None) is False
    assert cron_request_ok(None, None) is False
