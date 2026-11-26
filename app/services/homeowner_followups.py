"""20-hour homeowner check-ins for open, unbooked jobs.

Cloud Scheduler calls POST /api/cron/followups. The post-booking
"how did the job go?" message stays behind FEEDBACK_ENABLED.
"""

from __future__ import annotations

import hmac
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import settings
from app.services.alert_reply import job_ref
from app.services.session_window import clean_template_value, parse_timestamp

logger = logging.getLogger(__name__)

FOLLOWUP_WAIT = timedelta(hours=20)
OPEN_STATES = frozenset({"dispatched", "awaiting_pick"})


def cron_request_ok(authorization: str | None, header_secret: str | None) -> bool:
    """True when the bearer token or X-Cron-Secret matches CRON_SECRET."""
    secret = (settings.cron_secret or "").strip()
    if not secret:
        return False
    supplied = ""
    auth = (authorization or "").strip()
    if auth.lower().startswith("bearer "):
        supplied = auth[7:].strip()
    if not supplied:
        supplied = (header_secret or "").strip()
    if not supplied:
        return False
    return hmac.compare_digest(supplied, secret)


def _aware(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def quote_is_pending(response: dict[str, Any]) -> bool:
    return (
        response.get("status") == "details_given"
        and bool(response.get("availability_text"))
        and bool(response.get("estimate_text"))
    )


def next_pending_quote(
    responses: list[dict[str, Any]], passed_phone: str
) -> dict[str, Any] | None:
    """Oldest quote the homeowner has not passed, other than the one just passed."""
    from app.utils.phones import normalize_phone

    skipped = normalize_phone(passed_phone)
    pending = [
        response
        for response in responses
        if quote_is_pending(response)
        and normalize_phone(str(response.get("pro_phone") or "")) != skipped
    ]
    pending.sort(key=lambda response: str(response.get("responded_at") or response.get("updated_at") or ""))
    return pending[0] if pending else None


def no_reply_is_due(job: dict[str, Any], responses: list[dict[str, Any]], now: datetime) -> bool:
    """No price yet, and the request has been waiting 20 hours."""
    if job.get("state") != "dispatched":
        return False
    if job.get("no_reply_followup_sent"):
        return False
    if any(quote_is_pending(response) for response in responses):
        return False
    anchor = parse_timestamp(job.get("no_reply_anchor_at") or job.get("created_at"))
    if anchor is None:
        return False
    return _aware(now) - anchor >= FOLLOWUP_WAIT


def quote_reminder_is_due(
    job: dict[str, Any], responses: list[dict[str, Any]], now: datetime
) -> dict[str, Any] | None:
    """Oldest pending quote that has been sitting for 20 hours."""
    if job.get("state") != "awaiting_pick":
        return None
    if job.get("quote_reminder_sent"):
        return None
    pending = [response for response in responses if quote_is_pending(response)]
    if not pending:
        return None
    pending.sort(key=lambda response: str(response.get("responded_at") or response.get("updated_at") or ""))
    oldest = pending[0]
    arrived = parse_timestamp(oldest.get("responded_at") or oldest.get("updated_at"))
    if arrived is None or _aware(now) - arrived < FOLLOWUP_WAIT:
        return None
    return oldest


async def run_homeowner_followups(now: datetime | None = None) -> dict[str, int]:
    """Send each due check-in once. Flags flip only after a successful send."""
    from app.services.jobs import job_store

    current = _aware(now or datetime.now(timezone.utc))
    jobs = await job_store.list_in_states(OPEN_STATES)
    no_reply = 0
    reminders = 0
    for job in jobs:
        job_id = str(job.get("job_id") or "")
        if not job_id:
            continue
        responses = await job_store.list_responses(job_id)
        if no_reply_is_due(job, responses, current):
            if await _send_no_reply(job):
                await job_store.update_fields(job_id, no_reply_followup_sent=True)
                no_reply += 1
            continue
        due = quote_reminder_is_due(job, responses, current)
        if due and await _send_quote_reminder(job, due):
            await job_store.update_fields(job_id, quote_reminder_sent=True)
            reminders += 1
    from app.services.job_checkin import run_post_job_checkins

    checkins = await run_post_job_checkins(current)
    from app.services.job_escalation import run_unanswered_escalations

    escalations = await run_unanswered_escalations(current)
    return {
        "processedNoReply": no_reply,
        "processedQuoteReminders": reminders,
        "processedCheckins": checkins,
        **escalations,
    }


async def _send_no_reply(job: dict[str, Any]) -> bool:
    from app.services.dispatch import send_no_reply_followup

    try:
        await send_no_reply_followup(job)
        return True
    except Exception as e:
        logger.warning("no-reply follow-up failed for %s: %s", job.get("job_id"), e)
        return False


async def _send_quote_reminder(job: dict[str, Any], response: dict[str, Any]) -> bool:
    from app.services.dispatch import send_quote_reminder

    try:
        await send_quote_reminder(job, response)
        return True
    except Exception as e:
        logger.warning("quote reminder failed for %s: %s", job.get("job_id"), e)
        return False


def no_reply_body(trade: str, job_id: str) -> str:
    label = clean_template_value(trade, "repair", limit=40)
    return (
        f"Still looking on your {label} request ({job_ref(job_id)}). "
        "Nobody has replied yet. Want me to ask again?"
    )


def quote_reminder_body(name: str, price: str, trade: str, job_id: str) -> str:
    return (
        f"You still have a quote from {name} for {price} on your {trade} request "
        f"({job_ref(job_id)}). You can book them, pass, or ask a question."
    )


def passed_only_body(name: str) -> str:
    return (
        f"Got it—passed on {name}. That's the only reply so far. "
        "I can keep looking for more pros, or you can send a new photo to update your request."
    )
