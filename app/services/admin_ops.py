"""Read-only snapshots and the few writes the ops dashboard is allowed to make."""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.services.alert_reply import job_ref
from app.services.pro_capacity import fee_label, lead_label
from app.services.service_areas import AREA_CHOICES, AREA_LABELS, area_label, zips_for_areas
from app.services.session_window import parse_timestamp
from app.utils.phones import normalize_phone, to_e164

_QUOTE_STATUSES = {"details_given", "homeowner_passed", "selected", "not_selected"}


def _central():
    try:
        return ZoneInfo("America/Chicago")
    except ZoneInfoNotFoundError:
        return timezone(timedelta(hours=-6))


def format_ct(value: Any) -> str:
    ts = parse_timestamp(value)
    if ts is None:
        return ""
    local = ts.astimezone(_central())
    hour = local.strftime("%I").lstrip("0") or "12"
    return f"{local.strftime('%b %d')}, {hour}:{local.strftime('%M %p')} CT"


def ticket_amount(text: Any) -> float | None:
    match = re.search(r"\d+(?:\.\d+)?", str(text or "").replace(",", ""))
    if not match:
        return None
    return float(match.group(0))


def is_quote(response: dict[str, Any]) -> bool:
    status = str(response.get("status") or "")
    if status not in _QUOTE_STATUSES:
        return False
    return bool(response.get("estimate_text") or response.get("availability_text"))


def job_bucket(state: str, responses: list[dict[str, Any]]) -> str:
    if state in {"completed", "cancelled", "expired"}:
        return "closed"
    if state == "assigned":
        return "booked"
    if state == "awaiting_pick" or any(is_quote(response) for response in responses):
        return "quoted"
    return "open"


def pipeline_label(response: dict[str, Any]) -> str:
    status = str(response.get("status") or "")
    if status == "selected":
        return "Booked"
    if status == "homeowner_passed":
        return "Passed"
    if status == "declined":
        return "Declined"
    if status == "not_selected":
        return "Not selected"
    if status == "details_given":
        price = response.get("estimate_text") or "quote"
        when = response.get("availability_text") or "time not set"
        return f"Quoted: {price} @ {when}"
    if status == "interested":
        return "Interested, no quote yet"
    return "Pinged - No Reply"


def issue_summary(job: dict[str, Any]) -> str:
    diagnosis = job.get("diagnosis") or {}
    if not isinstance(diagnosis, dict):
        return ""
    brief = diagnosis.get("pro_brief") or {}
    text = ""
    if isinstance(brief, dict):
        text = str(brief.get("issue") or "")
    if not text:
        text = str(diagnosis.get("homeowner_message") or "")
    return " ".join(text.split())[:240]


def onboarding_label(pro: dict[str, Any], session: dict[str, Any] | None) -> str:
    if pro.get("opted_in"):
        return "Completed"
    ctx = (session or {}).get("context") or {}
    if (session or {}).get("mode") == "pro_onboarding":
        step = ctx.get("step") or "?"
        return f"Stuck on step {step}"
    return "Not finished"


def draft_name(session: dict[str, Any] | None) -> str:
    draft = ((session or {}).get("context") or {}).get("draft") or {}
    if not isinstance(draft, dict):
        return ""
    return str(draft.get("business_name") or draft.get("name") or "").strip()


def summarize(jobs: list[dict[str, Any]], pros: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(jobs)
    open_count = sum(1 for job in jobs if job.get("bucket") == "open")
    quoted = sum(1 for job in jobs if job.get("has_quote"))
    rate = round(100 * quoted / total) if total else 0
    completed = [job for job in jobs if job.get("state") == "completed"]
    tickets = [amount for job in completed if (amount := ticket_amount(job.get("final_price_paid"))) is not None]
    average = round(sum(tickets) / len(tickets)) if tickets else None
    active = [
        pro
        for pro in pros
        if pro.get("opted_in") and str(pro.get("status") or "active") == "active"
    ]
    trades: Counter[str] = Counter()
    for pro in active:
        named = [str(trade).replace("_", " ") for trade in (pro.get("trades") or [])]
        for trade in named or ["unset"]:
            trades[trade] += 1
    return {
        "total_jobs": total,
        "open_jobs": open_count,
        "active_pros": len(active),
        "trades": trades.most_common(),
        "quote_rate": rate,
        "quoted_jobs": quoted,
        "completed_jobs": len(completed),
        "avg_ticket": average,
    }


def _pro_name(pro: dict[str, Any] | None, phone: str) -> str:
    data = pro or {}
    return str(data.get("business_name") or data.get("name") or to_e164(phone) or "Pro")


async def load_ops(
    *,
    tab: str,
    status: str,
    trade: str,
    zone: str,
    query: str,
) -> dict[str, Any]:
    from app.services.conversation_state import conversation_state_store
    from app.services.homeowners import homeowner_store
    from app.services.jobs import job_store
    from app.services.pros import pro_store

    raw_jobs = await job_store.list_all()
    raw_pros = await pro_store.list_all()
    sessions = await conversation_state_store.list_all()
    pros_by_phone = {normalize_phone(str(pro.get("phone") or "")): pro for pro in raw_pros}
    owners: dict[str, dict[str, Any] | None] = {}
    presented: list[dict[str, Any]] = []
    for job in raw_jobs:
        job_id = str(job.get("job_id") or "")
        responses = await job_store.list_responses(job_id) if job_id else []
        bucket = job_bucket(str(job.get("state") or ""), responses)
        owner_phone = normalize_phone(str(job.get("homeowner_phone") or ""))
        if owner_phone not in owners:
            owners[owner_phone] = await homeowner_store.get(owner_phone) if owner_phone else None
        owner = owners.get(owner_phone) or {}
        pipeline = []
        for response in responses:
            pro_phone = normalize_phone(str(response.get("pro_phone") or ""))
            pipeline.append(
                {
                    "name": _pro_name(pros_by_phone.get(pro_phone), pro_phone),
                    "phone": to_e164(pro_phone),
                    "label": pipeline_label(response),
                }
            )
        presented.append(
            {
                "job_id": job_id,
                "ref": job_ref(job_id),
                "trade": str(job.get("trade") or "request").replace("_", " "),
                "zip": str(job.get("area_zip") or ""),
                "state": str(job.get("state") or ""),
                "bucket": bucket,
                "has_quote": any(is_quote(response) for response in responses),
                "created": format_ct(job.get("created_at")),
                "created_sort": str(job.get("created_at") or ""),
                "homeowner_name": str(owner.get("name") or "Homeowner"),
                "homeowner_phone": to_e164(owner_phone) if owner_phone else "",
                "issue": issue_summary(job),
                "pipeline": pipeline,
                "final_price": str(job.get("final_price_paid") or ""),
                "rating": job.get("homeowner_rating"),
                "review": str(job.get("homeowner_review_text") or ""),
                "can_quote": bucket in {"open", "quoted"},
            }
        )
    presented.sort(key=lambda item: item["created_sort"], reverse=True)
    wanted = status if status in {"open", "quoted", "booked", "closed"} else "all"
    visible_jobs = presented if wanted == "all" else [job for job in presented if job["bucket"] == wanted]

    needle = " ".join(query.lower().split())
    trade_key = trade.strip().lower()
    zone_key = zone.strip().lower()
    visible_pros = []
    for pro in raw_pros:
        phone = normalize_phone(str(pro.get("phone") or ""))
        session = sessions.get(phone)
        name = _pro_name(pro, phone)
        if name == to_e164(phone):
            name = draft_name(session) or name
        trades = [str(item) for item in (pro.get("trades") or [])]
        areas = [str(item) for item in (pro.get("service_areas") or [])]
        blob = " ".join(
            [name, str(pro.get("business_name") or ""), str(pro.get("name") or ""), phone]
        ).lower()
        if needle and needle not in blob and needle not in phone:
            continue
        if trade_key and trade_key not in {item.lower() for item in trades}:
            continue
        if zone_key and zone_key not in areas and "all" not in areas:
            continue
        visible_pros.append(
            {
                "phone": phone,
                "display_phone": to_e164(phone),
                "name": name,
                "trades": [item.replace("_", " ") for item in trades] or ["—"],
                "emergency": bool(pro.get("accepts_emergency")),
                "lead": lead_label(pro.get("standard_lead_time")) or "—",
                "fee": fee_label(pro.get("dispatch_fee_tier")) or "—",
                "zones": [area_label(item) for item in areas] or ["—"],
                "zone_keys": areas,
                "onboarding": onboarding_label(pro, session),
                "paused": str(pro.get("status") or "active") == "paused",
                "notes": str(pro.get("profile_notes") or ""),
            }
        )
    visible_pros.sort(key=lambda item: item["name"].lower())
    trade_choices = sorted({str(item) for pro in raw_pros for item in (pro.get("trades") or [])})
    return {
        "tab": "pros" if tab == "pros" else "jobs",
        "status": wanted,
        "trade": trade_key,
        "zone": zone_key,
        "query": query,
        "metrics": summarize(presented, raw_pros),
        "jobs": visible_jobs,
        "pros": visible_pros,
        "trade_choices": trade_choices,
        "zone_choices": [{"key": key, "label": label} for key, label, _note in AREA_CHOICES],
    }


async def add_manual_quote(job_id: str, name: str, phone: str, availability: str, price: str) -> str:
    """Create a quote for a pro we type in, then send the normal homeowner card."""
    from datetime import datetime, timezone

    from app.services.dispatch import maybe_send_homeowner_options
    from app.services.jobs import job_store
    from app.services.pros import pro_store

    who = " ".join((name or "").split())[:80]
    when = " ".join((availability or "").split())[:200]
    estimate = " ".join((price or "").split())[:200]
    phone_n = normalize_phone(phone)
    if not who or not when or not estimate or len(phone_n) < 10:
        return "need_fields"
    job = await job_store.get_job(job_id)
    if not job or job_bucket(str(job.get("state") or ""), []) == "closed" or job.get("state") == "assigned":
        return "closed"
    if job.get("state") not in {"dispatched", "awaiting_pick"}:
        return "closed"
    existing = await pro_store.get(phone_n)
    if not existing:
        await pro_store.upsert(
            phone_n,
            name=who,
            business_name=who,
            trades=[],
            opted_in=False,
            status="paused",
        )
    elif not (existing.get("business_name") or existing.get("name")):
        await pro_store.upsert(phone_n, name=who, business_name=who)
    await job_store.upsert_response(
        job_id,
        phone_n,
        status="details_given",
        availability_text=when,
        estimate_text=estimate,
        responded_at=datetime.now(timezone.utc).isoformat(),
        manual=True,
    )
    alerted = [normalize_phone(str(item)) for item in (job.get("alerted_pro_phones") or [])]
    if phone_n not in alerted:
        alerted.append(phone_n)
        await job_store.update_fields(job_id, alerted_pro_phones=alerted)
    await maybe_send_homeowner_options(job_id)
    return "quoted"


async def set_pro_status(phone: str, paused: bool) -> str:
    from app.services.pros import pro_store

    phone_n = normalize_phone(phone)
    if not await pro_store.get(phone_n):
        return "missing"
    await pro_store.upsert(phone_n, status="paused" if paused else "active")
    return "saved"


async def save_pro_ops(phone: str, notes: str, zones: list[str]) -> str:
    from app.services.pros import pro_store

    phone_n = normalize_phone(phone)
    if not await pro_store.get(phone_n):
        return "missing"
    chosen = [zone for zone in zones if zone in AREA_LABELS]
    if not chosen:
        return "need_zone"
    if "all" in chosen:
        chosen = ["all"]
    await pro_store.upsert(
        phone_n,
        profile_notes=" ".join((notes or "").split())[:500],
        service_areas=chosen,
        service_zips=zips_for_areas(chosen),
    )
    return "saved"
