"""Read and update the jobs and settings a signed-in pro is allowed to see."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from app.services.alert_reply import job_ref
from app.services.pro_capacity import FEE_LABELS, LEAD_LABELS, fee_label, lead_label
from app.services.pro_profile import needs_insurance_question
from app.services.service_areas import AREA_ALL, AREA_CHOICES, AREA_LABELS, area_for_zip, area_label, zips_for_areas
from app.utils.phones import normalize_phone, to_e164

logger = logging.getLogger(__name__)

_JOB_ID = re.compile(r"^[a-f0-9]{8,32}$")
_OPEN_STATES = frozenset({"dispatched", "awaiting_pick"})
_OPEN_STATUSES = frozenset({"", "alerted", "interested"})


def valid_job_id(job_id: str) -> bool:
    return bool(_JOB_ID.fullmatch(job_id or ""))


def _involved(phone: str, job: dict[str, Any], response: dict[str, Any] | None) -> bool:
    phone_n = normalize_phone(phone)
    selected = normalize_phone(str(job.get("selected_pro_phone") or ""))
    alerted = [normalize_phone(str(p)) for p in (job.get("alerted_pro_phones") or [])]
    responded = normalize_phone(str((response or {}).get("pro_phone") or ""))
    return phone_n in {selected, responded} or phone_n in alerted


def bucket_for_pro(phone: str, job: dict[str, Any], response: dict[str, Any] | None) -> str | None:
    """booked, open, or history. None when this pro has no stake in the job."""
    if not _involved(phone, job, response):
        return None
    phone_n = normalize_phone(phone)
    selected = normalize_phone(str(job.get("selected_pro_phone") or ""))
    state = str(job.get("state") or "")
    status = str((response or {}).get("status") or "")
    if state == "assigned" and selected == phone_n:
        return "booked"
    if state == "completed" and selected == phone_n:
        return "history"
    if status in {"declined", "not_selected", "homeowner_passed"}:
        return "history"
    if status == "details_given":
        return "history"
    if state in _OPEN_STATES and status in _OPEN_STATUSES:
        return "open"
    if state in {"expired", "cancelled"} and status in {"interested", "details_given"}:
        return "history"
    return None


def history_badge(phone: str, job: dict[str, Any], response: dict[str, Any] | None) -> str:
    phone_n = normalize_phone(phone)
    selected = normalize_phone(str(job.get("selected_pro_phone") or ""))
    status = str((response or {}).get("status") or "")
    if job.get("state") == "completed" and selected == phone_n:
        return "Completed"
    if status == "not_selected":
        return "Not Selected"
    if status in {"declined", "homeowner_passed"}:
        return "Declined"
    if status == "details_given":
        return "Quoted - Pending"
    return "Declined"


def can_complete(phone: str, job: dict[str, Any] | None) -> bool:
    if not job:
        return False
    selected = normalize_phone(str(job.get("selected_pro_phone") or ""))
    return selected == normalize_phone(phone) and job.get("state") == "assigned"


def can_answer_alert(
    phone: str, job: dict[str, Any] | None, response: dict[str, Any] | None
) -> bool:
    if not job or not _involved(phone, job, response):
        return False
    if job.get("state") not in _OPEN_STATES:
        return False
    status = str((response or {}).get("status") or "")
    return status in _OPEN_STATUSES


def clean_profile_form(data: dict[str, Any]) -> dict[str, Any] | str:
    """Return pro fields to save, or an error sentence."""
    lead = str(data.get("standard_lead_time") or "")
    fee = str(data.get("dispatch_fee_tier") or "")
    if lead not in LEAD_LABELS:
        return "Choose a lead time."
    if fee not in FEE_LABELS:
        return "Choose a service-call fee."
    raw_areas = data.get("service_areas") or []
    if isinstance(raw_areas, str):
        raw_areas = [raw_areas]
    areas = [str(key) for key in raw_areas if str(key) in AREA_LABELS]
    if not areas:
        return "Choose at least one service area."
    if AREA_ALL in areas:
        areas = [AREA_ALL]
    raw_languages = data.get("languages") or []
    if isinstance(raw_languages, str):
        raw_languages = [raw_languages]
    languages = [str(code) for code in raw_languages if str(code) in {"en", "es"}]
    if not languages:
        return "Choose English, Spanish, or both."
    notes = " ".join(str(data.get("profile_notes") or "").split())[:500]
    cleaned: dict[str, Any] = {
        "accepts_emergency": str(data.get("accepts_emergency") or "") in {"1", "true", "on", "yes"},
        "standard_lead_time": lead,
        "availability_text": lead_label(lead),
        "dispatch_fee_tier": fee,
        "rate_text": fee_label(fee),
        "service_areas": areas,
        "service_zips": zips_for_areas(areas),
        "languages": languages,
        "profile_notes": notes,
    }
    insured = str(data.get("insured") or "")
    if insured in {"yes", "no"}:
        cleaned["insured"] = insured == "yes"
    photo = str(data.get("free_photo_estimate") or "")
    if photo in {"yes", "no"}:
        cleaned["free_photo_estimate"] = photo == "yes"
    return cleaned


def _issue(job: dict[str, Any]) -> str:
    diagnosis = job.get("diagnosis") or {}
    if not isinstance(diagnosis, dict):
        return ""
    brief = diagnosis.get("pro_brief") or {}
    if isinstance(brief, dict) and brief.get("issue"):
        return str(brief["issue"])
    return str(diagnosis.get("homeowner_message") or "")


def _when_label(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        ts = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text[:32]
    return ts.strftime("%b %d, %Y")


def present_card(
    phone: str,
    job: dict[str, Any],
    response: dict[str, Any] | None,
    *,
    bucket: str,
    homeowner: dict[str, Any] | None = None,
    access_notes: list[str] | None = None,
) -> dict[str, Any]:
    phone_n = normalize_phone(phone)
    selected = normalize_phone(str(job.get("selected_pro_phone") or ""))
    zip_code = str(job.get("area_zip") or "")
    area_key = area_for_zip(zip_code)
    place = ", ".join(part for part in (area_label(area_key) if area_key else "", zip_code) if part)
    show_contact = bucket == "booked" or (
        bucket == "history" and job.get("state") == "completed" and selected == phone_n
    )
    owner = homeowner or {}
    return {
        "job_id": job.get("job_id") or "",
        "ref": job_ref(str(job.get("job_id") or "")),
        "trade": str(job.get("trade") or "request").replace("_", " "),
        "place": place or "Greater Houston",
        "issue": _issue(job),
        "when": str((response or {}).get("availability_text") or ""),
        "estimate": str((response or {}).get("estimate_text") or ""),
        "date": _when_label(job.get("updated_at") or (response or {}).get("responded_at") or job.get("created_at")),
        "badge": history_badge(phone, job, response) if bucket == "history" else "",
        "needs_quote": str((response or {}).get("status") or "") == "interested",
        "show_contact": show_contact,
        "homeowner_name": (owner.get("name") or "Homeowner") if show_contact else "",
        "homeowner_phone": to_e164(str(job.get("homeowner_phone") or "")) if show_contact else "",
        "access_notes": list(access_notes or []) if show_contact else [],
    }


def area_options(selected: list[str] | None) -> list[dict[str, Any]]:
    chosen = set(selected or [])
    return [
        {
            "key": key,
            "label": label,
            "note": note,
            "checked": key in chosen or (key == AREA_ALL and AREA_ALL in chosen),
        }
        for key, label, note in AREA_CHOICES
    ]


async def _response_for(job_id: str, phone: str) -> dict[str, Any] | None:
    from app.services.jobs import job_store

    phone_n = normalize_phone(phone)
    for response in await job_store.list_responses(job_id):
        if normalize_phone(str(response.get("pro_phone") or "")) == phone_n:
            return response
    return None


async def load_dashboard(phone: str) -> dict[str, Any]:
    from app.services.homeowners import homeowner_store
    from app.services.jobs import job_store
    from app.services.pros import pro_store

    phone_n = normalize_phone(phone)
    pro = await pro_store.get(phone_n) or {}
    jobs = await job_store.list_for_pro(phone_n)
    seen = {str(job.get("job_id") or "") for job in jobs}
    for job_id in await pro_store.pending_alert_ids(phone_n):
        if job_id in seen:
            continue
        extra = await job_store.get_job(job_id)
        if extra:
            jobs.append(extra)
    jobs.sort(key=lambda job: str(job.get("updated_at") or job.get("created_at") or ""), reverse=True)
    booked: list[dict[str, Any]] = []
    open_alerts: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []
    for job in jobs:
        job_id = str(job.get("job_id") or "")
        response = await _response_for(job_id, phone_n) if job_id else None
        bucket = bucket_for_pro(phone_n, job, response)
        if not bucket:
            continue
        homeowner = None
        notes: list[str] = []
        if bucket == "booked" or (bucket == "history" and job.get("state") == "completed"):
            owner_phone = str(job.get("homeowner_phone") or "")
            if owner_phone:
                homeowner = await homeowner_store.get(owner_phone)
                if bucket == "booked":
                    notes = await homeowner_store.list_service_notes(owner_phone)
        card = present_card(
            phone_n, job, response, bucket=bucket, homeowner=homeowner, access_notes=notes
        )
        if bucket == "booked":
            booked.append(card)
        elif bucket == "open":
            open_alerts.append(card)
        else:
            history.append(card)
    return {
        "pro": pro,
        "business": pro.get("business_name") or pro.get("name") or "Your profile",
        "booked": booked,
        "open_alerts": open_alerts,
        "history": history,
        "areas": area_options(list(pro.get("service_areas") or [])),
        "lead": pro.get("standard_lead_time") or "",
        "fee": pro.get("dispatch_fee_tier") or "",
        "emergency": bool(pro.get("accepts_emergency")),
        "lead_choices": [{"key": key, "label": label} for key, label in LEAD_LABELS.items()],
        "fee_choices": [{"key": key, "label": label} for key, label in FEE_LABELS.items()],
        "languages": list(pro.get("languages") or []),
        "show_insured": needs_insurance_question(list(pro.get("trades") or [])),
        "insured": pro.get("insured"),
        "free_photo_estimate": pro.get("free_photo_estimate"),
        "profile_notes": pro.get("profile_notes") or "",
    }


async def mark_completed(phone: str, job_id: str) -> str:
    from app.services.jobs import job_store
    from app.services.whatsapp import whatsapp_service

    if not valid_job_id(job_id):
        return "updated"
    job = await job_store.get_job(job_id)
    if not can_complete(phone, job):
        return "updated"
    await job_store.set_state(job_id, "completed")
    owner = str((job or {}).get("homeowner_phone") or "")
    if owner:
        try:
            await whatsapp_service.send_text_message(
                owner,
                f"Your pro marked {job_ref(job_id)} complete. "
                "Reply here if anything still needs attention.",
            )
        except Exception as e:
            logger.warning("complete notice failed: %s", e)
    return "completed"


async def pass_on_job(phone: str, job_id: str) -> str:
    from app.services.jobs import job_store
    from app.services.pros import pro_store

    if not valid_job_id(job_id):
        return "updated"
    phone_n = normalize_phone(phone)
    job = await job_store.get_job(job_id)
    response = await _response_for(job_id, phone_n)
    if not can_answer_alert(phone_n, job, response):
        return "updated"
    await job_store.upsert_response(job_id, phone_n, status="declined")
    await pro_store.remove_pending_alert(phone_n, job_id)
    return "passed"


async def submit_quote(phone: str, job_id: str, availability: str, estimate: str) -> str:
    from datetime import datetime, timezone

    from app.models import MODE_PRO_AWAITING_DETAILS
    from app.services.conversation_state import conversation_state_store
    from app.services.jobs import job_store
    from app.services.pros import pro_store
    from app.services.whatsapp import whatsapp_service

    if not valid_job_id(job_id):
        return "updated"
    when = " ".join((availability or "").split())[:200]
    price = " ".join((estimate or "").split())[:200]
    if not when or not price:
        return "need_both"
    phone_n = normalize_phone(phone)
    job = await job_store.get_job(job_id)
    response = await _response_for(job_id, phone_n)
    if not can_answer_alert(phone_n, job, response):
        return "updated"
    await job_store.upsert_response(
        job_id,
        phone_n,
        status="details_given",
        availability_text=when,
        estimate_text=price,
        estimate_low=None,
        estimate_high=None,
        responded_at=datetime.now(timezone.utc).isoformat(),
    )
    await pro_store.remove_pending_alert(phone_n, job_id)
    state = await conversation_state_store.get(phone_n)
    ctx = state.get("context") or {}
    locked = ctx.get("active_job_id") or ctx.get("job_id")
    if state.get("mode") == MODE_PRO_AWAITING_DETAILS and locked == job_id:
        await conversation_state_store.clear(phone_n)
    try:
        await whatsapp_service.send_text_message(
            phone_n,
            "Got it — I'll share this with the homeowner:\n"
            f"Availability: {when}\n"
            f"Estimate: {price}",
        )
    except Exception as e:
        logger.warning("quote confirmation failed: %s", e)
    try:
        from app.services.dispatch import maybe_send_homeowner_options

        await maybe_send_homeowner_options(job_id)
    except Exception as e:
        logger.warning("homeowner options after dashboard quote failed: %s", e)
    return "quoted"


async def save_profile(phone: str, data: dict[str, Any]) -> str:
    from app.services.pros import pro_store

    cleaned = clean_profile_form(data)
    if isinstance(cleaned, str):
        return cleaned
    existing = await pro_store.get(phone)
    if not existing:
        return "Create your pro profile in WhatsApp first. Reply JOIN or Pro Signup."
    await pro_store.upsert(phone, **cleaned)
    return ""
