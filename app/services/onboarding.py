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
from app.services.pro_capacity import (
    capacity_summary,
    fee_label,
    interpret_dispatch_fee,
    interpret_emergency,
    interpret_lead_time,
    lead_label,
)
from app.services.pro_profile import (
    clean_profile_note,
    interpret_insured,
    interpret_languages,
    interpret_photo_estimate,
    language_label,
    needs_insurance_question,
)
from app.services.pros import pro_store
from app.services.service_areas import (
    AREA_ALL,
    AREA_CHOICES,
    format_areas,
    interpret_area_turn,
    zips_for_areas,
)
from app.services.whatsapp import whatsapp_service
from app.utils.phones import normalize_phone

logger = logging.getLogger(__name__)

# Painting is part of handyman, not its own row. Pool, lawn, and sprinkler are
# typed under Other. Mobile mechanic and cleaning are their own rows.
TRADE_CHOICES: list[tuple[str, str]] = [
    ("plumbing", "Plumbing"),
    ("electrical", "Electrical"),
    ("hvac", "HVAC"),
    ("appliance", "Appliance"),
    ("handyman", "Handyman"),
    ("general_contractor", "General contractor"),
    ("mobile_auto", "Mobile mechanic"),
    ("cleaning", "Cleaning service"),
]
_EXTRA_TRADE_LABELS = {
    "painting": "Painting",
    "pool": "Pool",
    "lawn": "Lawn",
    "sprinkler": "Sprinkler",
}
TRADE_LABELS = {key: label for key, label in TRADE_CHOICES}
TRADE_LABELS.update(_EXTRA_TRADE_LABELS)
TRADE_MAP = {key: key for key in TRADE_LABELS}
# Old buttons and words that now belong to a row above.
_TRADE_REDIRECTS = {
    "painting": "handyman",
    "car_detailing": "mobile_auto",
    "detailing": "mobile_auto",
}
TRADE_OTHER = "trade:other"
TRADE_MORE_YES = "trade_more:yes"
TRADE_MORE_NO = "trade_more:no"

# Taps from an earlier setup bubble. WhatsApp keeps those messages tappable.
_ONBOARDING_PREFIXES = ("trade:", "trade_more:", "area:", "area_more:")
_ONBOARDING_IDS = frozenset(
    {
        "emerg_yes",
        "emerg_no",
        "lead_same_day",
        "lead_48h",
        "lead_3plus",
        "fee_free",
        "fee_50_99",
        "fee_100_plus",
        "lang_en",
        "lang_es",
        "lang_both",
        "insured_yes",
        "insured_no",
        "photo_est_yes",
        "photo_est_no",
    }
)
_ONBOARDING_TITLES = frozenset(
    {
        "other",
        "add another",
        "that's all",
        "thats all",
        "yes, same-day/urgent",
        "no, scheduled only",
        "same day",
        "next 48 hours",
        "3+ days out",
        "free estimate",
        "$50 - $99 fee",
        "$100+ fee",
        "english",
        "spanish",
        "both",
        "yes, insured",
        "not insured",
        "yes, for free",
        "no, not free",
        "mobile auto repair",
        "mobile mechanic",
        "car detailing",
        "cleaning",
        "painting",
    }
)


def is_onboarding_tap(payload: str, text: str = "", *, interactive: bool = False) -> bool:
    """True for a setup button or list row, including one left behind in the thread."""
    pl = (payload or "").strip().lower()
    if pl.startswith(_ONBOARDING_PREFIXES) or pl in _ONBOARDING_IDS:
        return True
    if not interactive:
        return False
    title = " ".join((text or "").strip().lower().split())
    return title in _ONBOARDING_TITLES

_TRADE_ALIASES: dict[str, tuple[str, ...]] = {
    "plumbing": ("plumbing", "plumber", "plumbers"),
    "hvac": ("hvac", "air conditioning", "heating"),
    "electrical": ("electrical", "electrician", "electric"),
    "handyman": (
        "handyman",
        "handy man",
        "painting",
        "painter",
        "painters",
        "paint",
    ),
    "appliance": ("appliance", "appliances"),
    "cleaning": (
        "cleaning service",
        "cleaning",
        "house cleaning",
        "cleaner",
        "housekeeper",
    ),
    "general_contractor": (
        "general contractor",
        "general contracting",
        "remodel",
        "remodeling",
        "renovation",
    ),
    "pool": ("pool repair", "pool service", "pools", "pool"),
    "lawn": ("lawn work", "lawn care", "landscaping", "mowing", "lawn"),
    "sprinkler": ("sprinkler repair", "sprinklers", "sprinkler", "irrigation"),
    "mobile_auto": (
        "car detailing",
        "auto detailing",
        "detailing",
        "mobile auto repair",
        "mobile auto",
        "auto repair",
        "mobile mechanic",
        "mechanic",
    ),
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


def _norm_phrase(text: str) -> str:
    raw = (text or "").strip().lower().replace("’", "'")
    return " ".join(raw.split())


def format_trades(trades: list[str] | None) -> str:
    """Human labels for stored trade keys. Custom text is shown as typed."""
    labels = [TRADE_LABELS.get(str(trade), str(trade)) for trade in (trades or [])]
    return ", ".join(labels)


def known_trades_in_text(text: str) -> list[str]:
    """Pull known trades out of a typed reply, including Other hints."""
    low = _norm_phrase(text)
    found: list[str] = []
    for key, phrases in _TRADE_ALIASES.items():
        for phrase in phrases:
            if re.search(rf"\b{re.escape(phrase)}\b", low):
                found.append(_TRADE_REDIRECTS.get(key, key))
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
    if not any(len(word) > 2 for word in cleaned.split()):
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
        key = _TRADE_REDIRECTS.get(pl.split(":", 1)[1].strip(), pl.split(":", 1)[1].strip())
        if key in TRADE_MAP and key not in selected:
            return TradeTurn("add", [key])
        if key in TRADE_MAP:
            return TradeTurn("add", [])
        return TradeTurn("invalid")

    if stage == "other":
        known = [key for key in known_trades_in_text(text) if key not in selected]
        if known:
            return TradeTurn("add", known)
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
    """In-session list. Falls back to text if the Content SID is missing."""
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
        "*Step 2:* Pick a trade. You can add more than one. "
        "Handyman includes painting.\n\n"
        f"{lines}\n"
        "- Other\n\n"
        "Reply with a trade. For pool, lawn, or sprinkler, reply *Other*.",
    )


async def _send_trade_more(phone: str, selected: list[str]) -> None:
    summary = format_trades(selected) or "none yet"
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
        "What's the trade? Pool, lawn, and sprinkler go here. "
        "You can also type something else, like roofing or garage doors.",
    )


async def _send_area_picker(phone: str) -> None:
    """In-session list of Houston areas. Falls back to text if the SID is missing."""
    sid = (settings.tpl_pro_areas_sid or "").strip()
    if sid:
        try:
            await whatsapp_service.send_content_template(phone, content_sid=sid)
            return
        except Exception as e:
            logger.warning("pro areas list failed: %s", e)
    lines = "\n".join(f"- {label}" for _key, label, _note in AREA_CHOICES)
    await whatsapp_service.send_text_message(
        phone,
        "*Step 3/7:* Where do you work? You can add more than one.\n\n"
        f"{lines}\n\n"
        "Reply with an area, like Katy or The Woodlands.",
    )


async def _send_area_more(phone: str, selected: list[str]) -> None:
    summary = format_areas(selected) or "none yet"
    sid = (settings.tpl_pro_area_more_sid or "").strip()
    if sid:
        try:
            await whatsapp_service.send_text_message(
                phone,
                f"Added: {summary}. You can pick more than one.",
            )
            await whatsapp_service.send_content_template(phone, content_sid=sid)
            return
        except Exception as e:
            logger.warning("pro area-more prompt failed: %s", e)
    await whatsapp_service.send_text_message(
        phone,
        f"Added: {summary}. You can pick more than one.\n\n"
        "Add another area? Reply *Yes* or *That's all*.",
    )


async def _send_emergency_prompt(phone: str) -> None:
    sid = (settings.tpl_pro_emergency_sid or "").strip()
    if sid:
        try:
            await whatsapp_service.send_content_template(phone, content_sid=sid)
            return
        except Exception as e:
            logger.warning("pro emergency prompt failed: %s", e)
    await whatsapp_service.send_text_message(
        phone,
        "*Step 4/7:* Do you take same-day or emergency calls when urgent jobs pop up?\n\n"
        "Reply *Yes* or *No*.",
    )


async def _send_lead_time_prompt(phone: str) -> None:
    sid = (settings.tpl_pro_lead_time_sid or "").strip()
    if sid:
        try:
            await whatsapp_service.send_content_template(phone, content_sid=sid)
            return
        except Exception as e:
            logger.warning("pro lead-time prompt failed: %s", e)
    await whatsapp_service.send_text_message(
        phone,
        "*Step 5/7:* What's your typical lead time for standard, non-urgent jobs?\n\n"
        "Reply *Same day*, *Next 48 hours*, or *3+ days*.",
    )


async def _send_fee_prompt(phone: str) -> None:
    sid = (settings.tpl_pro_fee_sid or "").strip()
    if sid:
        try:
            await whatsapp_service.send_content_template(phone, content_sid=sid)
            return
        except Exception as e:
            logger.warning("pro fee prompt failed: %s", e)
    await whatsapp_service.send_text_message(
        phone,
        "*Step 6/7:* What is your typical service call / diagnostic fee to come out and assess a job?\n\n"
        "Reply *Free estimate*, *$50 – $99*, or *$100+*.",
    )


def _step_total(draft: dict[str, Any]) -> int:
    return 11 if needs_insurance_question(draft.get("trades") or []) else 10


async def _send_photo_estimate_prompt(phone: str, draft: dict[str, Any]) -> None:
    total = _step_total(draft)
    sid = (settings.tpl_pro_photo_estimate_sid or "").strip()
    if sid:
        try:
            await whatsapp_service.send_content_template(phone, content_sid=sid)
            return
        except Exception as e:
            logger.warning("pro photo-estimate prompt failed: %s", e)
    await whatsapp_service.send_text_message(
        phone,
        f"*Step 7 of {total}:* If a homeowner sends photos of the issue, "
        "can you give a rough estimate for free?\n\n"
        "Reply *Yes* or *No*.",
    )


async def _send_language_prompt(phone: str, draft: dict[str, Any]) -> None:
    total = _step_total(draft)
    sid = (settings.tpl_pro_language_sid or "").strip()
    if sid:
        try:
            await whatsapp_service.send_content_template(phone, content_sid=sid)
            return
        except Exception as e:
            logger.warning("pro language prompt failed: %s", e)
    await whatsapp_service.send_text_message(
        phone,
        f"*Step 8 of {total}:* Which languages do you speak with customers?\n\n"
        "Reply *English*, *Spanish*, or *Both*.",
    )


async def _send_insured_prompt(phone: str) -> None:
    sid = (settings.tpl_pro_insured_sid or "").strip()
    if sid:
        try:
            await whatsapp_service.send_content_template(phone, content_sid=sid)
            return
        except Exception as e:
            logger.warning("pro insured prompt failed: %s", e)
    await whatsapp_service.send_text_message(
        phone,
        "*Step 9 of 11:* Are you insured for the plumbing, electrical, or HVAC work you do?\n\n"
        "Reply *Yes, insured* or *Not insured*.",
    )


async def _send_notes_prompt(phone: str, draft: dict[str, Any]) -> None:
    total = _step_total(draft)
    step = 10 if needs_insurance_question(draft.get("trades") or []) else 9
    await whatsapp_service.send_text_message(
        phone,
        f"*Step {step} of {total}:* Anything else homeowners should know? "
        "Years of experience, specialties, brands you work on — a sentence or two.\n\n"
        "This helps us send you better leads. Reply with that note, or *Skip*.",
    )


async def _send_opt_in_prompt(phone: str, draft: dict[str, Any]) -> None:
    total = _step_total(draft)
    await whatsapp_service.send_text_message(
        phone,
        f"*Step {total} of {total}:* You'll receive job alerts on WhatsApp. Reply STOP anytime.\n"
        "Reply *Yes* to opt in, or *No* to cancel.",
    )


async def _advance_to_emergency(phone: str, ctx: dict[str, Any], draft: dict[str, Any]) -> None:
    ctx.update({"step": 4, "draft": draft})
    ctx.pop("area_stage", None)
    await conversation_state_store.set(phone, MODE_PRO_ONBOARDING, ctx)
    await _send_emergency_prompt(phone)


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
    """Begin the 7-step pro profile flow."""
    phone_n = normalize_phone(phone)
    await conversation_state_store.set(
        phone_n, MODE_PRO_ONBOARDING, {"step": 1, "draft": {}}
    )
    await pro_store.upsert(phone_n)
    await whatsapp_service.send_text_message(
        phone_n,
        "Welcome! Let's set up your pro profile.\n\n"
        "*Step 1/7:* What's your name and business name?\n"
        "(e.g. Mike / Mike's Plumbing)",
    )
    await log_event(
        phone=phone_n, role="pro", type="pro_onboarding_started", payload={}
    )


async def handle_finished_setup_tap(phone: str) -> None:
    """A tap on an old setup message after that step is no longer active."""
    phone_n = normalize_phone(phone)
    pro = await pro_store.get(phone_n)
    if pro and (pro.get("trades") or pro.get("name")):
        text = (
            "That was from the setup you already finished, so I left your profile as it is.\n\n"
            "Reply *PROFILE* to see it, or *JOIN* to set it up again."
        )
    else:
        text = (
            "That was from an earlier setup message, so I didn't start a new request.\n\n"
            "Reply *JOIN* to sign up as a pro."
        )
    await whatsapp_service.send_text_message(phone_n, text)
    await log_event(
        phone=phone_n,
        role="pro",
        type="stale_onboarding_tap",
        payload={"has_profile": bool(pro)},
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
        trades = format_trades(pro.get("trades")) or "—"
        areas = format_areas(pro.get("service_areas")) or ", ".join(
            pro.get("service_zips") or []
        ) or "—"
        msg = (
            f"*Your pro profile*\n"
            f"Name: {pro.get('name') or '—'}\n"
            f"Business: {pro.get('business_name') or '—'}\n"
            f"Trades: {trades}\n"
            f"Areas: {areas}\n"
            f"Urgent calls: {'yes' if pro.get('accepts_emergency') else 'no' if 'accepts_emergency' in pro else '—'}\n"
            f"Lead time: {lead_label(pro.get('standard_lead_time')) or pro.get('availability_text') or '—'}\n"
            f"Service call: {fee_label(pro.get('dispatch_fee_tier')) or pro.get('rate_text') or '—'}\n"
            f"Free photo estimate: {'yes' if pro.get('free_photo_estimate') is True else 'no' if pro.get('free_photo_estimate') is False else '—'}\n"
            f"Languages: {language_label(pro.get('languages')) or '—'}\n"
            f"Insured: {'yes' if pro.get('insured') is True else 'no' if pro.get('insured') is False else '—'}\n"
            f"About: {pro.get('profile_notes') or '—'}\n"
            f"Opted in: {'yes' if pro.get('opted_in') else 'no'}\n"
            f"Status: {pro.get('status') or '—'}\n\n"
            "Reply *DASHBOARD* to change areas, urgent calls, timing, fee, photo estimates, language, insurance, or your note. "
            "Reply *JOIN* to change your name, business, or trades. Reply *HELP* for the full menu."
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
            ctx.update({"step": 3, "area_stage": "choose", "draft": draft})
            ctx.pop("trade_stage", None)
            await conversation_state_store.set(phone_n, MODE_PRO_ONBOARDING, ctx)
            await _send_area_picker(phone_n)
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
        stage = str(ctx.get("area_stage") or "choose")
        selected = list(draft.get("service_areas") or [])
        turn = interpret_area_turn(text, payload, stage, selected)
        if turn.action == "show_list":
            ctx.update({"area_stage": "choose", "draft": draft})
            await conversation_state_store.set(phone_n, MODE_PRO_ONBOARDING, ctx)
            await _send_area_picker(phone_n)
            return True
        if turn.action == "done":
            if not selected:
                await whatsapp_service.send_text_message(
                    phone_n, "Pick at least one area before continuing."
                )
                await _send_area_picker(phone_n)
                return True
            await _advance_to_emergency(phone_n, ctx, draft)
            return True
        if turn.action == "add":
            if AREA_ALL in turn.areas:
                draft["service_areas"] = [AREA_ALL]
                draft["service_zips"] = []
                await _advance_to_emergency(phone_n, ctx, draft)
                return True
            added = [area for area in turn.areas if area not in selected and area != AREA_ALL]
            selected = [area for area in selected if area != AREA_ALL]
            if not added and not selected:
                await _send_area_picker(phone_n)
                return True
            selected.extend(added)
            draft["service_areas"] = selected
            draft["service_zips"] = zips_for_areas(selected)
            ctx.update({"area_stage": "more", "draft": draft})
            await conversation_state_store.set(phone_n, MODE_PRO_ONBOARDING, ctx)
            if not added:
                await whatsapp_service.send_text_message(
                    phone_n, "That's already on your list."
                )
            await _send_area_more(phone_n, selected)
            return True
        await whatsapp_service.send_text_message(
            phone_n,
            "Tap an area, or type one like Katy or The Woodlands.",
        )
        await _send_area_picker(phone_n)
        return True

    if step == 4:
        answer = interpret_emergency(text, payload)
        if answer is None:
            await whatsapp_service.send_text_message(
                phone_n, "Tap *Yes* if you take urgent calls, or *No* if you only do scheduled work."
            )
            await _send_emergency_prompt(phone_n)
            return True
        draft["accepts_emergency"] = answer
        ctx.update({"step": 5, "draft": draft})
        await conversation_state_store.set(phone_n, MODE_PRO_ONBOARDING, ctx)
        await _send_lead_time_prompt(phone_n)
        return True

    if step == 5:
        lead = interpret_lead_time(text, payload)
        if not lead:
            await whatsapp_service.send_text_message(
                phone_n, "Tap *Same day*, *Next 48 hours*, or *3+ days*."
            )
            await _send_lead_time_prompt(phone_n)
            return True
        draft["standard_lead_time"] = lead
        draft["availability_text"] = lead_label(lead)
        ctx.update({"step": 6, "draft": draft})
        await conversation_state_store.set(phone_n, MODE_PRO_ONBOARDING, ctx)
        await _send_fee_prompt(phone_n)
        return True

    if step == 6:
        fee = interpret_dispatch_fee(text, payload)
        if not fee:
            await whatsapp_service.send_text_message(
                phone_n, "Tap *Free estimate*, *$50 – $99*, or *$100+*."
            )
            await _send_fee_prompt(phone_n)
            return True
        draft["dispatch_fee_tier"] = fee
        draft["rate_text"] = fee_label(fee)
        ctx.update({"step": 7, "draft": draft})
        await conversation_state_store.set(phone_n, MODE_PRO_ONBOARDING, ctx)
        await _send_photo_estimate_prompt(phone_n, draft)
        return True

    if step == 7:
        answer = interpret_photo_estimate(text, payload)
        if answer is None:
            await whatsapp_service.send_text_message(
                phone_n,
                "Tap *Yes, for free* if you can price it from photos, or *No, not free* if you cannot.",
            )
            await _send_photo_estimate_prompt(phone_n, draft)
            return True
        draft["free_photo_estimate"] = answer
        ctx.update({"step": 8, "draft": draft})
        await conversation_state_store.set(phone_n, MODE_PRO_ONBOARDING, ctx)
        await _send_language_prompt(phone_n, draft)
        return True

    if step == 8:
        languages = interpret_languages(text, payload)
        if not languages:
            await whatsapp_service.send_text_message(
                phone_n, "Tap *English*, *Spanish*, or *Both*."
            )
            await _send_language_prompt(phone_n, draft)
            return True
        draft["languages"] = languages
        if needs_insurance_question(draft.get("trades") or []):
            ctx.update({"step": 9, "draft": draft})
            await conversation_state_store.set(phone_n, MODE_PRO_ONBOARDING, ctx)
            await _send_insured_prompt(phone_n)
        else:
            draft["insured"] = None
            ctx.update({"step": 10, "draft": draft})
            await conversation_state_store.set(phone_n, MODE_PRO_ONBOARDING, ctx)
            await _send_notes_prompt(phone_n, draft)
        return True

    if step == 9:
        insured = interpret_insured(text, payload)
        if insured is None:
            await whatsapp_service.send_text_message(
                phone_n, "Tap *Yes, insured* or *Not insured*."
            )
            await _send_insured_prompt(phone_n)
            return True
        draft["insured"] = insured
        ctx.update({"step": 10, "draft": draft})
        await conversation_state_store.set(phone_n, MODE_PRO_ONBOARDING, ctx)
        await _send_notes_prompt(phone_n, draft)
        return True

    if step == 10:
        note = clean_profile_note(text)
        if note is None:
            await whatsapp_service.send_text_message(
                phone_n, "Send a short note, or reply *Skip*."
            )
            await _send_notes_prompt(phone_n, draft)
            return True
        draft["profile_notes"] = note
        ctx.update({"step": 11, "draft": draft})
        await conversation_state_store.set(phone_n, MODE_PRO_ONBOARDING, ctx)
        await _send_opt_in_prompt(phone_n, draft)
        return True

    if step == 11:
        if text.lower() in {"yes", "y", "yeah", "sure"}:
            from datetime import datetime, timezone

            await pro_store.upsert(
                phone_n,
                name=draft.get("name"),
                business_name=draft.get("business_name"),
                trades=draft.get("trades") or [],
                service_areas=draft.get("service_areas") or [],
                service_zips=draft.get("service_zips") or [],
                accepts_emergency=bool(draft.get("accepts_emergency")),
                standard_lead_time=draft.get("standard_lead_time"),
                availability_text=draft.get("availability_text"),
                dispatch_fee_tier=draft.get("dispatch_fee_tier"),
                rate_text=draft.get("rate_text"),
                languages=draft.get("languages") or [],
                insured=draft.get("insured"),
                free_photo_estimate=draft.get("free_photo_estimate"),
                profile_notes=draft.get("profile_notes") or "",
                opted_in=True,
                opted_in_at=datetime.now(timezone.utc).isoformat(),
                status="active",
            )
            await conversation_state_store.clear(phone_n)
            trades = format_trades(draft.get("trades")) or "—"
            areas = format_areas(draft.get("service_areas")) or "—"
            await whatsapp_service.send_text_message(
                phone_n,
                "You're in! Here's your summary:\n"
                f"*{draft.get('business_name') or draft.get('name')}*\n"
                f"Trades: {trades}\nAreas: {areas}\n"
                f"{capacity_summary(draft.get('accepts_emergency'), draft.get('standard_lead_time'))}\n"
                f"Service call: {fee_label(draft.get('dispatch_fee_tier')) or '—'}\n"
                f"Free photo estimate: {'yes' if draft.get('free_photo_estimate') is True else 'no' if draft.get('free_photo_estimate') is False else '—'}\n"
                f"Languages: {language_label(draft.get('languages')) or '—'}\n"
                f"Insured: {'yes' if draft.get('insured') is True else 'no' if draft.get('insured') is False else '—'}\n"
                f"About: {draft.get('profile_notes') or '—'}\n\n"
                "We'll message you when a matching job comes in.\n\n"
                "Reply *DASHBOARD* for your jobs and to update areas, timing, fee, photo estimates, language, insurance, or your note. "
                "Reply *JOIN* again to change your name, business, or trades. Reply *HELP* anytime.",
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
