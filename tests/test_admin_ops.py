"""Ops dashboard auth and the job/pro summaries it shows."""

from datetime import datetime, timedelta, timezone

from app.services.admin_auth import session_ok, sign_session
from app.services.admin_ops import (
    job_bucket,
    pipeline_label,
    summarize,
    ticket_amount,
)


def test_admin_cookie_rejects_a_bad_password():
    expires = datetime.now(timezone.utc) + timedelta(hours=1)
    cookie = sign_session(expires, "correct")
    assert session_ok(cookie, "correct") is True
    assert session_ok(cookie, "wrong") is False
    assert session_ok("admin|1|nope", "correct") is False


def test_job_buckets_and_quote_labels():
    quoted = {"status": "details_given", "estimate_text": "$150", "availability_text": "3pm"}
    assert job_bucket("dispatched", []) == "open"
    assert job_bucket("awaiting_pick", [quoted]) == "quoted"
    assert job_bucket("assigned", [quoted]) == "booked"
    assert job_bucket("completed", []) == "closed"
    assert pipeline_label(quoted) == "Quoted: $150 @ 3pm"
    assert pipeline_label({"status": "alerted"}) == "Pinged - No Reply"
    assert pipeline_label({"status": "homeowner_passed"}) == "Passed"
    assert pipeline_label({"status": "selected"}) == "Booked"


def test_metrics_count_quotes_and_the_average_ticket():
    jobs = [
        {"bucket": "open", "has_quote": False, "state": "dispatched", "final_price_paid": ""},
        {"bucket": "quoted", "has_quote": True, "state": "awaiting_pick", "final_price_paid": ""},
        {"bucket": "closed", "has_quote": True, "state": "completed", "final_price_paid": "$180"},
        {"bucket": "closed", "has_quote": True, "state": "completed", "final_price_paid": "$220"},
    ]
    pros = [
        {"opted_in": True, "status": "active", "trades": ["plumbing"]},
        {"opted_in": True, "status": "paused", "trades": ["hvac"]},
        {"opted_in": False, "status": "active", "trades": ["appliance"]},
    ]
    stats = summarize(jobs, pros)
    assert stats["total_jobs"] == 4
    assert stats["open_jobs"] == 1
    assert stats["active_pros"] == 1
    assert stats["quote_rate"] == 75
    assert stats["completed_jobs"] == 2
    assert stats["avg_ticket"] == 200
    assert ticket_amount("about $90") == 90
