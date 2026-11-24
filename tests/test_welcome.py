"""Role welcome routing: greetings, photos, role answers, join code, returning users."""

import asyncio

from app.models import MessageContent
from app.services.welcome import (
    MODE_AWAITING_JOIN_CODE,
    MODE_AWAITING_ROLE,
    decide,
    handle_welcome,
    resolve_actor,
)

PICK = {"job_id": "abc", "option_phones": ["15551111", "15552222"]}


def _d(**kwargs):
    base = dict(
        role="unknown",
        text="",
        payload="",
        message_type="text",
        mode="idle",
        context={},
        has_active_job=False,
        pending_count=0,
        opted_in=False,
        join_code="",
    )
    base.update(kwargs)
    return decide(**base)


def test_unknown_greeting_shows_welcome():
    for text in ("hi", "hello", "hey", "start", "what is this", "👋"):
        decision = _d(text=text)
        assert decision.action == "show_welcome", text
        assert decision.next_mode == MODE_AWAITING_ROLE


def test_unknown_help_shows_welcome_not_menu():
    decision = _d(text="help")
    assert decision.action == "show_welcome"


def test_unknown_photo_becomes_homeowner_diagnosis():
    decision = _d(message_type="image", text="")
    assert decision.action == "create_homeowner_and_continue"


def test_unknown_issue_text_becomes_homeowner_diagnosis():
    decision = _d(text="leak under the kitchen sink")
    assert decision.action == "create_homeowner_and_continue"


def test_role_button_and_typed_answers():
    homeowner_cases = [
        ("I'm a homeowner", "role:homeowner"),
        ("1", ""),
        ("homeowner", ""),
        ("owner", ""),
    ]
    for text, payload in homeowner_cases:
        decision = _d(text=text, payload=payload, mode=MODE_AWAITING_ROLE, join_code="secret")
        assert decision.action == "confirm_homeowner", text
        assert decision.clear_state is True

    decision = _d(text="I'm a pro", payload="role:pro", mode=MODE_AWAITING_ROLE, join_code="secret")
    assert decision.action == "ask_join_code"
    assert decision.next_mode == MODE_AWAITING_JOIN_CODE

    for text in ("2", "pro", "professional", "contractor"):
        decision = _d(text=text, mode=MODE_AWAITING_ROLE, join_code="")
        assert decision.action == "start_pro_onboarding", text


def test_role_reprompt_then_defaults_to_homeowner():
    first = _d(text="purple", mode=MODE_AWAITING_ROLE, context={"role_reprompts": 0})
    assert first.action == "reprompt_role"
    assert first.next_context["role_reprompts"] == 1
    second = _d(text="still no", mode=MODE_AWAITING_ROLE, context=first.next_context)
    assert second.action == "default_homeowner"
    assert second.clear_state is True


def test_photo_during_role_question_diagnoses():
    decision = _d(message_type="image", mode=MODE_AWAITING_ROLE)
    assert decision.action == "create_homeowner_and_continue"


def test_join_code_success_and_failure():
    ok = _d(text="secret", mode=MODE_AWAITING_JOIN_CODE, join_code="secret")
    assert ok.action == "join_ok"

    shortcut = _d(text="JOIN secret", join_code="secret")
    assert shortcut.action == "start_pro_onboarding"

    first = _d(text="nope", mode=MODE_AWAITING_JOIN_CODE, join_code="secret", context={"join_attempts": 0})
    assert first.action == "join_fail"
    assert first.next_context["join_attempts"] == 1
    second = _d(text="nope", mode=MODE_AWAITING_JOIN_CODE, join_code="secret", context={"join_attempts": 1})
    assert second.action == "join_fail"
    assert second.next_context["join_attempts"] == 2
    third = _d(text="nope", mode=MODE_AWAITING_JOIN_CODE, join_code="secret", context={"join_attempts": 2})
    assert third.action == "join_locked"
    assert third.clear_state is True


def test_returning_homeowner_with_and_without_job():
    idle = _d(role="homeowner", text="hi", has_active_job=False)
    assert idle.action == "status_homeowner_idle"
    active = _d(role="homeowner", text="hello", has_active_job=True)
    assert active.action == "status_homeowner_job"
    assert active.clear_state is False


def test_returning_pro_with_and_without_alerts():
    none = _d(role="pro", text="hi", opted_in=True, pending_count=0)
    assert none.action == "status_pro_idle"
    pending = _d(role="pro", text="hey", opted_in=True, pending_count=2)
    assert pending.action == "status_pro_pending"
    stopped = _d(role="pro", text="hi", opted_in=False, pending_count=1)
    assert stopped.action == "status_pro_unsubscribed"


def test_help_during_awaiting_pick_keeps_state():
    decision = _d(
        role="homeowner",
        text="HELP",
        mode="awaiting_pick",
        context=PICK,
        has_active_job=True,
    )
    assert decision.action == "midflow_help"
    assert decision.clear_state is False
    assert decision.next_mode is None
    assert decision.reminder == "You still have 2 pro options to choose from."


def test_dual_role_photo_is_homeowner_and_text_is_pro():
    pro = {"opted_in": True}
    homeowner = {"active_job_id": None}
    assert (
        resolve_actor(pro, homeowner, message_type="image", mode="idle", context={})
        == "homeowner"
    )
    assert (
        resolve_actor(pro, homeowner, message_type="text", mode="idle", context={})
        == "pro"
    )
    assert (
        resolve_actor(
            pro,
            homeowner,
            message_type="text",
            mode="awaiting_pick",
            context=PICK,
        )
        == "homeowner"
    )


def test_help_handler_does_not_clear_pick_state(monkeypatch):
    cleared: list[str] = []

    async def get_pro(phone):
        return None

    async def get_ho(phone):
        return {"active_job_id": "job1", "phone": phone}

    async def get_job(job_id):
        return {
            "job_id": job_id,
            "state": "awaiting_pick",
            "trade": "plumbing",
            "area_zip": "77008",
        }

    async def list_responses(job_id):
        return [{"status": "details_given"}, {"status": "interested"}]

    async def clear(phone):
        cleared.append(phone)

    async def set_state(*args, **kwargs):
        raise AssertionError("awaiting_pick state must stay")

    async def send(*args, **kwargs):
        return {}

    async def options(*args, **kwargs):
        return None

    async def no_log(*args, **kwargs):
        return None

    import app.services.welcome as welcome
    import app.services.dispatch as dispatch

    monkeypatch.setattr(welcome.pro_store, "get", get_pro)
    monkeypatch.setattr(welcome.homeowner_store, "get", get_ho)
    monkeypatch.setattr(welcome.job_store, "get_job", get_job)
    monkeypatch.setattr(welcome.job_store, "list_responses", list_responses)
    monkeypatch.setattr(welcome.conversation_state_store, "clear", clear)
    monkeypatch.setattr(welcome.conversation_state_store, "set", set_state)
    monkeypatch.setattr(welcome.whatsapp_service, "send_text_message", send)
    monkeypatch.setattr(welcome.whatsapp_service, "send_content_template", send)
    monkeypatch.setattr(welcome, "log_event", no_log)
    monkeypatch.setattr(dispatch, "maybe_send_homeowner_options", options)

    message = MessageContent(id="m1", from_="+1555", type="text", body="help")
    state = {"mode": "awaiting_pick", "context": PICK}

    async def run():
        assert await handle_welcome(message, "15551212", state) is True

    asyncio.run(run())
    assert cleared == []
