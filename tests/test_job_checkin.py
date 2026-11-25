"""The 48-hour check-in only fires for a booked job that is still waiting on an outcome."""

from datetime import datetime, timedelta, timezone

from app.services.job_checkin import (
    checkin_is_due,
    coerce_feedback,
    fallback_feedback,
)
from app.services.jobs import transition


NOW = datetime(2026, 11, 25, 18, 0, tzinfo=timezone.utc)


def _job(**extra):
    base = {
        "state": "assigned",
        "outcome_status": "PENDING_CHECKIN",
        "checkin_count": 0,
        "checkin_due_at": (NOW - timedelta(hours=1)).isoformat(),
    }
    base.update(extra)
    return base


def test_checkin_is_due_only_while_waiting():
    assert checkin_is_due(_job(), NOW) is True
    assert checkin_is_due(_job(checkin_due_at=(NOW + timedelta(hours=2)).isoformat()), NOW) is False
    assert checkin_is_due(_job(checkin_count=2), NOW) is False
    assert checkin_is_due(_job(outcome_status="COMPLETED"), NOW) is False
    assert checkin_is_due(_job(state="awaiting_pick"), NOW) is False


def test_feedback_keeps_a_price_and_a_rating():
    parsed = coerce_feedback(
        {
            "final_price_paid": "$180",
            "rating_1_to_5": 5,
            "review_summary": "Showed up on time and fixed the leak.",
        }
    )
    assert parsed["final_price_paid"] == "$180"
    assert parsed["rating_1_to_5"] == 5
    assert "leak" in parsed["review_summary"]
    blank = coerce_feedback({"final_price_paid": "", "rating_1_to_5": 0, "review_summary": ""})
    assert blank["final_price_paid"] is None
    assert blank["rating_1_to_5"] is None


def test_fallback_reads_dollars_and_stars():
    parsed = coerce_feedback(fallback_feedback("Paid $150 and I'd give them 4 stars"))
    assert parsed["final_price_paid"] == "$150"
    assert parsed["rating_1_to_5"] == 4


def test_a_booked_job_can_be_reopened():
    assert transition("assigned", "awaiting_pick") == "awaiting_pick"
    assert transition("assigned", "dispatched") == "dispatched"
    assert transition("assigned", "completed") == "completed"
