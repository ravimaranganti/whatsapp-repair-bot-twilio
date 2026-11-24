"""Unit tests for job state machine and pick payload resolution."""

import pytest

from app.services.jobs import (
    ALLOWED_TRANSITIONS,
    InvalidTransition,
    can_transition,
    resolve_pro_from_pick_payload,
    transition,
)
from app.utils.phones import phone_hash


def test_allowed_happy_path():
    state = "diagnosed"
    for nxt in [
        "awaiting_consent",
        "dispatched",
        "awaiting_pick",
        "assigned",
        "completed",
    ]:
        assert can_transition(state, nxt) or nxt not in ALLOWED_TRANSITIONS[state]
        if can_transition(state, nxt):
            state = transition(state, nxt)
    assert state == "completed"


def test_cancel_from_awaiting_consent():
    assert transition("awaiting_consent", "cancelled") == "cancelled"


def test_invalid_transition():
    with pytest.raises(InvalidTransition):
        transition("diagnosed", "assigned")


def test_expired_from_dispatched():
    assert transition("dispatched", "expired") == "expired"


def test_no_revive_from_completed():
    with pytest.raises(InvalidTransition):
        transition("completed", "dispatched")


def test_resolve_pick_payload():
    phones = ["17135550100", "17135550101"]
    h = phone_hash(phones[1])
    payload = f"pick:abc123:{h}"
    assert resolve_pro_from_pick_payload(payload, phones) == "17135550101"


def test_resolve_pick_bad_payload():
    assert resolve_pro_from_pick_payload("nope", ["17135550100"]) is None
