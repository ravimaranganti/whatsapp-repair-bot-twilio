"""Phase 4 — brokered job dispatch, pro responses, homeowner pick."""

from __future__ import annotations

import logging
import re
from typing import Any

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
from app.services.homeowner_followups import (
    next_pending_quote,
    no_reply_body,
    passed_only_body,
    quote_is_pending,
    quote_reminder_body,
)
from app.services.homeowners import homeowner_store
from app.services.jobs import job_store, resolve_pro_from_pick_payload
from app.services.alert_reply import job_ref, resolve_alert_reply
from app.services.matching import select_pros_for_job
from app.services.pro_profile import pro_fit_line
from app.services.pro_reply import (
    QUOTE_READY,
    QUOTE_WITH_QUESTION,
    QUESTION_ONLY,
    button_first_name,
    card_extra,
    estimate_bounds,
    experience_sentence,
    parse_pro_job_reply,
    quote_card_text,
)
from app.services.pros import pro_store
from app.services.session_window import (
    clean_template_value,
    get_last_inbound,
    window_is_open,
)
from app.services.whatsapp import whatsapp_service
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
    """DIY steps, or ask for a ZIP and permission before any pro is contacted."""
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

    if tap.action == "job_outcome":
        await _apply_job_outcome(phone, job, tap.outcome)
        return True

    if tap.action == "reopen_job":
        await _reopen_fell_through(phone, job)
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

    if tap.action in {"keep_looking", "close_job", "retry_pros"}:
        if tap.action == "close_job" and job.get("state") == "assigned":
            await _close_homeowner_job(phone, job)
            return True
        if job.get("state") not in {"dispatched", "awaiting_pick"}:
            await _already_updated(phone, tap.job_id)
            return True
        if tap.action == "close_job":
            await _close_homeowner_job(phone, job)
            return True
        if tap.action == "keep_looking":
            await whatsapp_service.send_text_message(
                phone,
                f"I'll keep {job_ref(tap.job_id)} open and message you if another pro replies.",
            )
            return True
        asked = await reask_pros(job)
        await whatsapp_service.send_text_message(
            phone,
            f"Asked again for {job_ref(tap.job_id)}. "
            f"I pinged {asked} pros and I'll message you when someone replies.",
        )
        return True

    if tap.action == "reply_pro_q":
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
        open_reply = (
            job.get("state") in {"dispatched", "awaiting_pick"}
            and mine
            and mine.get("status") in {"interested", "details_given"}
        )
        if not pro_phone or not open_reply:
            await _already_updated(phone, tap.job_id)
            return True
        await _ask_homeowner_to_reply(phone, job, pro_phone, mine)
        return True

    if tap.action in {"book_quote", "pass_quote", "ask_quote"}:
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
        if tap.action == "pass_quote":
            await _after_pass(phone, job, pro_phone)
            return True
        if tap.action == "ask_quote":
            await _ask_homeowner_to_reply(phone, job, pro_phone, mine)
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


async def _ask_homeowner_to_reply(
    phone: str, job: dict[str, Any], pro_phone: str, response: dict[str, Any] | None
) -> None:
    """Lock the homeowner's next text to this pro, and offer a reply button."""
    job_id = str(job.get("job_id") or "")
    pro = await pro_store.get(pro_phone)
    name = (pro or {}).get("business_name") or (pro or {}).get("name") or "that pro"
    question = str((response or {}).get("pro_question") or "")
    await conversation_state_store.set(
        phone,
        MODE_AWAITING_QUOTE_QUESTION,
        {
            "job_id": job_id,
            "active_job_id": job_id,
            "pro_phone": pro_phone,
            "active_quote_id": phone_hash(pro_phone),
        },
    )
    if question:
        prompt = (
            f"*{name}* asked about {job_ref(job_id)}: {question}\n\n"
            "Type your reply and I'll send it."
        )
    else:
        prompt = f"What do you want to ask *{name}* about {job_ref(job_id)}?"
    trade = clean_template_value(job.get("trade"), "repair", limit=32)
    await _send_choice(
        phone,
        settings.tpl_pro_question_sid if question else "",
        {
            "1": clean_template_value(name, "Pro", limit=40),
            "2": trade,
            "3": job_id,
            "4": clean_template_value(question, "a question", limit=200),
            "5": phone_hash(pro_phone),
            "6": clean_template_value(button_first_name(name), "Pro", limit=10),
        },
        prompt,
    )


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
            f"The homeowner replied about {job_ref(job_id)}:\n{text[:500]}\n\n"
            "Reply here with when you can come and a rough estimate.",
        )
    except Exception as e:
        logger.warning("quote question relay failed: %s", e)
    pro_state = await conversation_state_store.get(pro_phone)
    pro_ctx = dict(pro_state.get("context") or {})
    if pro_state.get("mode") != MODE_PRO_AWAITING_DETAILS or str(
        pro_ctx.get("active_job_id") or ""
    ) != job_id:
        pro_ctx.update({"job_id": job_id, "active_job_id": job_id})
        await conversation_state_store.set(pro_phone, MODE_PRO_AWAITING_DETAILS, pro_ctx)
    await conversation_state_store.clear(phone)
    await whatsapp_service.send_text_message(
        phone, f"Sent. I passed that to the pro for {job_ref(job_id)}."
    )


async def handle_homeowner_typed(phone: str, body: str) -> bool:
    """Book / Yes / Pass with no locked button. Do not guess among several quotes."""
    phrase = " ".join((body or "").lower().split())
    if phrase in {"job completed", "still scheduled", "didn't work out", "didnt work out"}:
        phone_n = normalize_phone(phone)
        jobs = await job_store.list_for_homeowner(phone_n, {"assigned"})
        waiting = [job for job in jobs if job.get("outcome_status") == "PENDING_CHECKIN"]
        if len(waiting) != 1:
            return False
        outcome = {
            "job completed": "COMPLETED",
            "still scheduled": "SCHEDULED",
            "didn't work out": "FELL_THROUGH",
            "didnt work out": "FELL_THROUGH",
        }[phrase]
        await _apply_job_outcome(phone_n, waiting[0], outcome)
        return True
    if phrase in {"find another pro", "close request"}:
        phone_n = normalize_phone(phone)
        jobs = await job_store.list_for_homeowner(phone_n, {"assigned"})
        fallen = [job for job in jobs if job.get("outcome_status") == "FELL_THROUGH"]
        if len(fallen) != 1:
            return False
        if phrase == "find another pro":
            await _reopen_fell_through(phone_n, fallen[0])
        else:
            await _close_homeowner_job(phone_n, fallen[0])
        return True
    if phrase in {"keep looking", "ask again", "leave it", "cancel request"}:
        phone_n = normalize_phone(phone)
        jobs = await job_store.list_for_homeowner(phone_n, {"dispatched", "awaiting_pick"})
        if len(jobs) != 1:
            return False
        job = jobs[0]
        if phrase == "keep looking":
            await whatsapp_service.send_text_message(
                phone_n,
                f"I'll keep {job_ref(job['job_id'])} open and message you if another pro replies.",
            )
            return True
        if phrase == "ask again":
            asked = await reask_pros(job)
            await whatsapp_service.send_text_message(
                phone_n,
                f"Asked again for {job_ref(job['job_id'])}. I pinged {asked} pros.",
            )
            return True
        await _close_homeowner_job(phone_n, job)
        return True
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
        job = await job_store.get_job(quote["job_id"])
        if job:
            await _after_pass(phone_n, job, quote["pro_phone"])
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

    if mode == "awaiting_job_feedback":
        await _capture_job_feedback(phone_n, text, ctx)
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

    if not settings.dispatch_enabled:
        await job_store.set_state(job_id, "cancelled")
        await homeowner_store.update(phone_n, active_job_id=None)
        await conversation_state_store.clear(phone_n)
        await whatsapp_service.send_text_message(
            phone_n,
            "I won't share your number or text a list of pros. "
            "Matching real local pros from the bot is off right now.",
        )
        return

    pros = await select_pros_for_job(
        trade=trade,
        area_zip=area_zip,
        severity=diag.severity.value,
        issue=diag.pro_brief.issue or "",
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
        f"Sent to *{len(alerted)}* pros. I'll message you as each one replies.",
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
    link_line = ""
    try:
        from app.services.portal_auth import LINK_TEXT, issue_magic_link

        dashboard_url = await issue_magic_link(pro["phone"])
        if dashboard_url:
            link_line = f"{LINK_TEXT} {dashboard_url}"
    except Exception as e:
        logger.warning("dashboard link on alert skipped: %s", e)
    body = (
        f"New *{trade_v}* request near {zip_v}: {issue_v}\n"
        f"Severity: {severity_v}\n\n"
        f"Tap a button on this message, or reply *Interested {job_ref(ref)}* "
        f"or *Not available {job_ref(ref)}*."
    )
    if link_line:
        body = f"{body}\n\n{link_line}"
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
                if link_line and window_is_open(await get_last_inbound(pro["phone"])):
                    try:
                        await whatsapp_service.send_text_message(to, link_line)
                    except Exception as e:
                        logger.warning("dashboard link follow-up failed: %s", e)
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
        "Got it! Reply with when you can come and a rough estimate "
        "(or type a quick question if you need more info from the homeowner first).",
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

    from datetime import datetime, timezone

    job = await job_store.get_job(str(job_id))
    if not job or job.get("state") not in {"dispatched", "awaiting_pick"}:
        await conversation_state_store.clear(phone_n)
        await whatsapp_service.send_text_message(
            phone_n, f"This job ({job_ref(str(job_id))}) has already been updated or filled."
        )
        return True

    pro = await pro_store.get(phone_n) or {}
    draft = {
        "availability_text": ctx.get("draft_availability") or "",
        "price_estimate": ctx.get("draft_price") or "",
        "caveats_or_notes": ctx.get("draft_notes") or "",
        "question_for_homeowner": ctx.get("draft_question") or "",
    }
    reply = await parse_pro_job_reply(body, pro, job, draft)
    kept = {
        "job_id": job_id,
        "active_job_id": job_id,
        "draft_availability": reply.availability_text,
        "draft_price": reply.price_estimate,
        "draft_notes": reply.caveats_or_notes,
        "draft_question": reply.question_for_homeowner,
    }

    if reply.intent == QUESTION_ONLY:
        await job_store.upsert_response(
            job_id, phone_n, pro_question=reply.question_for_homeowner
        )
        await conversation_state_store.set(phone_n, MODE_PRO_AWAITING_DETAILS, kept)
        homeowner = str(job.get("homeowner_phone") or "")
        if homeowner:
            await _ask_homeowner_to_reply(
                homeowner,
                job,
                phone_n,
                {"pro_question": reply.question_for_homeowner},
            )
        await whatsapp_service.send_text_message(
            phone_n,
            "Got it. I've passed your question to the homeowner and will "
            "message you as soon as they reply.",
        )
        return True

    if reply.intent not in {QUOTE_READY, QUOTE_WITH_QUESTION}:
        await conversation_state_store.set(phone_n, MODE_PRO_AWAITING_DETAILS, kept)
        await whatsapp_service.send_text_message(phone_n, reply.missing_field_prompt)
        return True

    low, high = estimate_bounds(reply.price_estimate)
    await job_store.upsert_response(
        job_id,
        phone_n,
        status="details_given",
        availability_text=reply.availability_text,
        estimate_text=reply.price_estimate,
        estimate_low=low,
        estimate_high=high,
        caveat_text=reply.caveats_or_notes,
        pro_question=reply.question_for_homeowner,
        responded_at=datetime.now(timezone.utc).isoformat(),
    )
    await conversation_state_store.clear(phone_n)
    await whatsapp_service.send_text_message(
        phone_n,
        "Thanks! I've sent your quote to the homeowner and will let you know "
        "as soon as they respond.",
    )
    await maybe_send_homeowner_options(str(job_id))
    return True


async def _quote_label(response: dict[str, Any]) -> tuple[str, str, str]:
    pro_phone = normalize_phone(str(response.get("pro_phone") or ""))
    pro = await pro_store.get(pro_phone)
    name = (pro or {}).get("business_name") or (pro or {}).get("name") or "Pro"
    fit = pro_fit_line(pro)
    estimate = str(response.get("estimate_text") or "see message")
    if fit:
        estimate = f"{estimate}. {fit}"
    return pro_phone, name, estimate


async def _deliver_quote_cards(job: dict[str, Any], responses: list[dict[str, Any]]) -> None:
    """Send one Book / Pass / Ask card per quote. Each id includes the job and quote."""
    from datetime import datetime, timezone

    job_id = str(job.get("job_id") or "")
    homeowner = str(job.get("homeowner_phone") or "")
    if not job_id or not homeowner or not responses:
        return
    option_phones: list[str] = []
    trade = str(job.get("trade") or "repair")
    card_sid = (settings.tpl_quote_card_sid or "").strip()
    for response in responses:
        pro_phone = normalize_phone(str(response.get("pro_phone") or ""))
        if not pro_phone:
            continue
        pro = await pro_store.get(pro_phone)
        name = (pro or {}).get("business_name") or (pro or {}).get("name") or "Pro"
        availability = str(response.get("availability_text") or "soon")
        price = str(response.get("estimate_text") or "see message")
        notes = str(response.get("caveat_text") or "")
        question = str(response.get("pro_question") or "")
        quote_hash = phone_hash(pro_phone)
        body = quote_card_text(
            name=name,
            trade=trade,
            job_id=job_id,
            availability=availability,
            price=price,
            pro=pro,
            notes=notes,
            question=question,
        )
        extra = card_extra(notes, question, pro, name, price) or "Details are above."
        if card_sid:
            await _send_choice(
                homeowner,
                card_sid,
                {
                    "1": job_id,
                    "2": clean_template_value(trade, "repair", limit=32),
                    "3": clean_template_value(name, "Pro", limit=40),
                    "4": clean_template_value(
                        f"{experience_sentence(pro)} and are available {availability}.",
                        "They are a verified local pro.",
                        limit=160,
                    ),
                    "5": clean_template_value(price, "see message", limit=60),
                    "6": clean_template_value(extra, "Details are above.", limit=240),
                    "7": quote_hash,
                },
                body + "\n\nTap Book This Pro, Ask / Reply, or Pass.",
            )
        else:
            await _send_choice(
                homeowner,
                settings.tpl_quote_choice_sid,
                {
                    "1": clean_template_value(name, "Pro", limit=40),
                    "2": clean_template_value(availability, "soon", limit=80),
                    "3": clean_template_value(price, "see message", limit=80),
                    "4": job_id,
                    "5": quote_hash,
                },
                body + "\n\nTap Book This Pro, Ask / Reply, or Pass.",
            )
        await job_store.upsert_response(
            job_id,
            pro_phone,
            quote_sent_at=datetime.now(timezone.utc).isoformat(),
        )
        option_phones.append(pro_phone)
    if not option_phones:
        return
    pending = [
        normalize_phone(str(item.get("pro_phone") or ""))
        for item in await job_store.list_responses(job_id)
        if quote_is_pending(item)
    ]
    await conversation_state_store.set(
        homeowner,
        MODE_AWAITING_PICK,
        {
            "job_id": job_id,
            "active_job_id": job_id,
            "option_phones": pending or option_phones,
        },
    )
    await log_event(
        phone=homeowner,
        role="system",
        type="options_sent",
        job_id=job_id,
        payload={"count": len(option_phones)},
    )


async def maybe_send_homeowner_options(job_id: str, force: bool = False) -> None:
    """Send each new quote as soon as it has a time and a price."""
    job = await job_store.get_job(job_id)
    if not job or job.get("state") not in {"dispatched", "awaiting_pick"}:
        return
    responses = await job_store.list_responses(job_id)
    complete = [response for response in responses if quote_is_pending(response)]
    if not complete:
        return
    if job.get("state") == "dispatched":
        job = await job_store.set_state(job_id, "awaiting_pick")
    to_send = complete if force else [response for response in complete if not response.get("quote_sent_at")]
    if not to_send:
        return
    homeowner = str(job.get("homeowner_phone") or "")
    last = await get_last_inbound(homeowner)
    reopen = (settings.tpl_job_reopen_sid or "").strip()
    if not window_is_open(last) and reopen:
        try:
            await whatsapp_service.send_content_template(
                homeowner,
                content_sid=reopen,
                content_variables={
                    "1": clean_template_value(len(complete), "1", limit=8),
                    "2": clean_template_value(job.get("trade"), "repair", limit=40),
                    "3": clean_template_value(job_id, "job", limit=32),
                },
            )
        except Exception as e:
            logger.warning("reopen template failed: %s", e)
            return
        await conversation_state_store.set(
            homeowner,
            MODE_AWAITING_SHOW_OPTIONS,
            {
                "job_id": job_id,
                "active_job_id": job_id,
                "option_phones": [
                    normalize_phone(str(response.get("pro_phone") or "")) for response in complete
                ],
            },
        )
        return
    await _deliver_quote_cards(job, to_send)


async def _after_pass(phone: str, job: dict[str, Any], pro_phone: str) -> None:
    """Mark one quote passed, then show the next quote or offer to keep looking."""
    job_id = str(job.get("job_id") or "")
    await job_store.upsert_response(job_id, pro_phone, status="homeowner_passed")
    responses = await job_store.list_responses(job_id)
    nxt = next_pending_quote(responses, pro_phone)
    if nxt:
        fresh = await job_store.get_job(job_id) or job
        await _deliver_quote_cards(fresh, [nxt])
        return
    pro = await pro_store.get(pro_phone)
    name = (pro or {}).get("business_name") or (pro or {}).get("name") or "that pro"
    await _send_choice(
        phone,
        settings.tpl_keep_looking_sid,
        {
            "1": clean_template_value(name, "that pro", limit=40),
            "2": job_id,
        },
        passed_only_body(name) + "\n\nReply *Keep looking* or *Cancel request*.",
    )


async def _close_homeowner_job(phone: str, job: dict[str, Any]) -> None:
    job_id = str(job.get("job_id") or "")
    if job.get("state") not in {"dispatched", "awaiting_pick", "awaiting_consent", "assigned"}:
        await _already_updated(phone, job_id)
        return
    await job_store.set_state(job_id, "cancelled")
    if job.get("state") == "assigned":
        await job_store.update_fields(job_id, outcome_status="CLOSED")
    ho = await homeowner_store.get(phone)
    if (ho or {}).get("active_job_id") == job_id:
        await homeowner_store.update(phone, active_job_id=None)
    await conversation_state_store.clear(phone)
    await whatsapp_service.send_text_message(
        phone,
        f"No problem. I closed {job_ref(job_id)}. Send a photo anytime if you want to start again.",
    )


async def reask_pros(job: dict[str, Any]) -> int:
    """Ping matched pros again, including pros who were not alerted the first time."""
    from datetime import datetime, timezone

    job_id = str(job.get("job_id") or "")
    diagnosis = Diagnosis.model_validate(job.get("diagnosis") or {})
    trade = TRADE_TO_PRO_CATEGORY.get(diagnosis.trade, diagnosis.trade.value)
    area_zip = str(job.get("area_zip") or "")
    pros = await select_pros_for_job(
        trade=trade,
        area_zip=area_zip,
        severity=diagnosis.severity.value,
        issue=diagnosis.pro_brief.issue or "",
    )
    responses = {
        normalize_phone(str(response.get("pro_phone") or "")): response
        for response in await job_store.list_responses(job_id)
    }
    skip = {"details_given", "interested", "homeowner_passed", "selected", "not_selected", "declined"}
    alerted = [normalize_phone(str(item)) for item in (job.get("alerted_pro_phones") or [])]
    sent = 0
    issue = diagnosis.pro_brief.issue
    for pro in pros:
        pro_phone = normalize_phone(str(pro.get("phone") or ""))
        status = str((responses.get(pro_phone) or {}).get("status") or "")
        if not pro_phone or status in skip:
            continue
        ok = await _send_job_alert(
            pro, job_id, trade, issue, diagnosis.severity.value, area_zip
        )
        if not ok:
            continue
        await job_store.upsert_response(job_id, pro_phone, status="alerted")
        await pro_store.push_pending_alert(pro_phone, job_id)
        if pro_phone not in alerted:
            alerted.append(pro_phone)
        sent += 1
    now = datetime.now(timezone.utc).isoformat()
    await job_store.update_fields(
        job_id,
        alerted_pro_phones=alerted,
        no_reply_followup_sent=False,
        no_reply_anchor_at=now,
    )
    return sent


async def send_no_reply_followup(job: dict[str, Any]) -> None:
    job_id = str(job.get("job_id") or "")
    homeowner = str(job.get("homeowner_phone") or "")
    trade = clean_template_value(job.get("trade"), "repair", limit=40)
    await _send_choice(
        homeowner,
        settings.tpl_no_reply_followup_sid,
        {"1": trade, "2": job_id, "3": job_id},
        no_reply_body(trade, job_id) + "\n\nReply *Ask again* or *Leave it*.",
    )


async def send_quote_reminder(job: dict[str, Any], response: dict[str, Any]) -> None:
    job_id = str(job.get("job_id") or "")
    homeowner = str(job.get("homeowner_phone") or "")
    pro_phone, name, estimate = await _quote_label(response)
    trade = clean_template_value(job.get("trade"), "repair", limit=40)
    quote_hash = phone_hash(pro_phone)
    body = quote_reminder_body(name, estimate, trade, job_id)
    await _send_choice(
        homeowner,
        settings.tpl_quote_reminder_sid,
        {
            "1": clean_template_value(name, "Pro", limit=40),
            "2": clean_template_value(estimate, "see message", limit=80),
            "3": trade,
            "4": job_id,
            "5": job_id,
            "6": quote_hash,
        },
        body + "\n\nTap Book This Pro, Pass, or Ask Question.",
    )


async def _apply_job_outcome(phone: str, job: dict[str, Any], outcome: str) -> None:
    """Homeowner tapped Job Completed, Still Scheduled, or Didn't Work Out."""
    from datetime import datetime, timedelta, timezone

    from app.services.job_checkin import (
        COMPLETED,
        MODE_AWAITING_JOB_FEEDBACK,
        PENDING,
        SCHEDULED,
    )

    job_id = str(job.get("job_id") or "")
    if job.get("state") != "assigned" or job.get("outcome_status") != PENDING:
        await _already_updated(phone, job_id)
        return
    pro = await pro_store.get(str(job.get("selected_pro_phone") or ""))
    name = (pro or {}).get("business_name") or (pro or {}).get("name") or "your pro"
    if outcome == COMPLETED:
        await job_store.set_state(job_id, "completed")
        await job_store.update_fields(job_id, outcome_status=COMPLETED)
        await conversation_state_store.set(
            phone,
            MODE_AWAITING_JOB_FEEDBACK,
            {"job_id": job_id, "active_feedback_job_id": job_id, "pro_phone": job.get("selected_pro_phone")},
        )
        await homeowner_store.update(phone, active_feedback_job_id=job_id)
        await whatsapp_service.send_text_message(
            phone,
            f"Awesome! Glad *{name}* got it taken care of. "
            "Mind sharing roughly how much you paid and how your experience was "
            "(for example, 1-5 stars or a quick note)? Just reply in one message. "
            "It helps us keep our local pro list in good shape.",
        )
        return
    if outcome == SCHEDULED:
        due = datetime.now(timezone.utc) + timedelta(hours=48)
        await job_store.update_fields(
            job_id,
            outcome_status=PENDING,
            checkin_due_at=due.isoformat(),
            checkin_count=0,
        )
        await whatsapp_service.send_text_message(
            phone,
            "Got it! I'll check back in with you in a couple of days after the appointment.",
        )
        return
    if outcome == FELL_THROUGH:
        await job_store.update_fields(job_id, outcome_status=FELL_THROUGH)
        trade = clean_template_value(job.get("trade"), "repair", limit=32)
        await _send_choice(
            phone,
            settings.tpl_job_rescue_sid,
            {"1": trade, "2": job_id},
            (
                f"Sorry to hear that! Would you like me to reach back out to other pros "
                f"for your {trade} request ({job_ref(job_id)}), or close this request?\n\n"
                "Reply *Find another pro* or *Close request*."
            ),
        )


async def _reopen_fell_through(phone: str, job: dict[str, Any]) -> None:
    """Show another quote, or ask a new set of pros, after a booking falls through."""
    job_id = str(job.get("job_id") or "")
    if job.get("state") != "assigned" or job.get("outcome_status") != "FELL_THROUGH":
        await _already_updated(phone, job_id)
        return
    selected = normalize_phone(str(job.get("selected_pro_phone") or ""))
    responses = await job_store.list_responses(job_id)
    if selected:
        await job_store.upsert_response(job_id, selected, status="not_selected")
    others = [
        response
        for response in responses
        if quote_is_pending(response)
        and normalize_phone(str(response.get("pro_phone") or "")) != selected
    ]
    await job_store.update_fields(
        job_id,
        selected_pro_phone=None,
        outcome_status=None,
        checkin_due_at=None,
        checkin_count=0,
    )
    if others:
        fresh = await job_store.set_state(job_id, "awaiting_pick", selected_pro_phone=None)
        await whatsapp_service.send_text_message(
            phone,
            f"Here is another quote I already have for {job_ref(job_id)}.",
        )
        await _deliver_quote_cards(fresh, others)
        return
    for response in responses:
        pro_phone = normalize_phone(str(response.get("pro_phone") or ""))
        if pro_phone and pro_phone != selected and response.get("status") == "interested":
            await job_store.upsert_response(job_id, pro_phone, status="alerted")
    fresh = await job_store.set_state(job_id, "dispatched", selected_pro_phone=None)
    asked = await reask_pros(fresh)
    await whatsapp_service.send_text_message(
        phone,
        f"I'll ask other pros about {job_ref(job_id)}. I pinged {asked}.",
    )


async def _capture_job_feedback(phone: str, text: str, ctx: dict[str, Any]) -> None:
    from app.services.job_checkin import parse_job_feedback

    job_id = str(ctx.get("active_feedback_job_id") or ctx.get("job_id") or "")
    if not text:
        await whatsapp_service.send_text_message(
            phone, "Reply in one message with the price, a 1-5 rating, or a short note."
        )
        return
    job = await job_store.get_job(job_id) if job_id else None
    if not job or job.get("outcome_status") != "COMPLETED":
        await conversation_state_store.clear(phone)
        await homeowner_store.update(phone, active_feedback_job_id=None)
        return
    parsed = await parse_job_feedback(text)
    await job_store.update_fields(
        job_id,
        final_price_paid=parsed["final_price_paid"],
        homeowner_rating=parsed["rating_1_to_5"],
        homeowner_review_text=parsed["review_summary"],
    )
    if not job.get("pro_stats_recorded"):
        pro_phone = str(job.get("selected_pro_phone") or ctx.get("pro_phone") or "")
        if pro_phone:
            await pro_store.record_completed_job(pro_phone, parsed["rating_1_to_5"])
        await job_store.update_fields(job_id, pro_stats_recorded=True)
    await conversation_state_store.clear(phone)
    await homeowner_store.update(phone, active_feedback_job_id=None)
    await whatsapp_service.send_text_message(
        phone,
        "Thanks, that helps a lot. I'll use it to keep the local pro list in good shape.",
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
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    await job_store.update_fields(
        job_id,
        connected_at=now.isoformat(),
        checkin_due_at=(now + timedelta(hours=48)).isoformat(),
        checkin_count=0,
        outcome_status="PENDING_CHECKIN",
    )
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
    """Older 60-minute timer. Quotes already go out immediately, so this does not expire the job."""
    job = await job_store.get_job(job_id)
    if not job or job.get("state") not in {"dispatched", "awaiting_pick"}:
        return
    await maybe_send_homeowner_options(job_id)
