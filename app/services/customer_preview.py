"""Homeowner replies while job posting is paused.

Pros keep onboarding. A customer gets a short notice, a diagnosis, and up to
three Google listings. No job is created and no pro is alerted.
"""

from __future__ import annotations

import logging
from typing import Any

from app.models import MessageContent
from app.services.conversation_state import conversation_state_store
from app.services.homeowner_profile import parse_zip_and_name, saved_zip, trade_phrase, zip_only_update
from app.services.homeowners import homeowner_store
from app.services.places import search_local_specialists
from app.services.service_areas import area_for_zip
from app.services.whatsapp import whatsapp_service

logger = logging.getLogger(__name__)

PREVIEW_MODE = "awaiting_preview_zip"

INTRO = (
    "Thanks for reaching out. Right now we're onboarding vetted local professionals, "
    "and the full service will start soon."
)
PREVIEW_OFFER = (
    "I can still look at the problem and show you 3 top-rated local companies you can call. "
    "Nothing is sent to our pros yet."
)
PAUSED_HOMEOWNER = (
    f"{INTRO}\n\n"
    "Describe the issue or send a photo, and I'll show 3 top-rated local companies you can call. "
    "Pros can reply *Pro Signup* to join."
)


def preview_places_message(trade: str, zip_code: str, places: list[dict[str, Any]]) -> str:
    label = trade_phrase(trade)
    area = (zip_code or "").strip() or "your area"
    if not places:
        return (
            f"I couldn't find a public {label} listing near {area} at 4.6 stars with 30 reviews yet. "
            "Nothing was sent to our pros."
        )
    lines = [
        f"Here are 3 top-rated local {label} companies near {area} you can call:",
        "",
    ]
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
    lines.append("")
    lines.append("Nothing was sent to our pros.")
    return "\n".join(lines).strip()


_FILLER = frozenset(
    {"yes", "y", "no", "no thanks", "ok", "okay", "thanks", "thank you", "sure", "hi", "hello"}
)


async def handle_customer_preview(
    message: MessageContent, phone: str, state: dict[str, Any]
) -> bool:
    mode = str(state.get("mode") or "")
    body = (message.body or "").strip()
    ctx = state.get("context") if isinstance(state.get("context"), dict) else {}
    payload = (message.button_payload or message.list_id or "").strip()

    if payload and message.type != "image":
        await whatsapp_service.send_text_message(phone, PAUSED_HOMEOWNER)
        return True

    if mode == PREVIEW_MODE and message.type != "image":
        zip_code = zip_only_update(body)
        if zip_code:
            await _send_places(phone, str(ctx.get("trade") or "repair"), zip_code)
            return True

    if message.type == "image":
        await _from_image(message, phone, body)
        return True
    if message.type == "video":
        await whatsapp_service.send_text_message(
            phone,
            f"{INTRO}\n\nA clear photo or a short description works better than a video. "
            "Nothing is sent to our pros yet.",
        )
        return True
    if body and body.lower().strip(" .!") not in _FILLER:
        await _from_text(phone, body)
        return True
    await whatsapp_service.send_text_message(phone, PAUSED_HOMEOWNER)
    return True


async def _from_text(phone: str, body: str) -> None:
    from app.services.gemini import gemini_service

    await whatsapp_service.send_text_message(phone, f"{INTRO}\n\n{PREVIEW_OFFER}")
    try:
        diagnosis = await gemini_service.analyze_text(body, phone=phone)
    except Exception as exc:
        logger.warning("preview text diagnosis failed: %s", exc)
        await whatsapp_service.send_text_message(
            phone,
            "I couldn't read that yet. Send one more sentence about the problem, or a clear photo.",
        )
        return
    await _finish_diagnosis(phone, diagnosis, body)


async def _from_image(message: MessageContent, phone: str, body: str) -> None:
    from app.services.gemini import gemini_service

    await whatsapp_service.send_text_message(phone, f"{INTRO}\n\n{PREVIEW_OFFER}")
    if not message.media_url:
        await whatsapp_service.send_text_message(
            phone, "I didn't receive the photo. Please send it one more time."
        )
        return
    try:
        image_bytes = await whatsapp_service.download_media(message.media_url)
        diagnosis = await gemini_service.analyze_image(
            image_bytes,
            body,
            message.media_content_type or "image/jpeg",
            phone=phone,
        )
    except Exception as exc:
        logger.warning("preview image diagnosis failed: %s", exc)
        await whatsapp_service.send_text_message(
            phone,
            "I couldn't analyze that photo. Please try another clear shot of the problem.",
        )
        return
    await _finish_diagnosis(phone, diagnosis, body)


async def _finish_diagnosis(phone: str, diagnosis: Any, body: str) -> None:
    text = (diagnosis.homeowner_message or "").strip()
    if text:
        await whatsapp_service.send_text_message(phone, text[:1550])
    if diagnosis.trade.value == "not_home_issue":
        await conversation_state_store.clear(phone)
        return
    zip_code = parse_zip_and_name(body)[0]
    if not zip_code:
        ho = await homeowner_store.get(phone)
        zip_code = saved_zip(ho or {})
    if zip_code:
        await _send_places(phone, diagnosis.trade.value, zip_code)
        return
    await conversation_state_store.set(
        phone,
        PREVIEW_MODE,
        {"trade": diagnosis.trade.value},
    )
    label = trade_phrase(diagnosis.trade.value)
    await whatsapp_service.send_text_message(
        phone,
        f"What's your 5-digit ZIP code? I'll use it to find 3 top-rated local {label} companies you can call.",
    )


async def _send_places(phone: str, trade: str, zip_code: str) -> None:
    await homeowner_store.ensure(
        phone,
        zip_code=zip_code,
        area_zip=zip_code,
        default_zone=area_for_zip(zip_code),
    )
    places = await search_local_specialists(trade, zip_code)
    await whatsapp_service.send_text_message(
        phone, preview_places_message(trade, zip_code, places)
    )
    await conversation_state_store.clear(phone)
