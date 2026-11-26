"""
Agent orchestration — routes inbound WhatsApp messages through
allowlist, conversation state, diagnosis, chat, and dispatch.
"""

from __future__ import annotations

import logging

from app.config import settings
from app.models import (
    MODE_AWAITING_DISPATCH_CONSENT,
    MODE_AWAITING_FEEDBACK_COMMENT,
    MODE_AWAITING_PICK,
    MODE_AWAITING_SHOW_OPTIONS,
    MODE_PRO_AWAITING_DETAILS,
    MODE_PRO_ONBOARDING,
    MessageContent,
)
from app.services.conversation_store import conversation_store
from app.services.conversation_state import conversation_state_store
from app.services.events import log_event
from app.services.gemini import gemini_service
from app.services.homeowners import homeowner_store
from app.services.memory_bank import memory_bank_service
from app.services.whatsapp import whatsapp_service
from app.utils.phones import normalize_phone

logger = logging.getLogger(__name__)

PILOT_DENY_MSG = (
    "Thanks for messaging Houston Home Repair AI — this is a private pilot "
    "right now. We'll open it up soon!"
)


def _on_allowlist(phone: str) -> bool:
    allow = settings.allowlist_numbers()
    if not allow:
        return True  # empty allowlist = allow all (local/dev)
    return normalize_phone(phone) in allow


async def process_message(message: MessageContent, sender: str):
    """Main entry after webhook ack / task dequeue."""
    try:
        from app.services.inbound_guard import cap_text

        phone = normalize_phone(sender)
        message.body = cap_text(message.body or "")
        from app.services.session_window import record_inbound

        await record_inbound(phone)
        await log_event(
            phone=phone,
            role="homeowner",
            type="inbound_message",
            payload={
                "message_sid": message.id,
                "type": message.type,
                "body_preview": (message.body or "")[:200],
                "button_payload": message.button_payload,
            },
        )

        # Allowlist (JOIN with code bypasses for pros — checked in onboarding)
        body_u = (message.body or "").strip().upper()
        from app.services.onboarding import join_code_ok

        from app.services.portal_commands import is_dashboard_command, is_pro_signup

        if (
            not _on_allowlist(phone)
            and not join_code_ok(message.body or "")
            and not is_pro_signup(message.body or "")
            and not is_dashboard_command(message.body or "")
        ):
            await whatsapp_service.send_text_message(phone, PILOT_DENY_MSG)
            return

        from app.services.welcome import handle_welcome

        welcome_state = await conversation_state_store.get(phone)
        if await handle_welcome(message, phone, welcome_state):
            return

        # Feedback keyword
        from app.services.feedback import (
            capture_pending_feedback,
            handle_feedback_keyword,
        )

        if await handle_feedback_keyword(phone, message.body or ""):
            return

        from app.services.feedback import handle_followup_rating

        if await handle_followup_rating(
            phone, message.body or "", message.button_payload or ""
        ):
            return

        state = await conversation_state_store.get(phone)
        mode = state.get("mode")

        if mode == MODE_AWAITING_FEEDBACK_COMMENT:
            if await capture_pending_feedback(
                phone, message.body or "", message.media_url, state
            ):
                return

        # Pro commands / onboarding
        from app.services.onboarding import (
            continue_onboarding,
            handle_finished_setup_tap,
            handle_pro_command,
            is_onboarding_tap,
        )

        if await handle_pro_command(phone, message.body or ""):
            return
        tap = message.button_payload or message.list_id or ""
        if mode == MODE_PRO_ONBOARDING:
            if await continue_onboarding(
                phone,
                message.body or "",
                state,
                tap,
                message.interactive_data or "",
            ):
                return
        elif is_onboarding_tap(
            tap, message.body or "", interactive=message.type == "interactive"
        ):
            await handle_finished_setup_tap(phone)
            return

        # Pro alert responses / details
        from app.services.dispatch import (
            handle_homeowner_state,
            handle_pro_alert_response,
            handle_pro_details,
        )

        payload = message.button_payload or message.list_id or ""
        from app.services.alert_reply import resolve_alert_reply

        named_job = resolve_alert_reply(payload, "", []).job_id
        if named_job and await handle_pro_alert_response(
            phone, message.body or "", payload
        ):
            await log_event(
                phone=phone,
                role="pro",
                type="button_tap",
                payload={"payload": payload, "body": message.body},
            )
            return

        if mode == MODE_PRO_AWAITING_DETAILS:
            if await handle_pro_details(phone, message.body or "", state):
                return

        if await handle_pro_alert_response(phone, message.body or "", payload):
            await log_event(
                phone=phone,
                role="pro",
                type="button_tap",
                payload={"payload": payload, "body": message.body},
            )
            return

        # Homeowner stateful flows. A button that names a job is handled
        # even when the session is sitting on a different request.
        from app.services.homeowner_actions import parse_homeowner_payload

        homeowner_payload = message.button_payload or message.list_id or ""
        if (
            parse_homeowner_payload(homeowner_payload)
            or mode
            in {
                MODE_AWAITING_DISPATCH_CONSENT,
                MODE_AWAITING_PICK,
                MODE_AWAITING_SHOW_OPTIONS,
                "awaiting_area",
                "awaiting_share_notes",
                "awaiting_visit_details",
                "awaiting_quote_question",
                "awaiting_new_issue_confirm",
                "awaiting_job_feedback",
            }
            or homeowner_payload == "show_options"
            or homeowner_payload.startswith("show_options:")
        ):
            if await handle_homeowner_state(
                phone, message.body or "", homeowner_payload or payload, state
            ):
                return

        from app.services.dispatch import handle_homeowner_typed

        if await handle_homeowner_typed(phone, message.body or ""):
            return

        if message.type == "text":
            await handle_text_message(message, phone)
        elif message.type == "image":
            await handle_image_message(message, phone)
        elif message.type == "video":
            await handle_video_message(message, phone)
        elif message.type == "interactive":
            # Treat as text with payload already tried above
            await handle_text_message(message, phone)
        else:
            await send_unsupported_message(phone)

    except Exception as e:
        logger.error("Error processing message from %s: %s", sender, e, exc_info=True)
        await send_error_message(normalize_phone(sender))


async def handle_text_message(message: MessageContent, sender: str):
    raw = (message.body or "").strip()
    text_body = raw.lower()
    logger.info("Text message from %s: %s", sender, text_body[:80])

    # retry keyword after expired job
    if text_body == "retry" and settings.dispatch_enabled:
        await whatsapp_service.send_text_message(
            sender, "Send a photo of the issue and we can try matching pros again."
        )
        return

    prior = await conversation_store.get_prior_turns(sender)
    memory_facts = await memory_bank_service.retrieve_facts(sender, raw)
    try:
        service_notes = await homeowner_store.list_service_notes(sender)
    except Exception:
        service_notes = []
    profile_note = ""
    try:
        from app.services.homeowner_profile import chat_profile_note
        from app.services.jobs import job_store

        ho = await homeowner_store.get(sender)
        job_id = str((ho or {}).get("active_job_id") or "")
        job = await job_store.get_job(job_id) if job_id else None
        profile_note = chat_profile_note(ho, job)
    except Exception:
        logger.warning("homeowner profile note unavailable for %s", sender)
    try:
        reply = await gemini_service.chat_reply(
            prior,
            raw,
            memory_facts=memory_facts,
            service_notes=service_notes,
            profile_note=profile_note,
            phone=sender,
        )
    except Exception as e:
        logger.error("Conversational reply failed: %s", e, exc_info=True)
        reply = (
            "I had trouble thinking that through — please try again in a moment, "
            "or send a photo of the issue so I can diagnose it."
        )
    await conversation_store.append_turn(sender, "user", raw)
    await conversation_store.append_turn(sender, "assistant", reply)
    await whatsapp_service.send_text_message(sender, reply)
    await memory_bank_service.record_exchange(
        sender,
        [{"role": "user", "content": raw}, {"role": "assistant", "content": reply}],
    )


async def handle_image_message(message: MessageContent, sender: str):
    try:
        if not message.media_url:
            logger.error("Image message has no media URL")
            await send_error_message(sender)
            return

        user_description = (message.body or "").strip()
        photo_note = "[Sent a photo]"
        if user_description:
            photo_note = f"[Sent a photo - note: {user_description}]"
        await conversation_store.append_turn(sender, "user", photo_note)

        await whatsapp_service.send_text_message(
            sender, "Analyzing your image… this may take a moment."
        )

        image_bytes = await whatsapp_service.download_media(message.media_url)
        mime_type = message.media_content_type or "image/jpeg"
        prior = await conversation_store.get_prior_turns(sender)
        memory_facts = await memory_bank_service.retrieve_facts(
            sender, user_description or "home repair photo"
        )
        try:
            service_notes = await homeowner_store.list_service_notes(sender)
        except Exception:
            service_notes = []

        try:
            diagnosis = await gemini_service.analyze_image(
                image_bytes,
                user_description,
                mime_type,
                prior_turns=prior,
                memory_facts=memory_facts,
                service_notes=service_notes,
                phone=sender,
            )
        except Exception as e:
            logger.error("Diagnosis failed: %s", e, exc_info=True)
            await whatsapp_service.send_text_message(
                sender,
                "I couldn't analyze that photo. Please try another clear shot "
                "of the job (good lighting, the whole area in frame).",
            )
            return

        # Homeowner-facing message
        parts = [diagnosis.homeowner_message]
        if diagnosis.follow_up_question and diagnosis.confidence == "low":
            parts.append(diagnosis.follow_up_question)
        homeowner_msg = "\n\n".join(p for p in parts if p)

        # Same brief the pro will receive, so the homeowner can see it before we ask anyone.
        pb = diagnosis.pro_brief
        pro_lines = [
            "*Here's what I'll tell a pro:*",
            f"*Issue:* {pb.issue}",
            f"*Likely cause:* {pb.likely_cause}",
            f"*Severity:* {diagnosis.severity.value}",
            f"*Trade:* {diagnosis.trade.value}",
            f"*Parts:* {', '.join(pb.parts_needed) or '—'}",
            f"*Est. time:* {pb.est_time}",
        ]
        if pb.notes:
            pro_lines.append(f"*Notes:* {pb.notes}")
        if diagnosis.safety_flags:
            pro_lines.append(
                "*Safety:* " + ", ".join(f.value for f in diagnosis.safety_flags)
            )

        await whatsapp_service.send_text_message(sender, homeowner_msg[:1550])
        if diagnosis.trade.value != "not_home_issue":
            await whatsapp_service.send_text_message(
                sender, "\n".join(pro_lines)[:1550]
            )

        summary = diagnosis.homeowner_message[:400]
        await conversation_store.append_turn(
            sender, "assistant", f"(Diagnosis from your photo) {summary}"
        )
        await memory_bank_service.record_exchange(
            sender,
            [
                {"role": "user", "content": photo_note},
                {"role": "assistant", "content": f"(Diagnosis) {summary}"},
            ],
        )

        if diagnosis.trade.value != "not_home_issue":
            from app.services.dispatch import after_diagnosis

            await after_diagnosis(
                phone=sender,
                diagnosis=diagnosis,
                photo_url=message.media_url,
            )

        logger.info(
            "Processed image for %s trade=%s sev=%s diy=%s",
            sender,
            diagnosis.trade.value,
            diagnosis.severity.value,
            diagnosis.diy_appropriate,
        )

    except Exception as e:
        logger.error("Error handling image message: %s", e, exc_info=True)
        await send_error_message(sender)


async def handle_video_message(message: MessageContent, sender: str):
    await whatsapp_service.send_text_message(
        sender,
        "Video received. For best results, please send a clear photo of the issue instead.",
    )


async def send_unsupported_message(sender: str):
    await whatsapp_service.send_text_message(
        sender,
        "I can only analyze photos of household issues right now. "
        "Please send an image showing the problem.",
    )


async def send_error_message(sender: str):
    await whatsapp_service.send_text_message(
        sender,
        "Sorry, something went wrong while processing your request. Please try again.",
    )
