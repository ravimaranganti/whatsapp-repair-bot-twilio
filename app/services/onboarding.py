"""Phase 3 — automated pro JOIN onboarding via WhatsApp."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.config import settings
from app.models import MODE_IDLE, MODE_PRO_ONBOARDING
from app.services.conversation_state import conversation_state_store
from app.services.events import log_event
from app.services.pros import pro_store
from app.services.whatsapp import whatsapp_service
from app.utils.phones import normalize_phone

logger = logging.getLogger(__name__)

AVAIL_OPTIONS = ["Same day", "Next day", "Within a week"]

# Button / list ids. "other" asks them to type a trade that is not in the list.
# WhatsApp list pickers allow 10 rows. Other is the last row, so nine named trades.
TRADE_CHOICES: list[tuple[str, str]] = [
    ("plumbing", "Plumbing"),
    ("electrical", "Electrical"),
    ("hvac", "HVAC"),
    ("appliance", "Appliance"),
    ("handyman", "Handyman"),
    ("painting", "Painting"),
    ("general_contractor", "General contractor"),
    ("pool", "Pool"),
    ("lawn", "Lawn & garden"),
]
TRADE_MAP = {key: key for key, _label in TRADE_CHOICES}
TRADE_OTHER = "trade:other"
TRADE_MORE_YES = "trade_more:yes"
TRADE_MORE_NO = "trade_more:no"

_TRADE_ALIASES: dict[str, tuple[str, ...]] = {
    "plumbing": ("plumbing", "plumber", "plumbers"),
    "hvac": ("hvac", "air conditioning", "heating"),
    "electrical": ("electrical", "electrician", "electric"),
    "handyman": ("handyman", "handy man"),
    "appliance": ("appliance", "appliances"),
    "painting": ("painting", "painter", "painters", "paint"),
    "general_contractor": (
        "general contractor",
        "general contracting",
        "remodel",
        "remodeling",
        "renovation",
    ),
    "pool": ("pool", "pools", "pool service"),
    "lawn": ("lawn", "lawn care", "landscaping", "mowing"),
}
_DONE_WORDS = frozenset(
    {
        "no",
        "nope",
        "done",
        "finished",
        "that's all",
        "thats all",
        "that is all",
        "all set",
        "stop",
    }
)
_MORE_WORDS = frozenset(
    {"yes", "y", "yeah", "add", "add another", "another", "more"}
)
_NOT_A_TRADE = frozenset(
    {
        "other",
        "yes",
        "no",
        "ok",
        "okay",
        "add",
        "another",
        "more",
        "done",
        "help",
        "menu",
    }
)


def parse_zips(text: str) -> list[str]:
    return re.findall(r"\b\d{5}\b", text or "")


def _norm_phrase(text: str) -> str:
    raw = (text or "").strip().lower().replace("’", "'")
    return " ".join(raw.split())


def known_trades_in_text(text: str) -> list[str]:
    """Pull standard trades out of a typed reply. Order follows TRADE_CHOICES."""
    low = _norm_phrase(text)
    found: list[str] = []
    for key, _label in TRADE_CHOICES:
        for phrase in _TRADE_ALIASES[key]:
            if re.search(rf"\b{re.escape(phrase)}\b", low):
                found.append(key)
                break
    return found


def normalize_custom_trade(text: str) -> str | None:
    """A free-text trade such as 'roofing' or 'pest control'. None if it is not usable."""
    raw = (text or "").lower().replace("&", " and ")
    cleaned = " ".join(re.sub(r"[^a-z0-9 /-]", " ", raw).split())
    cleaned = cleaned.strip(" /-")
    if not cleaned or cleaned in _NOT_A_TRADE:
        return None
    if len(cleaned) < 2 or len(cleaned) > 40:
        return None
    if not re.search(r"[a-z]", cleaned):
        return None
    return cleaned


def _leftover_custom(text: str, known: list[str]) -> str | None:
    low = _norm_phrase(text)
    phrases = [phrase for key in known for phrase in _TRADE_ALIASES[key]]
    for phrase in sorted(phrases, key=len, reverse=True):
        low = re.sub(rf"\b{re.escape(phrase)}\b", " ", low)
    low = re.sub(r"\b(and|or)\b", " ", low)
    low = low.replace(",", " ")
    return normalize_custom_trade(low)


@dataclass
class TradeTurn:
    """What to do with one reply during the trade step."""

    action: str  # add | ask_other | show_list | done | invalid
    trades: list[str] = field(default_factory=list)


def interpret_trade_turn(
    text: str,
    payload: str,
    stage: str,
    selected: list[str] | None = None,
) -> TradeTurn:
    """
    stage is choose (pick from the list), other (they are typing a custom trade),
    or more (add another vs that's all).
    """
    stage = stage or "choose"
    selected = list(selected or [])
    pl = (payload or "").strip().lower()
    phrase = _norm_phrase(text)

    if pl == TRADE_MORE_NO or (stage == "more" and phrase in _DONE_WORDS):
        return TradeTurn("done")
    if pl == TRADE_MORE_YES or (stage == "more" and phrase in _MORE_WORDS):
        return TradeTurn("show_list")
    if pl == TRADE_OTHER or (stage == "choose" and phrase == "other"):
        return TradeTurn("ask_other")

    if pl.startswith("trade:"):
        key = pl.split(":", 1)[1].strip()
        if key in TRADE_MAP and key not in selected:
            return TradeTurn("add", [key])
        if key in TRADE_MAP:
            return TradeTurn("add", [])
        return TradeTurn("invalid")

    if stage == "other":
        custom = normalize_custom_trade(text)
        if not custom or custom in selected:
            return TradeTurn("invalid" if not custom else "add", [] if custom in selected else [])
        return TradeTurn("add", [custom])

    known = known_trades_in_text(text)
    custom = _leftover_custom(text, known)
    adds: list[str] = []
    for key in known:
        if key not in selected and key not in adds:
            adds.append(key)
    if custom and custom not in selected and custom not in adds and custom not in TRADE_MAP:
        adds.append(custom)
    if adds:
        return TradeTurn("add", adds)
    if known and all(key in selected for key in known):
        return TradeTurn("add", [])
    return TradeTurn("invalid")


async def _send_trade_picker(phone: str) -> None:
    """In-session list of trades. Falls back to text if the Content SID is missing."""
    sid = (settings.tpl_pro_trades_sid or "").strip()
    if sid:
        try:
            await whatsapp_service.send_content_template(phone, content_sid=sid)
            return
        except Exception as e:
            logger.warning("pro trades list failed: %s", e)
    lines = "\n".join(f"- {label}" for _key, label in TRADE_CHOICES)
    await whatsapp_service.send_text_message(
        phone,
        "*Step 2/6:* Pick a trade. You can add more than one.\n\n"
        f"{lines}\n"
        "- Other\n\n"
        "Reply with a trade, or type your own, like roofing. "
        "After each one you can add another.",
    )


async def _send_trade_more(phone: str, selected: list[str]) -> None:
    summary = ", ".join(selected) or "none yet"
    sid = (settings.tpl_pro_trade_more_sid or "").strip()
    if sid:
        try:
            await whatsapp_service.send_text_message(
                phone,
                f"Added: {summary}. You can pick more than one.",
            )
            await whatsapp_service.send_content_template(phone, content_sid=sid)
            return
        except Exception as e:
            logger.warning("pro trade-more prompt failed: %s", e)
    await whatsapp_service.send_text_message(
        phone,
        f"Added: {summary}. You can pick more than one.\n\n"
        "Add another trade? Reply *Yes* or *That's all*.",
    )


async def _ask_custom_trade(phone: str) -> None:
    await whatsapp_service.send_text_message(
        phone,
        "What's the trade? Type it in your own words, like roofing, painting, "
        "or garage doors.",
    )


def join_code_ok(body: str) -> bool:
    """JOIN or JOIN <code> — code required only when PRO_JOIN_CODE is set."""
    parts = (body or "").strip().split()
    if not parts or parts[0].upper() != "JOIN":
        return False
    expected = (settings.pro_join_code or "").strip()
    if not expected:
        return True
    return len(parts) >= 2 and parts[1] == expected


async def start_pro_onboarding(phone: str) -> None:
    """Begin the existing 6-step pro profile flow."""
    phone_n = normalize_phone(phone)
    await conversation_state_store.set(
        phone_n, MODE_PRO_ONBOARDING, {"step": 1, "draft": {}}
    )
    await pro_store.upsert(phone_n)
    await whatsapp_service.send_text_message(
        phone_n,
        "Welcome! Let's set up your pro profile.\n\n"
        "*Step 1/6:* What's your name and business name?\n"
        "(e.g. Mike / Mike's Plumbing)",
    )
    await log_event(
        phone=phone_n, role="pro", type="pro_onboarding_started", payload={}
    )


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
        await start_pro_onboarding(phone_n)
        return True

    return False


async def continue_onboarding(
    phone: str,
    body: str,
    state: dict[str, Any],
    payload: str = "",
) -> bool:
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
        ctx.update({"step": 2, "trade_stage": "choose", "draft": draft})
        await conversation_state_store.set(phone_n, MODE_PRO_ONBOARDING, ctx)
        await _send_trade_picker(phone_n)
        return True

    if step == 2:
        stage = str(ctx.get("trade_stage") or "choose")
        selected = list(draft.get("trades") or [])
        turn = interpret_trade_turn(text, payload, stage, selected)
        if turn.action == "ask_other":
            ctx.update({"trade_stage": "other", "draft": draft})
            await conversation_state_store.set(phone_n, MODE_PRO_ONBOARDING, ctx)
            await _ask_custom_trade(phone_n)
            return True
        if turn.action == "show_list":
            ctx.update({"trade_stage": "choose", "draft": draft})
            await conversation_state_store.set(phone_n, MODE_PRO_ONBOARDING, ctx)
            await _send_trade_picker(phone_n)
            return True
        if turn.action == "done":
            if not selected:
                await whatsapp_service.send_text_message(
                    phone_n, "Pick at least one trade before continuing."
                )
                await _send_trade_picker(phone_n)
                return True
            ctx.update({"step": 3, "draft": draft})
            ctx.pop("trade_stage", None)
            await conversation_state_store.set(phone_n, MODE_PRO_ONBOARDING, ctx)
            await whatsapp_service.send_text_message(
                phone_n,
                "*Step 3/6:* Which ZIP codes do you serve? (e.g. 77002 77006 77019)",
            )
            return True
        if turn.action == "add":
            added = [trade for trade in turn.trades if trade not in selected]
            if not added and not selected:
                await _send_trade_picker(phone_n)
                return True
            selected.extend(added)
            draft["trades"] = selected
            ctx.update({"trade_stage": "more", "draft": draft})
            await conversation_state_store.set(phone_n, MODE_PRO_ONBOARDING, ctx)
            if not added:
                await whatsapp_service.send_text_message(
                    phone_n, "That's already on your list."
                )
            await _send_trade_more(phone_n, selected)
            return True
        await whatsapp_service.send_text_message(
            phone_n,
            "Tap a trade, or type one like plumbing or roofing.",
        )
        if stage != "other":
            await _send_trade_picker(phone_n)
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
