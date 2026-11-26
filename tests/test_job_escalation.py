"""Unanswered jobs escalate only while nobody has viewed or quoted."""

from datetime import datetime, timedelta, timezone

from app.services.job_escalation import due_stage, reassure_body

NOW = datetime(2026, 11, 26, 16, 0, tzinfo=timezone.utc)


def _ago(minutes: int) -> str:
    return (NOW - timedelta(minutes=minutes)).isoformat()


def _job(**extra):
    base = {
        "state": "dispatched",
        "job_id": "abc12345",
        "created_at": _ago(20),
        "alerted_pro_phones": ["15550001111"],
        "trade": "hvac",
    }
    base.update(extra)
    return base


def test_a_view_stops_the_15_minute_message():
    job = _job()
    assert due_stage(job, [{"status": "viewed"}], NOW) == "stop"
    assert due_stage(job, [{"status": "details_given"}], NOW) == "stop"


def test_a_decline_still_reassures_at_15_minutes():
    assert due_stage(_job(), [{"status": "declined"}], NOW) == "reassure"
    assert due_stage(_job(), [{"status": "alerted"}], NOW) == "reassure"


def test_stages_catch_up_one_at_a_time():
    job = _job(created_at=_ago(70))
    assert due_stage(job, [], NOW) == "reassure"
    job["escalation_15_sent"] = True
    assert due_stage(job, [], NOW) == "founder"
    job["escalation_30_sent"] = True
    assert due_stage(job, [], NOW) == "choice"
    job["escalation_choice"] = "keep_waiting"
    assert due_stage(job, [], NOW) is None


def test_reassurance_names_the_trade():
    assert "hvac" in reassure_body("hvac").lower()
    assert "Hang tight" in reassure_body("hvac")


def test_urgent_jobs_skip_the_slow_ladder():
    job = _job(is_urgent=True, dispatched_at=_ago(12))
    assert due_stage(job, [], NOW) == "urgent_places"
    job["escalation_urgent_sent"] = True
    assert due_stage(job, [], NOW) is None
    assert due_stage(_job(is_urgent=True, dispatched_at=_ago(5)), [], NOW) is None


def test_flexible_jobs_keep_the_15_minute_update():
    assert due_stage(_job(is_urgent=False), [], NOW) == "reassure"


def test_urgent_words_change_the_prompt_only():
    from app.services.job_escalation import looks_urgent, urgency_prompt

    assert looks_urgent("There is a gas smell in the kitchen")
    assert looks_urgent("No A/C since last night")
    assert not looks_urgent("The sink drips a little")
    assert urgency_prompt("burst pipe") == "This sounds urgent. How urgent is this repair?"
    assert urgency_prompt("loose cabinet hinge") == "How urgent is this repair?"
