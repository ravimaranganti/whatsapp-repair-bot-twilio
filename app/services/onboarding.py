"""Phase 3 — automated pro JOIN onboarding via WhatsApp."""

from __future__ import annotations

import re
from typing import Any

from app.config import settings
from app.models import MODE_IDLE, MODE_PRO_ONBOARDING
from app.services.conversation_state import conversation_state_store
from app.services.events import log_event
from app.services.pros import pro_store
from app.services.whatsapp import whatsapp_service
from app.utils.phones import normalize_phone

TRADE_OPTIONS = ["Plumbing", "HVAC", "Electrical", "Handyman", "Appliance"]
AVAIL_OPTIONS = ["Same day", "Next day", "Within a week"]

TRADE_MAP = {
    "plumbing": "plumbing",
    "hvac": "hvac",
    "electrical": "electrical",
    "handyman": "handyman",
    "appliance": "appliance",
}


def parse_zips(text: str) -> list[str]:
    return re.findall(r"\b\d{5}\b", text or "")


def join_code_ok(body: str) -> bool:
    """JOIN or JOIN <code> — code required only when PRO_JOIN_CODE is set."""
    parts = (body or "").strip().split()
    if not parts or parts[0].upper() != "JOIN":
        return False
    expected = (settings.pro_join_code or "").strip()
    if not expected:
        return True
    return len(parts) >= 2 and parts[1] == expected


async def handle_pro_command(phone: str, body: str) -> bool:
    """
    Handle STOP/PAUSE/RESUME/PROFILE/JOIN.
    Returns True if the message was consumed.
    """
    text = (body or "").strip()
    upper = text.upper()
    phone_n = normalize_phone(phone)

    if upper == "STOP":
        await pro_store.set_opted_in(phone_n, False)
        await conversation_state_store.clear(phone_n)
        await whatsapp_service.send_text_message(
            phone_n,
            "You're opted out of job alerts. Reply JOIN anytime to come back.",
        )
        return True

    if upper == "PAUSE":
        await pro_store.upsert(phone_n, status="paused")
        await whatsapp_service.send_text_message(
            phone_n, "Alerts paused. Reply RESUME when you're ready again."
        )
        return True

    if upper == "RESUME":
        await pro_store.upsert(phone_n, status="active")
        await whatsapp_service.send_text_message(
            phone_n, "Welcome back — alerts resumed."
        )
        return True

    if upper == "PROFILE":
        pro = await pro_store.get(phone_n)
        if not pro:
            await whatsapp_service.send_text_message(
                phone_n, "No pro profile yet. Reply JOIN to set one up."
            )
            return True
        trades = ", ".join(pro.get("trades") or []) or "—"
        zips = ", ".join(pro.get("service_zips") or []) or "—"
        msg = (
            f"*Your pro profile*\n"
            f"Name: {pro.get('name') or '—'}\n"
            f"Business: {pro.get('business_name') or '—'}\n"
            f"Trades: {trades}\n"
            f"ZIPs: {zips}\n"
            f"Availability: {pro.get('availability_text') or '—'}\n"
            f"Rate: {pro.get('rate_text') or '—'}\n"
            f"Opted in: {'yes' if pro.get('opted_in') else 'no'}\n"
            f"Status: {pro.get('status') or '—'}"
        )
        await whatsapp_service.send_text_message(phone_n, msg)
        return True

    if join_code_ok(text):
        await conversation_state_store.set(
            phone_n, MODE_PRO_ONBOARDING, {"step": 1, "draft": {}}
        )
        await pro_store.upsert(phone_n)  # ensure doc exists
        await whatsapp_service.send_text_message(
            phone_n,
            "Welcome! Let's set up your pro profile.\n\n"
            "*Step 1/6:* What's your name and business name?\n"
            "(e.g. Mike / Mike's Plumbing)",
        )
        await log_event(
            phone=phone_n, role="pro", type="pro_onboarding_started", payload={}
        )
        return True

    return False


async def continue_onboarding(phone: str, body: str, state: dict[str, Any]) -> bool:
    """Advance multi-step JOIN flow. Returns True if handled."""
    if state.get("mode") != MODE_PRO_ONBOARDING:
        return False

    ctx = dict(state.get("context") or {})
    step = int(ctx.get("step") or 1)
    draft: dict[str, Any] = dict(ctx.get("draft") or {})
    text = (body or "").strip()
    phone_n = normalize_phone(phone)

    if step == 1:
        # "Name / Business"
        if "/" in text:
            name, biz = [p.strip() for p in text.split("/", 1)]
        else:
            name, biz = text, text
        draft["name"] = name
        draft["business_name"] = biz
        ctx.update({"step": 2, "draft": draft})
        await conversation_state_store.set(phone_n, MODE_PRO_ONBOARDING, ctx)
        opts = ", ".join(TRADE_OPTIONS)
        await whatsapp_service.send_text_message(
            phone_n,
            f"*Step 2/6:* Which trade(s)? Reply with one or more of:\n{opts}",
        )
        return True

    if step == 2:
        found = []
        low = text.lower()
        for label, key in TRADE_MAP.items():
            if label in low:
                found.append(key)
        if not found:
            await whatsapp_service.send_text_message(
                phone_n,
                "Please reply with at least one trade: "
                + ", ".join(TRADE_OPTIONS),
            )
            return True
        draft["trades"] = found
        ctx.update({"step": 3, "draft": draft})
        await conversation_state_store.set(phone_n, MODE_PRO_ONBOARDING, ctx)
        await whatsapp_service.send_text_message(
            phone_n,
            "*Step 3/6:* Which ZIP codes do you serve? (e.g. 77002 77006 77019)",
        )
        return True

    if step == 3:
        zips = parse_zips(text)
        if not zips:
            await whatsapp_service.send_text_message(
                phone_n, "Please include at least one 5-digit ZIP code."
            )
            return True
        draft["service_zips"] = zips
        ctx.update({"step": 4, "draft": draft})
        await conversation_state_store.set(phone_n, MODE_PRO_ONBOARDING, ctx)
        await whatsapp_service.send_text_message(
            phone_n,
            "*Step 4/6:* Typical availability? Reply: Same day / Next day / Within a week",
        )
        return True

    if step == 4:
        low = text.lower()
        avail = None
        for opt in AVAIL_OPTIONS:
            if opt.lower() in low:
                avail = opt
                break
        if not avail:
            avail = text[:80]
        draft["availability_text"] = avail
        ctx.update({"step": 5, "draft": draft})
        await conversation_state_store.set(phone_n, MODE_PRO_ONBOARDING, ctx)
        await whatsapp_service.send_text_message(
            phone_n,
            "*Step 5/6:* What's your typical rate or visit fee? (free text)",
        )
        return True

    if step == 5:
        draft["rate_text"] = text[:120]
        ctx.update({"step": 6, "draft": draft})
        await conversation_state_store.set(phone_n, MODE_PRO_ONBOARDING, ctx)
        await whatsapp_service.send_text_message(
            phone_n,
            "*Step 6/6:* You'll receive job alerts on WhatsApp. Reply STOP anytime.\n"
            "Reply *Yes* to opt in, or *No* to cancel.",
        )
        return True

    if step == 6:
        if text.lower() in {"yes", "y", "yeah", "sure"}:
            from datetime import datetime, timezone

            await pro_store.upsert(
                phone_n,
                name=draft.get("name"),
                business_name=draft.get("business_name"),
                trades=draft.get("trades") or [],
                service_zips=draft.get("service_zips") or [],
                availability_text=draft.get("availability_text"),
                rate_text=draft.get("rate_text"),
                opted_in=True,
                opted_in_at=datetime.now(timezone.utc).isoformat(),
                status="active",
            )
            await conversation_state_store.clear(phone_n)
            trades = ", ".join(draft.get("trades") or [])
            zips = ", ".join(draft.get("service_zips") or [])
            await whatsapp_service.send_text_message(
                phone_n,
                "You're in! Here's your summary:\n"
                f"*{draft.get('business_name') or draft.get('name')}*\n"
                f"Trades: {trades}\nZIPs: {zips}\n"
                f"Avail: {draft.get('availability_text')}\n"
                f"Rate: {draft.get('rate_text')}\n\n"
                "We'll message you when a matching job comes in.",
            )
            await log_event(
                phone=phone_n, role="pro", type="pro_opted_in", payload=draft
            )
        else:
            await conversation_state_store.clear(phone_n)
            await whatsapp_service.send_text_message(
                phone_n,
                "No problem — profile saved but not opted in. Reply JOIN to try again.",
            )
        return True

    return False
