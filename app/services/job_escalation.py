"""15-, 30-, and 60-minute rescue when a dispatched job still has no view or quote.

The live timer is the homeowner-followups cron. A decline does not stop it.
A VIEW, an Interested tap, or a quote does.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import settings
from app.services.alert_reply import job_ref
from app.services.homeowner_profile import trade_phrase
from app.services.session_window import clean_template_value, parse_timestamp

logger = logging.getLogger(__name__)

REASSURE_AFTER = timedelta(minutes=15)
FOUNDER_AFTER = timedelta(minutes=30)
CHOICE_AFTER = timedelta(minutes=60)
URGENT_AFTER = timedelta(minutes=10)
_ENGAGED = frozenset({"viewed", "interested", "details_given", "selected"})
_URGENT_MARKERS = (
    "flooding",
    "burst pipe",
    "active leak",
    "gas smell",
    "sparks",
    "no ac",
    "no a/c",
)


def _aware(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def issue_summary(job: dict[str, Any]) -> str:
    diagnosis = job.get("diagnosis") if isinstance(job.get("diagnosis"), dict) else {}
    brief = diagnosis.get("pro_brief") if isinstance(diagnosis.get("pro_brief"), dict) else {}
    text = " ".join(str(brief.get("issue") or "").split())
    return text or "home repair"


def looks_urgent(*parts: str) -> bool:
    text = " ".join(part for part in parts if part).lower().replace("a/c", "ac")
    return any(marker.replace("a/c", "ac") in text for marker in _URGENT_MARKERS)


def urgency_prompt(*parts: str) -> str:
    if looks_urgent(*parts):
        return "This sounds urgent. How urgent is this repair?"
    return "How urgent is this repair?"


def has_pro_response(responses: list[dict[str, Any]]) -> bool:
    return any(str(response.get("status") or "") in _ENGAGED for response in responses)


def due_stage(
    job: dict[str, Any], responses: list[dict[str, Any]], now: datetime
) -> str | None:
    """stop, reassure, founder, choice, or None. One catch-up stage per pass."""
    if job.get("state") != "dispatched":
        return None
    if job.get("escalation_stopped") or job.get("escalation_choice"):
        return None
    if not (job.get("alerted_pro_phones") or []):
        return None
    if has_pro_response(responses):
        return "stop"
    created = parse_timestamp(job.get("dispatched_at") or job.get("created_at"))
    if created is None:
        return None
    age = _aware(now) - _aware(created)
    if job.get("is_urgent"):
        if age >= URGENT_AFTER and not job.get("escalation_urgent_sent"):
            return "urgent_places"
        return None
    if age >= REASSURE_AFTER and not job.get("escalation_15_sent"):
        return "reassure"
    if age >= FOUNDER_AFTER and not job.get("escalation_30_sent"):
        return "founder"
    if age >= CHOICE_AFTER and not job.get("escalation_60_sent"):
        return "choice"
    return None


def reassure_body(trade: str) -> str:
    label = trade_phrase(trade)
    return (
        f"Quick update—we've notified our vetted local {label} pros in your area "
        "and are waiting on their availability. Hang tight, we'll update you as "
        "soon as the first pro responds!"
    )


def founder_body(job: dict[str, Any], *, headline: str = "Unanswered ticket") -> str:
    job_id = str(job.get("job_id") or "")
    return (
        f"{headline} {job_ref(job_id)}\n"
        f"Trade: {job.get('trade') or 'repair'}\n"
        f"ZIP: {job.get('area_zip') or 'unknown'}\n"
        f"Summary: {issue_summary(job)}\n"
        f"Homeowner: {job.get('homeowner_phone') or 'unknown'}"
    )


def urgent_places_message(trade: str, places: list[dict[str, Any]]) -> str:
    label = trade_phrase(trade)
    lead = (
        f"Because this is urgent and our network pros haven't confirmed availability "
        f"in the last 10 minutes, here are 3 top-rated local {label} companies you can "
        "call immediately—we'll also let you know if one of our vetted pros responds shortly."
    )
    if not places:
        return (
            f"{lead}\n\nI couldn't find a public listing at 4.6 stars with 30 reviews yet. "
            "I'll still message you if one of our pros responds."
        )
    lines = [lead, ""]
    for index, place in enumerate(places[:3], start=1):
        rating = place.get("rating")
        try:
            reviews = int(place.get("reviews") or 0)
        except (TypeError, ValueError):
            reviews = 0
        try:
            stars = f"{float(rating):.1f}★ · {reviews} reviews"
        except (TypeError, ValueError):
            stars = f"{reviews} reviews"
        lines.append(f"{index}. *{place.get('name') or 'Local company'}* ({stars})")
        lines.append(f"   📞 {place.get('phone') or 'phone not listed'}")
    return "\n".join(lines)


def choice_body() -> str:
    return (
        "Our network pros are currently tied up on active jobs and haven't "
        "confirmed availability yet. How would you like to proceed?"
    )


def founder_destination() -> str:
    from app.utils.phones import normalize_phone

    raw = (settings.founder_alert_phone or settings.operator_phone or "").strip()
    return normalize_phone(raw)


async def apply_escalation_choice(phone: str, job: dict[str, Any], choice: str) -> None:
    """keep_waiting leaves the network open. direct_numbers sends public listings."""
    from app.services.jobs import job_store
    from app.services.places import no_partner_message, places_offer_message, search_local_specialists
    from app.services.whatsapp import whatsapp_service

    job_id = str(job.get("job_id") or "")
    if not job_id or job.get("escalation_choice"):
        return
    if choice == "keep_waiting":
        await job_store.update_fields(
            job_id, escalation_choice="keep_waiting", escalation_stopped=True
        )
        await whatsapp_service.send_text_message(
            phone,
            f"I'll keep checking with our pros on {job_ref(job_id)} "
            "and message you when someone replies.",
        )
        return
    trade = str(job.get("trade") or "repair")
    area_zip = str(job.get("area_zip") or "")
    places = await search_local_specialists(trade, area_zip) if area_zip else []
    if places:
        body = places_offer_message(trade, area_zip, places)
    else:
        body = no_partner_message(trade, area_zip)
    await whatsapp_service.send_text_message(phone, body)
    await job_store.update_fields(
        job_id, escalation_choice="places_sent", escalation_stopped=True
    )


async def run_unanswered_escalations(now: datetime | None = None) -> dict[str, int]:
    from app.services.jobs import job_store

    current = _aware(now or datetime.now(timezone.utc))
    reassured = 0
    founders = 0
    choices = 0
    urgent = 0
    stopped = 0
    for job in await job_store.list_in_states({"dispatched"}):
        job_id = str(job.get("job_id") or "")
        if not job_id:
            continue
        responses = await job_store.list_responses(job_id)
        stage = due_stage(job, responses, current)
        if stage == "stop":
            await job_store.update_fields(job_id, escalation_stopped=True)
            stopped += 1
            continue
        if stage == "reassure" and await _reassure(job, responses):
            await job_store.update_fields(job_id, escalation_15_sent=True)
            reassured += 1
            continue
        if stage == "founder" and await _alert_founder(job):
            await job_store.update_fields(job_id, escalation_30_sent=True)
            founders += 1
            continue
        if stage == "choice" and await _ask_homeowner(job):
            await job_store.update_fields(job_id, escalation_60_sent=True)
            choices += 1
            continue
        if stage == "urgent_places" and await _send_urgent_places(job):
            await job_store.update_fields(job_id, escalation_urgent_sent=True)
            urgent += 1
    return {
        "processedEscalation15": reassured,
        "processedEscalation30": founders,
        "processedEscalation60": choices,
        "processedEscalationUrgent": urgent,
        "processedEscalationStopped": stopped,
    }


async def _reassure(job: dict[str, Any], responses: list[dict[str, Any]]) -> bool:
    from app.services.whatsapp import whatsapp_service

    homeowner = str(job.get("homeowner_phone") or "")
    if not homeowner:
        return False
    try:
        await whatsapp_service.send_text_message(
            homeowner, reassure_body(str(job.get("trade") or "repair"))
        )
    except Exception as exc:
        logger.warning("15-minute reassurance failed for %s: %s", job.get("job_id"), exc)
        return False
    await _remind_silent_pros(job, responses)
    return True


async def _remind_silent_pros(job: dict[str, Any], responses: list[dict[str, Any]]) -> None:
    from app.services.session_window import get_last_inbound, window_is_open
    from app.services.whatsapp import whatsapp_service
    from app.utils.phones import normalize_phone, to_e164

    answered = {
        normalize_phone(str(response.get("pro_phone") or "")): str(response.get("status") or "")
        for response in responses
    }
    job_id = str(job.get("job_id") or "")
    trade = str(job.get("trade") or "repair")
    area_zip = str(job.get("area_zip") or "")
    summary = issue_summary(job)
    ticket = clean_template_value(job_id, "ticket", limit=32)
    variables = {
        "1": ticket,
        "2": clean_template_value(trade, "repair", limit=40),
        "3": clean_template_value(area_zip, "your area", limit=32),
        "4": clean_template_value(summary, "home repair", limit=200),
    }
    sid = (settings.twilio_dispatch_utility_template_sid or "").strip()
    reminder = (
        f"Reminder: dispatch ticket {job_ref(job_id)} is still waiting. "
        f"Reply VIEW {job_id} to inspect it or PASS {job_id} to decline."
    )
    for raw in job.get("alerted_pro_phones") or []:
        phone = normalize_phone(str(raw))
        if not phone or answered.get(phone, "alerted") != "alerted":
            continue
        try:
            if window_is_open(await get_last_inbound(phone)):
                await whatsapp_service.send_text_message(to_e164(phone), reminder)
            elif sid:
                await whatsapp_service.send_content_template(
                    to_e164(phone),
                    content_sid=sid,
                    content_variables=variables,
                )
            else:
                logger.error(
                    "pro reminder skipped for %s; utility template is unset", job_id
                )
        except Exception as exc:
            logger.warning("pro reminder failed for job %s: %s", job_id, exc)


async def _alert_founder(job: dict[str, Any], *, headline: str = "Unanswered ticket") -> bool:
    from app.services.events import log_event
    from app.services.session_window import get_last_inbound, window_is_open
    from app.services.whatsapp import whatsapp_service
    from app.utils.phones import to_e164

    phone = founder_destination()
    body = founder_body(job, headline=headline)
    if not phone:
        logger.error("FOUNDER_ALERT_PHONE is empty; unanswered job %s", job.get("job_id"))
        await log_event(
            phone="system",
            role="system",
            type="founder_alert_missing",
            job_id=str(job.get("job_id") or ""),
            payload={"body": body[:500]},
        )
        return True
    if not window_is_open(await get_last_inbound(phone)):
        logger.error(
            "founder rescue window is closed for job %s. %s",
            job.get("job_id"),
            body.replace("\n", " | "),
        )
        await log_event(
            phone=phone,
            role="system",
            type="founder_alert_window_closed",
            job_id=str(job.get("job_id") or ""),
            payload={"body": body[:500]},
        )
        return False
    try:
        await whatsapp_service.send_text_message(to_e164(phone), body)
    except Exception as exc:
        logger.warning("founder alert failed for %s: %s", job.get("job_id"), exc)
        return False
    return True


async def _ask_homeowner(job: dict[str, Any]) -> bool:
    from app.services.session_window import get_last_inbound, window_is_open
    from app.services.whatsapp import whatsapp_service
    from app.utils.phones import to_e164

    homeowner = str(job.get("homeowner_phone") or "")
    job_id = str(job.get("job_id") or "")
    if not homeowner or not job_id:
        return False
    body = choice_body()
    try:
        if window_is_open(await get_last_inbound(homeowner)):
            await whatsapp_service.send_session_buttons(
                to_e164(homeowner),
                body,
                [
                    (f"keep_waiting:{job_id}", "Keep waiting"),
                    (f"direct_numbers:{job_id}", "Show companies"),
                ],
            )
        else:
            await whatsapp_service.send_text_message(
                to_e164(homeowner),
                f"{body}\n\nReply KEEP to keep checking with our pros, "
                "or CALL for 3 companies you can phone now.",
            )
    except Exception as exc:
        logger.warning("60-minute choice failed for %s: %s", job_id, exc)
        return False
    return True


async def _send_urgent_places(job: dict[str, Any]) -> bool:
    from app.services.places import search_local_specialists
    from app.services.whatsapp import whatsapp_service
    from app.utils.phones import to_e164

    homeowner = str(job.get("homeowner_phone") or "")
    if not homeowner:
        return False
    trade = str(job.get("trade") or "repair")
    area_zip = str(job.get("area_zip") or "")
    places = await search_local_specialists(trade, area_zip) if area_zip else []
    try:
        await whatsapp_service.send_text_message(
            to_e164(homeowner), urgent_places_message(trade, places)
        )
    except Exception as exc:
        logger.warning("urgent places fallback failed for %s: %s", job.get("job_id"), exc)
        return False
    return True
