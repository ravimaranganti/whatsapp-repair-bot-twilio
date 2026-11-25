"""Phase 4 — brokered job dispatch, pro responses, homeowner pick."""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from app.config import settings
from app.models import (
    MODE_AWAITING_AREA,
    MODE_AWAITING_DISPATCH_CONSENT,
    MODE_AWAITING_PICK,
    MODE_AWAITING_QUOTE_QUESTION,
    MODE_AWAITING_SHARE_NOTES,
    MODE_AWAITING_SHOW_OPTIONS,
    MODE_IDLE,
    MODE_PRO_AWAITING_DETAILS,
    Diagnosis,
)
from app.models.diagnosis import TRADE_TO_PRO_CATEGORY
from app.services.conversation_state import conversation_state_store
from app.services.events import log_event
from app.services.homeowner_actions import (
    parse_homeowner_payload,
    quote_choice_message,
    typed_choice,
)
from app.services.homeowners import homeowner_store
from app.services.jobs import job_store, resolve_pro_from_pick_payload
from app.services.alert_reply import job_ref, resolve_alert_reply
from app.services.matching import select_pros_for_job
from app.services.pro_capacity import fee_label
from app.services.pros import pro_store
from app.services.session_window import (
    choose_delivery,
    clean_template_value,
    get_last_inbound,
    should_retry_with_template,
    window_is_open,
)
from app.services.whatsapp import twilio_error_code, whatsapp_service
from app.utils.phones import normalize_phone, phone_hash, to_e164

logger = logging.getLogger(__name__)


async def _send_choice(
    phone: str, sid: str, variables: dict[str, str], fallback: str
) -> None:
    content_sid = (sid or "").strip()
    if content_sid:
        try:
            await whatsapp_service.send_content_template(
                phone, content_sid=content_sid, content_variables=variables
            )
            return
        except Exception as e:
            logger.warning("homeowner choice template failed: %s", e)
    await whatsapp_service.send_text_message(phone, fallback)


def _owns(job: dict[str, Any] | None, phone: str) -> bool:
    return bool(job) and normalize_phone(str(job.get("homeowner_phone") or "")) == normalize_phone(
        phone
    )


async def _quote_phone(job_id: str, quote_id: str) -> str | None:
    responses = await job_store.list_responses(job_id)
    for response in responses:
        phone = normalize_phone(str(response.get("pro_phone") or ""))
        if phone and (phone_hash(phone) == quote_id or phone == normalize_phone(quote_id)):
            return phone
    return None


async def _open_quotes(phone: str) -> list[dict[str, str]]:
    jobs = await job_store.list_for_homeowner(phone, {"awaiting_pick"})
    found: list[dict[str, str]] = []
    for job in jobs:
        for response in await job_store.list_responses(job["job_id"]):
            if response.get("status") != "details_given":
                continue
            pro_phone = normalize_phone(str(response.get("pro_phone") or ""))
            pro = await pro_store.get(pro_phone)
            found.append(
                {
                    "job_id": job["job_id"],
                    "pro_phone": pro_phone,
                    "quote_id": phone_hash(pro_phone),
                    "name": (pro or {}).get("business_name")
                    or (pro or {}).get("name")
                    or "Pro",
                }
            )
    return found


async def _already_updated(phone: str, job_id: str) -> None:
    await whatsapp_service.send_text_message(
        phone,
        f"This quote or job ({job_ref(job_id)}) has already been updated.",
    )


async def after_diagnosis(
    *,
    phone: str,
    diagnosis: Diagnosis,
    photo_url: str | None,
) -> None:
    """
    Post-diagnosis UX: DIY buttons path or start consent/ZIP for dispatch.
    When DISPATCH_ENABLED is false, only send diagnosis text (caller already did).
    """
    if not settings.dispatch_enabled:
        return

    phone_n = normalize_phone(phone)
    await homeowner_store.ensure(phone_n)

    if diagnosis.diy_appropriate and diagnosis.diy_steps:
        # homeowner_message already sent by agent; follow up with steps + buttons
        steps = "\n".join(f"{i}. {s}" for i, s in enumerate(diagnosis.diy_steps, 1))
        await whatsapp_service.send_text_message(
            phone_n,
            f"*Try this:*\n{steps}\n\n"
            "Reply *That fixed it*, *Didn't help*, or *Get a pro*.",
        )
        await conversation_state_store.set(
            phone_n,
            MODE_AWAITING_DISPATCH_CONSENT,
            {
                "diy_pending": True,
                "diagnosis": diagnosis.model_dump(mode="json"),
                "photo_url": photo_url,
            },
        )
        return

    await _start_consent_flow(phone_n, diagnosis, photo_url)


async def _start_consent_flow(
    phone: str, diagnosis: Diagnosis, photo_url: str | None
) -> None:
    ho = await homeowner_store.ensure(phone)
    active = ho.get("active_job_id")
    if active:
        job = await job_store.get_job(active)
        if job and job.get("state") in {
            "diagnosed",
            "awaiting_consent",
            "dispatched",
            "awaiting_pick",
            "assigned",
        }:
            await conversation_state_store.set(
                phone,
                "awaiting_new_issue_confirm",
                {
                    "diagnosis": diagnosis.model_dump(mode="json"),
                    "photo_url": photo_url,
                    "active_job_id": active,
                },
            )
            await _send_choice(
                phone,
                settings.tpl_new_issue_sid,
                {"1": active},
                (
                    f"You already have an open request ({job_ref(active)}). "
                    "Is this a *new* issue?\n"
                    "Reply *Yes* to start a new one, or *No* to keep the current request."
                ),
            )
            return

    ctx = {
        "diagnosis": diagnosis.model_dump(mode="json"),
        "photo_url": photo_url,
    }
    if not ho.get("area_zip"):
        await conversation_state_store.set(phone, MODE_AWAITING_AREA, ctx)
        await whatsapp_service.send_text_message(
            phone,
            "What's your ZIP code so I can find nearby pros?",
        )
        return

    await _ask_dispatch_consent(phone, diagnosis, photo_url, ho["area_zip"])


async def _ask_dispatch_consent(
    phone: str, diagnosis: Diagnosis, photo_url: str | None, area_zip: str
) -> None:
    trade = diagnosis.trade.value
    job = await job_store.create_job(
        homeowner_phone=phone,
        diagnosis=diagnosis,
        area_zip=area_zip,
        photo_url=photo_url,
        state="diagnosed",
    )
    job_id = job["job_id"]
    await job_store.set_state(job_id, "awaiting_consent")
    await homeowner_store.update(phone, active_job_id=job_id)
    await conversation_state_store.set(
        phone,
        MODE_AWAITING_DISPATCH_CONSENT,
        {
            "diagnosis": diagnosis.model_dump(mode="json"),
            "photo_url": photo_url,
            "area_zip": area_zip,
            "job_id": job_id,
            "active_job_id": job_id,
        },
    )
    await _send_choice(
        phone,
        settings.tpl_dispatch_consent_sid,
        {"1": job_id},
        (
            f"Want me to ask 3–5 local *{trade}* pros for availability and a rough estimate? "
            "Your number stays private until you choose one.\n\n"
            f"Reply *Yes, find pros* or *No thanks* for {job_ref(job_id)}."
        ),
    )


async def _apply_homeowner_tap(
    phone: str, tap: Any, ctx: dict[str, Any]
) -> bool:
    """Run a button that already names its job. True when the tap was ours."""
    job = await job_store.get_job(tap.job_id) if tap.job_id else None
    if tap.action in {"rating_great", "rating_okay", "rating_not_good"}:
        return False
    if not _owns(job, phone):
        await _already_updated(phone, tap.job_id)
        return True

    if tap.action == "confirm_job":
        if job.get("state") != "awaiting_consent":
            await _already_updated(phone, tap.job_id)
            return True
        diagnosis = Diagnosis.model_validate(job.get("diagnosis") or ctx.get("diagnosis"))
        await dispatch_job(
            phone,
            {
                "diagnosis": diagnosis.model_dump(mode="json"),
                "photo_url": job.get("photo_url"),
                "area_zip": job.get("area_zip"),
                "job_id": tap.job_id,
                "active_job_id": tap.job_id,
            },
        )
        return True

    if tap.action == "cancel_job":
        if job.get("state") != "awaiting_consent":
            await _already_updated(phone, tap.job_id)
            return True
        await job_store.set_state(tap.job_id, "cancelled")
        ho = await homeowner_store.get(phone)
        if (ho or {}).get("active_job_id") == tap.job_id:
            await homeowner_store.update(phone, active_job_id=None)
        await conversation_state_store.clear(phone)
        await whatsapp_service.send_text_message(
            phone, "No problem. Send another photo anytime if you change your mind."
        )
        return True

    if tap.action == "show_options":
        if job.get("state") not in {"dispatched", "awaiting_pick"}:
            await _already_updated(phone, tap.job_id)
            return True
        await maybe_send_homeowner_options(tap.job_id, force=True)
        return True

    if tap.action in {"book_quote", "decline_quote", "ask_quote"}:
        pro_phone = await _quote_phone(tap.job_id, tap.quote_id)
        responses = await job_store.list_responses(tap.job_id)
        mine = next(
            (
                response
                for response in responses
                if normalize_phone(str(response.get("pro_phone") or "")) == pro_phone
            ),
            None,
        )
        active = (
            job.get("state") == "awaiting_pick"
            and mine
            and mine.get("status") == "details_given"
        )
        if not pro_phone or not active:
            await _already_updated(phone, tap.job_id)
            return True
        if tap.action == "decline_quote":
            await job_store.upsert_response(tap.job_id, pro_phone, status="homeowner_passed")
            pro = await pro_store.get(pro_phone)
            name = (pro or {}).get("business_name") or (pro or {}).get("name") or "that pro"
            await whatsapp_service.send_text_message(
                phone, f"Passed on *{name}* for {job_ref(tap.job_id)}."
            )
            return True
        if tap.action == "ask_quote":
            pro = await pro_store.get(pro_phone)
            name = (pro or {}).get("business_name") or (pro or {}).get("name") or "that pro"
            await conversation_state_store.set(
                phone,
                MODE_AWAITING_QUOTE_QUESTION,
                {
                    "job_id": tap.job_id,
                    "active_job_id": tap.job_id,
                    "pro_phone": pro_phone,
                    "active_quote_id": tap.quote_id,
                },
            )
            await whatsapp_service.send_text_message(
                phone, f"What do you want to ask *{name}* about {job_ref(tap.job_id)}?"
            )
            return True
        await _choose_pro(phone, job, pro_phone)
        return True

    if tap.action in {"share_yes", "share_no"}:
        pro_phone = normalize_phone(
            str((await _quote_phone(tap.job_id, tap.quote_id)) or ctx.get("pro_phone") or "")
        )
        selected = normalize_phone(str(job.get("selected_pro_phone") or ""))
        responses = await job_store.list_responses(tap.job_id)
        already = any(
            normalize_phone(str(response.get("pro_phone") or "")) == pro_phone
            and response.get("status") == "selected"
            for response in responses
        )
        if (
            job.get("state") != "assigned"
            or not pro_phone
            or selected != pro_phone
            or already
        ):
            await _already_updated(phone, tap.job_id)
            return True
        await _finalize_assignment(
            phone, tap.job_id, pro_phone, share_notes=tap.action == "share_yes"
        )
        return True

    if tap.action == "new_issue_no":
        await conversation_state_store.clear(phone)
        await whatsapp_service.send_text_message(
            phone, "Keeping your current request. I'll update you as pros reply."
        )
        return True

    if tap.action == "new_issue_yes":
        if str(ctx.get("active_job_id") or "") != tap.job_id or "diagnosis" not in ctx:
            await _already_updated(phone, tap.job_id)
            return True
        await homeowner_store.update(phone, active_job_id=None)
        diagnosis = Diagnosis.model_validate(ctx["diagnosis"])
        await _start_consent_flow(phone, diagnosis, ctx.get("photo_url"))
        return True

    return False


async def _relay_quote_question(phone: str, text: str, ctx: dict[str, Any]) -> None:
    job_id = str(ctx.get("active_job_id") or ctx.get("job_id") or "")
    pro_phone = normalize_phone(str(ctx.get("pro_phone") or ""))
    if not text:
        await whatsapp_service.send_text_message(phone, "Send the question as a text.")
        return
    if not job_id or not pro_phone:
        await conversation_state_store.clear(phone)
        await _already_updated(phone, job_id)
        return
    try:
        await whatsapp_service.send_text_message(
            pro_phone,
            f"Question from the homeowner about {job_ref(job_id)}:\n{text[:500]}",
        )
    except Exception as e:
        logger.warning("quote question relay failed: %s", e)
    await conversation_state_store.clear(phone)
    await whatsapp_service.send_text_message(
        phone, f"Sent. I passed that to the pro for {job_ref(job_id)}."
    )


async def handle_homeowner_typed(phone: str, body: str) -> bool:
    """Book / Yes / Pass with no locked button. Do not guess among several quotes."""
    choice = typed_choice(body)
    if choice not in {"book", "yes", "pass"}:
        return False
    phone_n = normalize_phone(phone)
    quotes = await _open_quotes(phone_n)
    if choice == "pass":
        if not quotes:
            return False
        if len(quotes) > 1:
            await whatsapp_service.send_text_message(phone_n, quote_choice_message(quotes))
            return True
        quote = quotes[0]
        await job_store.upsert_response(
            quote["job_id"], quote["pro_phone"], status="homeowner_passed"
        )
        await whatsapp_service.send_text_message(
            phone_n, f"Passed on *{quote['name']}* for {job_ref(quote['job_id'])}."
        )
        return True
    if quotes:
        if len(quotes) > 1:
            await whatsapp_service.send_text_message(phone_n, quote_choice_message(quotes))
            return True
        job = await job_store.get_job(quotes[0]["job_id"])
        if job:
            await _choose_pro(phone_n, job, quotes[0]["pro_phone"])
        return True
    if choice != "yes":
        return False
    pending = await job_store.list_for_homeowner(phone_n, {"awaiting_consent"})
    if not pending:
        return False
    if len(pending) > 1:
        refs = ", ".join(job_ref(job["job_id"]) for job in pending[:5])
        await whatsapp_service.send_text_message(
            phone_n,
            f"You have {len(pending)} requests waiting ({refs}). "
            "Tap Yes, find pros on the message for the one you want.",
        )
        return True
    job = pending[0]
    diagnosis = Diagnosis.model_validate(job.get("diagnosis") or {})
    await dispatch_job(
        phone_n,
        {
            "diagnosis": diagnosis.model_dump(mode="json"),
            "photo_url": job.get("photo_url"),
            "area_zip": job.get("area_zip"),
            "job_id": job["job_id"],
            "active_job_id": job["job_id"],
        },
    )
    return True


async def handle_homeowner_state(
    phone: str, body: str, interaction_payload: str, state: dict[str, Any]
) -> bool:
    """Handle awaiting_area / consent / pick / share notes. Returns True if consumed."""
    mode = state.get("mode") or MODE_IDLE
    ctx = dict(state.get("context") or {})
    phone_n = normalize_phone(phone)
    text = (body or "").strip()
    low = text.lower()
    payload = (interaction_payload or "").strip()
    tap = parse_homeowner_payload(payload)
    if tap and await _apply_homeowner_tap(phone_n, tap, ctx):
        return True

    if mode == MODE_AWAITING_QUOTE_QUESTION:
        await _relay_quote_question(phone_n, text, ctx)
        return True

    if mode == MODE_AWAITING_AREA:
        zips = re.findall(r"\b\d{5}\b", text)
        if not zips:
            await whatsapp_service.send_text_message(
                phone_n, "Please send a 5-digit ZIP code (e.g. 77002)."
            )
            return True
        await homeowner_store.update(phone_n, area_zip=zips[0])
        diag_data = ctx.get("diagnosis")
        if not diag_data:
            await conversation_state_store.clear(phone_n)
            await whatsapp_service.send_text_message(
                phone_n, "Got your ZIP. Send a photo when you're ready."
            )
            return True
        diagnosis = Diagnosis.model_validate(diag_data)
        await _ask_dispatch_consent(
            phone_n, diagnosis, ctx.get("photo_url"), zips[0]
        )
        return True

    if mode == "awaiting_new_issue_confirm":
        if low in {"yes", "y", "yeah"}:
            await homeowner_store.update(phone_n, active_job_id=None)
            diag = Diagnosis.model_validate(ctx["diagnosis"])
            await _start_consent_flow(phone_n, diag, ctx.get("photo_url"))
        else:
            await conversation_state_store.clear(phone_n)
            await whatsapp_service.send_text_message(
                phone_n, "Keeping your current request. I'll update you as pros reply."
            )
        return True

    if mode == MODE_AWAITING_DISPATCH_CONSENT:
        # DIY follow-ups
        if ctx.get("diy_pending"):
            if "fixed" in low or low in {"that fixed it", "fixed it"}:
                await conversation_state_store.clear(phone_n)
                await whatsapp_service.send_text_message(
                    phone_n, "Glad that helped! Message anytime if something else comes up."
                )
                if settings.feedback_enabled:
                    from app.services.feedback import store_feedback

                    await store_feedback(
                        phone=phone_n,
                        role="homeowner",
                        kind="diy_helpful",
                        rating=5,
                        text="That fixed it",
                        job_id=None,
                    )
                return True
            if "didn't" in low or "didnt" in low or "get a pro" in low or "pro" in low:
                ctx.pop("diy_pending", None)
                diag = Diagnosis.model_validate(ctx["diagnosis"])
                # Force pro path
                diag = diag.model_copy(update={"diy_appropriate": False, "diy_steps": []})
                await _start_consent_flow(phone_n, diag, ctx.get("photo_url"))
                return True

        yes = (
            low in {"yes", "y", "yeah", "sure"}
            or "find pros" in low
            or payload.startswith("consent:yes")
        )
        no = low in {"no", "n", "no thanks"} or payload.startswith("consent:no")
        if no:
            await conversation_state_store.clear(phone_n)
            await whatsapp_service.send_text_message(
                phone_n, "No problem. Send another photo anytime if you change your mind."
            )
            return True
        if yes:
            await dispatch_job(phone_n, ctx)
            return True
        await whatsapp_service.send_text_message(
            phone_n, "Reply *Yes, find pros* or *No thanks*."
        )
        return True

    if payload == "show_options" or payload.startswith("show_options:") or mode == MODE_AWAITING_SHOW_OPTIONS:
        if payload == "show_options" or payload.startswith("show_options:") or low in {"show options", "options"}:
            job_id = ctx.get("job_id")
            if job_id:
                await maybe_send_homeowner_options(job_id, force=True)
            else:
                await whatsapp_service.send_text_message(
                    phone_n,
                    "Those options expired. Send a photo to start a new request.",
                )
            return True
        await whatsapp_service.send_text_message(
            phone_n, "Tap *Show options* to see the pros who replied."
        )
        return True

    if mode == MODE_AWAITING_PICK:
        job_id = ctx.get("job_id")
        if not job_id:
            await conversation_state_store.clear(phone_n)
            return True
        await handle_pick(phone_n, job_id, text, payload)
        return True

    if mode == MODE_AWAITING_SHARE_NOTES:
        job_id = ctx.get("job_id")
        pro_phone = ctx.get("pro_phone")
        share = low in {"yes", "y", "yeah"} or payload.startswith("share:yes")
        await _finalize_assignment(phone_n, job_id, pro_phone, share_notes=share)
        return True

    return False


async def dispatch_job(phone: str, ctx: dict[str, Any]) -> None:
    phone_n = normalize_phone(phone)
    diag = Diagnosis.model_validate(ctx["diagnosis"])
    ho = await homeowner_store.ensure(phone_n)
    area_zip = ctx.get("area_zip") or ho.get("area_zip") or ""
    trade = TRADE_TO_PRO_CATEGORY.get(diag.trade, diag.trade.value)

    job_id = str(ctx.get("job_id") or ctx.get("active_job_id") or "")
    job = await job_store.get_job(job_id) if job_id else None
    if not job:
        job = await job_store.create_job(
            homeowner_phone=phone_n,
            diagnosis=diag,
            area_zip=area_zip,
            photo_url=ctx.get("photo_url"),
            state="diagnosed",
        )
        job_id = job["job_id"]
        await job_store.set_state(job_id, "awaiting_consent")
    elif job.get("state") == "diagnosed":
        await job_store.set_state(job_id, "awaiting_consent")
    elif job.get("state") != "awaiting_consent":
        await _already_updated(phone_n, job_id)
        return
    await homeowner_store.update(phone_n, active_job_id=job_id)

    pros = await select_pros_for_job(
        trade=trade, area_zip=area_zip, severity=diag.severity.value
    )
    if not pros:
        await job_store.set_state(job_id, "cancelled")
        await homeowner_store.update(phone_n, active_job_id=None)
        await conversation_state_store.clear(phone_n)
        await whatsapp_service.send_text_message(
            phone_n,
            "I couldn't find opted-in pros for that trade/ZIP yet. "
            "We're still onboarding local pros — try again soon.",
        )
        await log_event(
            phone=phone_n,
            role="system",
            type="dispatch_no_matches",
            job_id=job_id,
            payload={"trade": trade, "zip": area_zip},
        )
        return

    await job_store.set_state(job_id, "dispatched")

    issue = diag.pro_brief.issue
    alerted: list[str] = []
    for p in pros:
        sent = await _send_job_alert(
            p, job_id, trade, issue, diag.severity.value, area_zip
        )
        if not sent:
            continue
        await job_store.upsert_response(job_id, p["phone"], status="alerted")
        await pro_store.push_pending_alert(p["phone"], job_id)
        alerted.append(normalize_phone(p["phone"]))

    await job_store.update_fields(job_id, alerted_pro_phones=alerted)
    if not alerted:
        await job_store.set_state(job_id, "cancelled")
        await homeowner_store.update(phone_n, active_job_id=None)
        await conversation_state_store.clear(phone_n)
        await whatsapp_service.send_text_message(
            phone_n,
            "I couldn't deliver the job alerts just now. "
            "Please try again in a little while.",
        )
        return

    await whatsapp_service.send_text_message(
        phone_n,
        f"Sent to *{len(alerted)}* pros. I'll message you as responses come in "
        "(usually within an hour).",
    )
    await conversation_state_store.clear(phone_n)
    await log_event(
        phone=phone_n,
        role="system",
        type="job_dispatched",
        job_id=job_id,
        payload={"pro_count": len(alerted), "trade": trade},
    )

    # Schedule collection window via Cloud Tasks if configured
    try:
        from app.services.task_queue import enqueue_collection_window

        await enqueue_collection_window(job_id)
    except Exception as e:
        logger.warning("Could not schedule collection window: %s", e)


async def _send_job_alert(
    pro: dict[str, Any],
    job_id: str,
    trade: str,
    issue: str,
    severity: str,
    area_zip: str,
) -> bool:
    to = to_e164(pro["phone"])
    trade_v = clean_template_value(trade, "repair")
    zip_v = clean_template_value(area_zip, "your area", limit=32)
    issue_v = clean_template_value(issue, "home repair", limit=200)
    severity_v = clean_template_value(severity, "medium", limit=32)
    ref = clean_template_value(job_id, "this job", limit=32)
    body = (
        f"New *{trade_v}* request near {zip_v}: {issue_v}\n"
        f"Severity: {severity_v}\n\n"
        f"Tap a button on this message, or reply *Interested {job_ref(ref)}* "
        f"or *Not available {job_ref(ref)}*."
    )
    variables = {
        "1": trade_v,
        "2": zip_v,
        "3": issue_v,
        "4": severity_v,
        "5": ref,
    }
    sid = (settings.tpl_job_alert_sid or "").strip()
    try:
        if sid:
            try:
                await whatsapp_service.send_content_template(
                    to, content_sid=sid, content_variables=variables
                )
                return True
            except Exception as e:
                logger.warning("job alert template failed, trying text: %s", e)
        await whatsapp_service.send_text_message(to, body)
        return True
    except Exception as e:
        logger.warning("job alert send failed: %s", e)
        await log_event(
            phone=to,
            role="system",
            type="error",
            job_id=job_id,
            payload={"error": "job_alert_send_failed", "detail": str(e)[:300]},
        )
        return False


def _pro_already_answered(responses: list[dict[str, Any]], phone: str) -> bool:
    phone_n = normalize_phone(phone)
    for response in responses:
        if normalize_phone(str(response.get("pro_phone") or "")) != phone_n:
            continue
        status = str(response.get("status") or "")
        return status not in {"", "alerted"}
    return False


async def handle_pro_alert_response(phone: str, body: str, payload: str) -> bool:
    """Interested / Not available for one specific job. Returns True if handled."""
    phone_n = normalize_phone(phone)
    open_ids = await pro_store.pending_alert_ids(phone_n)
    decision = resolve_alert_reply(payload, body, open_ids)
    if not decision.handled:
        return False
    if decision.clarify:
        await whatsapp_service.send_text_message(phone_n, decision.clarify)
        return True

    job_id = decision.job_id or ""
    ref = job_ref(job_id)
    job = await job_store.get_job(job_id) if job_id else None
    responses = await job_store.list_responses(job_id) if job else []
    open_state = bool(job) and job.get("state") in {"dispatched", "awaiting_pick"}
    if not open_state or _pro_already_answered(responses, phone_n):
        await whatsapp_service.send_text_message(
            phone_n,
            f"This job ({ref}) has already been updated or filled.",
        )
        await pro_store.remove_pending_alert(phone_n, job_id)
        return True

    await pro_store.remove_pending_alert(phone_n, job_id)
    if decision.status == "declined":
        await job_store.upsert_response(job_id, phone_n, status="declined")
        await whatsapp_service.send_text_message(
            phone_n, "Thanks for letting us know."
        )
        await log_event(
            phone=phone_n, role="pro", type="pro_declined", job_id=job_id, payload={}
        )
        return True

    await job_store.upsert_response(job_id, phone_n, status="interested")
    await conversation_state_store.set(
        phone_n,
        MODE_PRO_AWAITING_DETAILS,
        {"job_id": job_id, "active_job_id": job_id},
    )
    await whatsapp_service.send_text_message(
        phone_n,
        "Great! When can you come, and what's a rough estimate or visit fee?",
    )
    await log_event(
        phone=phone_n, role="pro", type="pro_interested", job_id=job_id, payload={}
    )
    return True


async def handle_pro_details(phone: str, body: str, state: dict[str, Any]) -> bool:
    if state.get("mode") != MODE_PRO_AWAITING_DETAILS:
        return False
    phone_n = normalize_phone(phone)
    ctx = state.get("context") or {}
    job_id = ctx.get("active_job_id") or ctx.get("job_id")
    if not job_id:
        await conversation_state_store.clear(phone_n)
        return True

    from app.services.gemini import gemini_service

    try:
        details = await gemini_service.extract_pro_details(body)
    except Exception:
        details = {
            "availability_text": body[:200],
            "estimate_text": body[:200],
            "estimate_low": None,
            "estimate_high": None,
        }

    from datetime import datetime, timezone

    await job_store.upsert_response(
        job_id,
        phone_n,
        status="details_given",
        availability_text=details.get("availability_text") or body[:200],
        estimate_text=details.get("estimate_text") or "",
        estimate_low=details.get("estimate_low"),
        estimate_high=details.get("estimate_high"),
        responded_at=datetime.now(timezone.utc).isoformat(),
    )
    await conversation_state_store.clear(phone_n)
    await whatsapp_service.send_text_message(
        phone_n,
        "Got it — I'll share this with the homeowner:\n"
        f"Availability: {details.get('availability_text')}\n"
        f"Estimate: {details.get('estimate_text')}",
    )
    await maybe_send_homeowner_options(job_id)
    return True


async def maybe_send_homeowner_options(job_id: str, force: bool = False) -> None:
    job = await job_store.get_job(job_id)
    if not job:
        return
    if job.get("state") not in {"dispatched", "awaiting_pick"}:
        return

    responses = await job_store.list_responses(job_id)
    complete = [
        r
        for r in responses
        if r.get("status") == "details_given" and r.get("availability_text")
    ]
    # Spec: first 3 complete responses, or collection window force with ≥1
    if len(complete) < 1:
        return
    if not force and len(complete) < 3:
        return

    if job.get("state") == "dispatched":
        await job_store.set_state(job_id, "awaiting_pick")

    # Build options message
    lines = ["*Here are your options:*\n"]
    option_phones: list[str] = []
    for i, r in enumerate(complete[:5], 1):
        pro = await pro_store.get(r["pro_phone"])
        name = (pro or {}).get("business_name") or (pro or {}).get("name") or "Pro"
        rating = (pro or {}).get("rating_avg") or "—"
        fee = fee_label((pro or {}).get("dispatch_fee_tier"))
        fee_line = f"   Service call: {fee}\n" if fee else ""
        lines.append(
            f"*{i}. {name}* (★{rating})\n"
            f"{fee_line}"
            f"   When: {r.get('availability_text')}\n"
            f"   Est: {r.get('estimate_text') or '—'}\n"
        )
        option_phones.append(normalize_phone(r["pro_phone"]))

    lines.append("Tap *Book This Pro* on the quote you want.")
    homeowner = job["homeowner_phone"]
    body = "\n".join(lines)
    context = {
        "job_id": job_id,
        "active_job_id": job_id,
        "option_phones": option_phones,
    }
    sid = settings.tpl_job_reopen_sid
    variables = {
        "1": clean_template_value(len(option_phones), "1", limit=8),
        "2": clean_template_value(job.get("trade"), "repair", limit=40),
        "3": clean_template_value(job_id, "job", limit=32),
    }
    last = await get_last_inbound(homeowner)
    use_template = (
        choose_delivery(window_open=window_is_open(last), template_sid=sid)
        == "template"
    )
    if not use_template:
        try:
            await whatsapp_service.send_text_message(
                homeowner,
                f"Here are your options for {job_ref(job_id)}. "
                "Tap a button on the quote you want.",
            )
            for response, pro_phone in zip(complete[:5], option_phones):
                pro = await pro_store.get(pro_phone)
                name = (pro or {}).get("business_name") or (pro or {}).get("name") or "Pro"
                quote_hash = phone_hash(pro_phone)
                await _send_choice(
                    homeowner,
                    settings.tpl_quote_choice_sid,
                    {
                        "1": clean_template_value(name, "Pro", limit=40),
                        "2": clean_template_value(response.get("availability_text"), "soon", limit=80),
                        "3": clean_template_value(response.get("estimate_text"), "see message", limit=80),
                        "4": job_id,
                        "5": quote_hash,
                    },
                    (
                        f"*{name}* for {job_ref(job_id)}\n"
                        f"When: {response.get('availability_text')}\n"
                        f"Est: {response.get('estimate_text') or '—'}\n"
                        "Tap Book This Pro, Pass, or Ask Question."
                    ),
                )
            await conversation_state_store.set(
                homeowner, MODE_AWAITING_PICK, context
            )
            await log_event(
                phone=homeowner,
                role="system",
                type="options_sent",
                job_id=job_id,
                payload={"count": len(option_phones)},
            )
            return
        except Exception as e:
            if not (
                isinstance(e, httpx.HTTPStatusError)
                and should_retry_with_template(twilio_error_code(e), sid)
            ):
                logger.warning("options send failed: %s", e)
                raise
            use_template = True

    try:
        await whatsapp_service.send_content_template(
            homeowner, content_sid=sid.strip(), content_variables=variables
        )
    except Exception as e:
        logger.warning("reopen template failed: %s", e)
        await log_event(
            phone=homeowner,
            role="system",
            type="error",
            job_id=job_id,
            payload={"error": "job_reopen_send_failed", "detail": str(e)[:300]},
        )
        return
    await conversation_state_store.set(
        homeowner, MODE_AWAITING_SHOW_OPTIONS, context
    )
    await log_event(
        phone=homeowner,
        role="system",
        type="options_reopen",
        job_id=job_id,
        payload={"count": len(option_phones)},
    )


async def handle_pick(
    phone: str, job_id: str, text: str, payload: str
) -> None:
    phone_n = normalize_phone(phone)
    job = await job_store.get_job(job_id)
    if not job or job.get("state") != "awaiting_pick":
        await whatsapp_service.send_text_message(
            phone_n, "That request is no longer open for picking."
        )
        await conversation_state_store.clear(phone_n)
        return
    if normalize_phone(job["homeowner_phone"]) != phone_n:
        return

    state = await conversation_state_store.get(phone_n)
    option_phones: list[str] = list(
        (state.get("context") or {}).get("option_phones")
        or job.get("alerted_pro_phones")
        or []
    )
    if not payload:
        open_jobs = await job_store.list_for_homeowner(phone_n, {"awaiting_pick"})
        if len(open_jobs) > 1:
            await whatsapp_service.send_text_message(
                phone_n, quote_choice_message(await _open_quotes(phone_n))
            )
            return

    chosen: str | None = None
    if payload.startswith("pick:"):
        chosen = resolve_pro_from_pick_payload(payload, option_phones)
    else:
        low = text.strip().lower()
        if typed_choice(text) == "book" and len(option_phones) == 1:
            chosen = option_phones[0]
        elif low.isdigit():
            idx = int(low) - 1
            if 0 <= idx < len(option_phones):
                chosen = option_phones[idx]
        else:
            for p in option_phones:
                pro = await pro_store.get(p)
                name = (
                    (pro or {}).get("business_name")
                    or (pro or {}).get("name")
                    or ""
                ).lower()
                if low and low in name:
                    chosen = p
                    break

    if not chosen:
        await whatsapp_service.send_text_message(
            phone_n, "I couldn't match that choice. Reply with 1, 2, or 3."
        )
        return

    await _choose_pro(phone_n, job, chosen)


async def _choose_pro(phone: str, job: dict[str, Any], pro_phone: str) -> None:
    phone_n = normalize_phone(phone)
    pro_n = normalize_phone(pro_phone)
    job_id = job["job_id"]
    notes = await homeowner_store.list_service_notes(phone_n)
    await job_store.set_state(job_id, "assigned", selected_pro_phone=pro_n)
    if notes:
        quote_hash = phone_hash(pro_n)
        pro = await pro_store.get(pro_n)
        pname = (pro or {}).get("business_name") or (pro or {}).get("name") or "the pro"
        await conversation_state_store.set(
            phone_n,
            MODE_AWAITING_SHARE_NOTES,
            {
                "job_id": job_id,
                "active_job_id": job_id,
                "pro_phone": pro_n,
                "active_quote_id": quote_hash,
            },
        )
        await _send_choice(
            phone_n,
            settings.tpl_share_notes_sid,
            {
                "1": clean_template_value(pname, "the pro", limit=40),
                "2": job_id,
                "3": quote_hash,
            },
            f"Share your saved access notes with {pname} for {job_ref(job_id)}?\nReply *Yes* or *No*.",
        )
        return
    await _finalize_assignment(phone_n, job_id, pro_n, share_notes=False)


async def _finalize_assignment(
    homeowner_phone: str,
    job_id: str,
    pro_phone: str,
    *,
    share_notes: bool,
) -> None:
    phone_n = normalize_phone(homeowner_phone)
    pro_n = normalize_phone(pro_phone)
    job = await job_store.get_job(job_id)
    if not job:
        return

    pro = await pro_store.get(pro_n)
    pname = (pro or {}).get("business_name") or (pro or {}).get("name") or "Your pro"
    pro_number = to_e164(pro_n)

    await whatsapp_service.send_text_message(
        phone_n,
        f"You're connected with *{pname}*.\nPhone: {pro_number}\n"
        "They also have your number — reach out to finalize timing.",
    )

    notes_text = ""
    if share_notes:
        notes = await homeowner_store.list_service_notes(phone_n)
        if notes:
            notes_text = "\n\nAccess notes from homeowner:\n" + "\n".join(
                f"- {n}" for n in notes
            )

    issue = (job.get("diagnosis") or {}).get("pro_brief", {}).get("issue") or job.get(
        "trade"
    )
    try:
        await whatsapp_service.send_text_message(
            pro_n,
            f"You were selected for a *{job.get('trade')}* job near {job.get('area_zip')}.\n"
            f"Issue: {issue}\n"
            f"Homeowner phone: {to_e164(phone_n)}"
            f"{notes_text}",
        )
    except Exception as e:
        # No template for this message: it includes a phone number.
        # 63016 is already logged by send_text_message.
        logger.warning("selected-pro notice failed: %s", e)

    await job_store.upsert_response(job_id, pro_n, status="selected")
    for other in job.get("alerted_pro_phones") or []:
        other_n = normalize_phone(other)
        if other_n == pro_n:
            continue
        resp_list = await job_store.list_responses(job_id)
        interested = {
            normalize_phone(r.get("pro_phone", ""))
            for r in resp_list
            if r.get("status") in {"interested", "details_given"}
        }
        if other_n not in interested:
            continue
        await job_store.upsert_response(job_id, other_n, status="not_selected")
        await _send_not_selected(other_n, job)

    await conversation_state_store.clear(phone_n)
    await homeowner_store.update(phone_n, active_job_id=job_id)
    await log_event(
        phone=phone_n,
        role="homeowner",
        type="job_assigned",
        job_id=job_id,
        payload={"pro": pro_n},
    )


async def _send_not_selected(pro_phone: str, job: dict[str, Any]) -> None:
    to = to_e164(pro_phone)
    trade = clean_template_value(job.get("trade"), "repair", limit=40)
    area = clean_template_value(job.get("area_zip"), "your area", limit=32)
    try:
        await whatsapp_service.send_business_message(
            to,
            body=(
                f"Thanks for responding to the {trade} request near {area}. "
                "The homeowner chose another pro for this job."
            ),
            template_sid=settings.tpl_job_not_selected_sid,
            content_variables={"1": trade, "2": area},
        )
    except Exception as e:
        logger.warning("not_selected send failed: %s", e)
        await log_event(
            phone=to,
            role="system",
            type="error",
            job_id=job.get("job_id"),
            payload={"error": "not_selected_send_failed", "detail": str(e)[:300]},
        )


async def on_collection_window_closed(job_id: str) -> None:
    """Cloud Tasks / timer callback."""
    job = await job_store.get_job(job_id)
    if not job or job.get("state") != "dispatched":
        return
    complete = await job_store.count_complete_responses(job_id)
    if complete >= 1:
        await maybe_send_homeowner_options(job_id, force=True)
    else:
        await job_store.set_state(job_id, "expired")
        await whatsapp_service.send_text_message(
            job["homeowner_phone"],
            "No pros were available yet for this request. "
            "Reply *retry* later or send another photo to try again.",
        )
        await homeowner_store.update(
            job["homeowner_phone"], active_job_id=None
        )
