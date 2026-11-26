"""Rate limit, body cap, and open-job cap."""

from app.services.inbound_guard import (
    BURST_LIMIT,
    DAILY_LIMIT,
    MAX_BODY_CHARS,
    InboundRateLimiter,
    cap_text,
)
from app.services.jobs import (
    OPEN_JOB_LIMIT,
    OPEN_JOB_LIMIT_REPLY,
    OPEN_JOB_STATES,
    at_open_job_limit,
)


def test_body_cap_keeps_the_first_thousand_characters():
    assert cap_text("short") == "short"
    assert cap_text("") == ""
    long = "a" * (MAX_BODY_CHARS + 250)
    capped = cap_text(long)
    assert len(capped) == MAX_BODY_CHARS
    assert capped == long[:MAX_BODY_CHARS]


def test_burst_replies_once_then_stays_quiet():
    limiter = InboundRateLimiter()
    phone = "17135550000"
    start = 1_700_000_000.0
    for offset in range(BURST_LIMIT):
        assert limiter.admit(phone, now=start + offset) == "ok"
    assert limiter.admit(phone, now=start + BURST_LIMIT) == "reply"
    # More messages inside the same 10 minutes must not each get a Twilio reply.
    for extra in range(20):
        assert limiter.admit(phone, now=start + BURST_LIMIT + 1 + extra) == "silent"
    # Cooldown has passed, and the later messages are still inside the burst window.
    assert limiter.admit(phone, now=start + BURST_LIMIT + 10 * 60) == "reply"


def test_daily_limit_is_independent_of_a_slow_burst():
    limiter = InboundRateLimiter()
    phone = "17135550001"
    start = 1_700_000_000.0
    # One message every 11 minutes stays under the 15/10-minute burst.
    for index in range(DAILY_LIMIT):
        assert limiter.admit(phone, now=start + index * 11 * 60) == "ok"
    assert limiter.admit(phone, now=start + DAILY_LIMIT * 11 * 60) == "reply"


def test_generation_config_caps_output_tokens():
    from app.services.gemini import MAX_OUTPUT_TOKENS, generation_config

    config = generation_config(system_instruction="hi")
    assert config.max_output_tokens == MAX_OUTPUT_TOKENS
    assert MAX_OUTPUT_TOKENS == 800
    assert "assigned" not in OPEN_JOB_STATES
    assert "cancelled" not in OPEN_JOB_STATES
    assert "UNMATCHED_FALLBACK" not in OPEN_JOB_STATES
    assert at_open_job_limit(OPEN_JOB_LIMIT - 1) is False
    assert at_open_job_limit(OPEN_JOB_LIMIT) is True
    assert "3 open requests" in OPEN_JOB_LIMIT_REPLY
    assert "book, pass, or close" in OPEN_JOB_LIMIT_REPLY
