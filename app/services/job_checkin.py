"""48-hour check-in after a homeowner and a pro exchange phone numbers.

Quote delivery and the 20-hour unbooked follow-ups stay on their own path.
This only runs for jobs that are already assigned.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import settings
from app.services.alert_reply import job_ref
from app.services.session_window import clean_template_value, parse_timestamp

logger = logging.getLogger(__name__)

CHECKIN_WAIT = timedelta(hours=48)
CHECKIN_LIMIT = 2
PENDING = "PENDING_CHECKIN"
COMPLETED = "COMPLETED"
SCHEDULED = "SCHEDULED"
FELL_THROUGH = "FELL_THROUGH"
CLOSED = "CLOSED"
MODE_AWAITING_JOB_FEEDBACK = "awaiting_job_feedback"

_PRICE = re.compile(
    r"\$\s?\d+(?:\.\d+)?(?:\s*[-–]\s*\$?\s?\d+(?:\.\d+)?)?",
    re.I,
)
_RATING_WORD = {
    "terrible": 1,
    "awful": 1,
    "bad": 2,
    "poor": 2,
    "okay": 3,
    "ok": 3,
    "fine": 3,
    "good": 4,
    "great": 5,
    "excellent": 5,
}


def _aware(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def checkin_is_due(job: dict[str, Any], now: datetime) -> bool:
    """Assigned, still waiting on an outcome, and under the two-message cap."""
    if job.get("state") != "assigned":
        return False
    if job.get("outcome_status") != PENDING:
        return False
    if int(job.get("checkin_count") or 0) >= CHECKIN_LIMIT:
        return False
    due = parse_timestamp(job.get("checkin_due_at"))
    if due is None:
        return False
    return _aware(now) >= _aware(due)


def homeowner_first_name(homeowner: dict[str, Any] | None) -> str:
    raw = str(
        (homeowner or {}).get("first_name") or (homeowner or {}).get("name") or ""
    ).strip()
    first = raw.split()[0] if raw else "there"
    return first[:40]


def checkin_body(name: str, trade: str, job_id: str, pro_name: str) -> str:
    who = name or "there"
    return (
        f"Hi {who}, checking in on your {trade or 'repair'} request "
        f"({job_ref(job_id)}) with {pro_name or 'your pro'}. How did things go?"
    )


def coerce_feedback(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Keep a price, a 1–5 rating, and one sentence. Drop anything the model invented as blank."""
    data = raw or {}
    price = " ".join(str(data.get("final_price_paid") or "").split())
    if price.lower() in {"", "null", "none", "n/a"}:
        price = ""
    rating_raw = data.get("rating_1_to_5")
    rating: int | None
    try:
        rating = int(rating_raw) if rating_raw not in (None, "", "null") else None
    except (TypeError, ValueError):
        rating = None
    if rating is not None and not 1 <= rating <= 5:
        rating = None
    summary = " ".join(str(data.get("review_summary") or "").split())[:300]
    return {
        "final_price_paid": price or None,
        "rating_1_to_5": rating,
        "review_summary": summary,
    }


def fallback_feedback(message: str) -> dict[str, Any]:
    """Used when Gemini is down. Pull a dollar amount and an obvious 1–5."""
    text = " ".join((message or "").split())
    price = ""
    found = _PRICE.search(text)
    if found:
        price = found.group(0)
    rating = None
    star = re.search(r"\b([1-5])\s*(?:stars?|/5)?\b", text, re.I)
    if star:
        rating = int(star.group(1))
    else:
        for word, score in _RATING_WORD.items():
            if re.search(rf"\b{word}\b", text, re.I):
                rating = score
                break
    return {
        "final_price_paid": price,
        "rating_1_to_5": rating or 0,
        "review_summary": text[:300],
    }


async def parse_job_feedback(message: str) -> dict[str, Any]:
    try:
        from app.services.gemini import gemini_service

        raw = await gemini_service.parse_job_feedback(message)
    except Exception as exc:
        logger.warning("job feedback parse failed: %s", exc)
        raw = fallback_feedback(message)
    return coerce_feedback(raw)


async def run_post_job_checkins(now: datetime | None = None) -> int:
    """Send due check-ins. The count moves only after Twilio accepts the message."""
    from app.services.jobs import job_store

    current = _aware(now or datetime.now(timezone.utc))
    jobs = await job_store.list_in_states({"assigned"})
    sent = 0
    for job in jobs:
        if not checkin_is_due(job, current):
            continue
        job_id = str(job.get("job_id") or "")
        if not await _send_checkin(job):
            continue
        count = int(job.get("checkin_count") or 0) + 1
        await job_store.update_fields(
            job_id,
            checkin_count=count,
            checkin_due_at=(current + CHECKIN_WAIT).isoformat(),
        )
        sent += 1
    return sent


async def _send_checkin(job: dict[str, Any]) -> bool:
    from app.services.homeowners import homeowner_store
    from app.services.pros import pro_store
    from app.services.whatsapp import whatsapp_service

    homeowner = str(job.get("homeowner_phone") or "")
    if not homeowner:
        return False
    profile = await homeowner_store.get(homeowner)
    pro = await pro_store.get(str(job.get("selected_pro_phone") or ""))
    pro_name = (pro or {}).get("business_name") or (pro or {}).get("name") or "your pro"
    trade = str(job.get("trade") or "repair")
    job_id = str(job.get("job_id") or "")
    first = homeowner_first_name(profile)
    body = (
        checkin_body(first, trade, job_id, pro_name)
        + "\n\nReply *Job completed*, *Still scheduled*, or *Didn't work out*."
    )
    variables = {
        "1": clean_template_value(first, "there", limit=40),
        "2": clean_template_value(trade, "repair", limit=32),
        "3": job_id,
        "4": clean_template_value(pro_name, "your pro", limit=40),
        "5": job_id,
    }
    sid = (settings.post_job_checkin_content_sid or "").strip()
    try:
        if sid:
            await whatsapp_service.send_content_template(
                homeowner,
                content_sid=sid,
                content_variables=variables,
            )
        else:
            await whatsapp_service.send_text_message(homeowner, body)
        return True
    except Exception as exc:
        logger.warning("post-job check-in failed for %s: %s", job_id, exc)
        return False
