"""A pro reply has to name which open job it belongs to."""

from app.services.alert_reply import resolve_alert_reply


def test_button_payload_selects_that_job():
    accept = resolve_alert_reply("accept_job:aaa111", "Interested", ["aaa111", "bbb222"])
    decline = resolve_alert_reply("decline_job:bbb222", "Not available", ["aaa111", "bbb222"])
    assert accept.job_id == "aaa111" and accept.status == "interested"
    assert decline.job_id == "bbb222" and decline.status == "declined"


def test_legacy_payload_prefixes_still_work():
    reply = resolve_alert_reply("int:aaa111", "", ["bbb222"])
    assert reply.job_id == "aaa111"
    assert resolve_alert_reply("dec:bbb222", "", []).status == "declined"


def test_typed_reply_without_a_ref_does_not_guess():
    reply = resolve_alert_reply("", "Interested", ["aaa111", "bbb222"])
    assert reply.handled and reply.job_id is None
    assert "2 open job alerts" in (reply.clarify or "")
    assert "#aaa111" in (reply.clarify or "")


def test_typed_reply_with_one_alert_uses_it():
    reply = resolve_alert_reply("", "Not available", ["aaa111"])
    assert reply.status == "declined" and reply.job_id == "aaa111"


def test_typed_reference_picks_the_named_job():
    reply = resolve_alert_reply("", "Interested #bbb222", ["aaa111", "bbb222"])
    assert reply.job_id == "bbb222" and reply.status == "interested"


def test_view_and_pass_commands_name_the_ticket():
    view = resolve_alert_reply("", "VIEW b5905bc59ec44a42", [])
    passed = resolve_alert_reply("", "pass #b5905bc59ec44a42", [])
    assert view.status == "view" and view.job_id == "b5905bc59ec44a42"
    assert passed.status == "declined" and passed.job_id == "b5905bc59ec44a42"


def test_unrelated_text_is_not_an_alert_reply():
    reply = resolve_alert_reply("", "Yes I can come tomorrow around 2", ["aaa111"])
    assert not reply.handled
