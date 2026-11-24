"""Window check and template-vs-freeform selection."""

from datetime import datetime, timedelta, timezone

from app.services.session_window import (
    choose_delivery,
    should_retry_with_template,
    window_is_open,
)

NOW = datetime(2026, 11, 24, 18, 0, tzinfo=timezone.utc)
SID = "HXtest"


def test_window_closed_when_no_inbound():
    assert window_is_open(None, now=NOW) is False
    assert window_is_open("", now=NOW) is False


def test_window_open_inside_24_hours():
    last = NOW - timedelta(hours=23, minutes=59)
    assert window_is_open(last, now=NOW) is True
    assert window_is_open(last.isoformat(), now=NOW) is True


def test_window_closed_at_24_hours():
    last = NOW - timedelta(hours=24)
    assert window_is_open(last, now=NOW) is False
    assert window_is_open(NOW - timedelta(hours=25), now=NOW) is False


def test_naive_timestamp_treated_as_utc():
    last = (NOW - timedelta(hours=1)).replace(tzinfo=None)
    assert window_is_open(last, now=NOW) is True


def test_freeform_when_window_open_even_if_template_exists():
    assert choose_delivery(window_open=True, template_sid=SID) == "freeform"


def test_template_when_window_closed_and_sid_set():
    assert choose_delivery(window_open=False, template_sid=SID) == "template"
    assert choose_delivery(window_open=False, template_sid="  " + SID + " ") == "template"


def test_freeform_when_window_closed_and_no_template():
    assert choose_delivery(window_open=False, template_sid="") == "freeform"
    assert choose_delivery(window_open=False, template_sid=None) == "freeform"


def test_63016_retries_only_when_a_template_exists():
    assert should_retry_with_template(63016, SID) is True
    assert should_retry_with_template(63016, "") is False
    assert should_retry_with_template(63016, None) is False
    assert should_retry_with_template(21610, SID) is False
    assert should_retry_with_template(None, SID) is False
