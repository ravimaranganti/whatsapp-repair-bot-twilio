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
        phone = normalize_phone(sender)
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

        if not _on_allowlist(phone) and not join_code_ok(message.body or ""):
            await whatsapp_service.send_text_message(phone, PILOT_DENY_MSG)
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
        from app.services.onboarding import continue_onboarding, handle_pro_command

        if await handle_pro_command(phone, message.body or ""):
            return
        if mode == MODE_PRO_ONBOARDING:
            if await continue_onboarding(phone, message.body or "", state):
                return

        # Pro alert responses / details
        from app.services.dispatch import (
            handle_homeowner_state,
            handle_pro_alert_response,
            handle_pro_details,
        )

        if mode == MODE_PRO_AWAITING_DETAILS:
            if await handle_pro_details(phone, message.body or "", state):
                return

        payload = message.button_payload or ""
        if await handle_pro_alert_response(phone, message.body or "", payload):
            await log_event(
                phone=phone,
                role="pro",
                type="button_tap",
                payload={"payload": payload, "body": message.body},
            )
            return

        # Homeowner stateful flows
        if mode in {
            MODE_AWAITING_DISPATCH_CONSENT,
            MODE_AWAITING_PICK,
            MODE_AWAITING_SHOW_OPTIONS,
            "awaiting_area",
            "awaiting_share_notes",
            "awaiting_new_issue_confirm",
        } or (message.button_payload or "") == "show_options":
            if await handle_homeowner_state(
                phone, message.body or "", payload, state
            ):
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

    if text_body in {"help", "start", "hello", "hi"}:
        welcome_message = (
            "Welcome to *Houston Home Repair AI*!\n\n"
            "Send a clear photo of a plumbing, HVAC, electrical, appliance, "
            "or handyman issue. I'll diagnose it and — when you're ready — "
            "ask local opted-in pros for availability and estimates.\n\n"
            "Pros: reply *JOIN* to opt in for job alerts."
        )
        await conversation_store.append_turn(sender, "user", raw or text_body)
        await conversation_store.append_turn(sender, "assistant", welcome_message)
        await whatsapp_service.send_text_message(sender, welcome_message)
        return

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
    try:
        reply = await gemini_service.chat_reply(
            prior,
            raw,
            memory_facts=memory_facts,
            service_notes=service_notes,
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
                "of the issue (good lighting, whole fixture in frame).",
            )
            return

        # Homeowner-facing message
        parts = [diagnosis.homeowner_message]
        if diagnosis.follow_up_question and diagnosis.confidence == "low":
            parts.append(diagnosis.follow_up_question)
        homeowner_msg = "\n\n".join(p for p in parts if p)

        # Pro brief for repair team (no phone dump when dispatch enabled)
        pb = diagnosis.pro_brief
        pro_lines = [
            "*For our repair team:*",
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

        if not settings.dispatch_enabled:
            # Legacy-style: append static directory recommendations (no phones in chat tools path)
            from app.services.professional_matcher import (
                format_professional_recommendations,
                recommend_professionals,
            )

            cat_map = {
                "plumbing": "plumbers",
                "hvac": "hvac",
                "electrical": "electricians",
                "handyman": "handyman",
                "appliance": "handyman",
            }
            cat = cat_map.get(diagnosis.trade.value, "handyman")
            sev_map = {
                "low": "Low",
                "medium": "Medium",
                "high": "High",
                "emergency": "High",
            }
            recs, urgency = recommend_professionals(
                cat, sev_map.get(diagnosis.severity.value, "Medium"), 3
            )
            # Soften WhatsApp formatting in legacy formatter output
            section = format_professional_recommendations(recs, urgency)
            section = section.replace("**", "*")
            pro_lines.append(section)

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

        # Phase 4 path (DIY follow-up or consent) when flag on
        if settings.dispatch_enabled and diagnosis.trade.value != "not_home_issue":
            from app.services.dispatch import after_diagnosis

            # If DIY, after_diagnosis sends DIY steps; avoid duplicate homeowner_msg DIY
            # We already sent homeowner_msg — after_diagnosis may re-send with steps.
            # For DIY, skip duplicate: only call after_diagnosis which includes message.
            # Simpler: if diy, we already sent message without steps — let after_diagnosis add steps.
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
