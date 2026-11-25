"""Turn a pro's free-form reply into a quote, a question, or a follow-up."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from app.services.alert_reply import job_ref
from app.services.pro_capacity import fee_label, visit_price_label

logger = logging.getLogger(__name__)

QUOTE_READY = "QUOTE_READY"
QUESTION_ONLY = "QUESTION_ONLY"
QUOTE_WITH_QUESTION = "QUOTE_WITH_QUESTION"
MISSING_INFO = "MISSING_INFO"
_INTENTS = {QUOTE_READY, QUESTION_ONLY, QUOTE_WITH_QUESTION, MISSING_INFO}

_TIME_HINT = re.compile(
    r"\b(today|tomorrow|tonight|morning|afternoon|evening|asap|monday|tuesday|"
    r"wednesday|thursday|friday|saturday|sunday|am|pm|o'clock|available)\b",
    re.I,
)
_PRICE = re.compile(
    r"\$\s?\d+(?:\.\d+)?(?:\s*[-–]\s*\$?\s?\d+(?:\.\d+)?)?|\b\d+(?:\.\d+)?\s*dollars?\b",
    re.I,
)
_YEARS = re.compile(r"\b(\d{1,2})\s*\+?\s*years?\b", re.I)
_INSPECT = re.compile(
    r"\b("
    r"inspect(?:ion)?|look first|take a look|have a look|"
    r"see it first|need to see|have to see|come look|"
    r"on-?site|in person|diagnos(?:e|tic|is)|"
    r"before (?:i|we) (?:quote|price|can quote)|"
    r"after (?:i|we) (?:see|look|inspect|check)|"
    r"won'?t know|can'?t quote|"
    r"quoted on-?site"
    r")\b",
    re.I,
)


@dataclass
class ProReply:
    intent: str
    availability_text: str = ""
    price_estimate: str = ""
    caveats_or_notes: str = ""
    question_for_homeowner: str = ""
    missing_field_prompt: str = ""


def _clean(value: object, limit: int = 300) -> str:
    return " ".join(str(value or "").split())[:limit]


def fallback_pro_reply(message: str) -> dict[str, str]:
    """Used when Gemini is down. Quote only when a time and a price are both obvious."""
    text = _clean(message, 500)
    has_time = bool(_TIME_HINT.search(text))
    price_match = _PRICE.search(text)
    if has_time and price_match:
        return {
            "intent": QUOTE_READY,
            "availability_text": text[:200],
            "price_estimate": price_match.group(0),
            "caveats_or_notes": "",
            "question_for_homeowner": "",
            "missing_field_prompt": "",
        }
    if "?" in text and not price_match:
        return {
            "intent": QUESTION_ONLY,
            "availability_text": "",
            "price_estimate": "",
            "caveats_or_notes": "",
            "question_for_homeowner": text[:300],
            "missing_field_prompt": "",
        }
    return {
        "intent": MISSING_INFO,
        "availability_text": "",
        "price_estimate": "",
        "caveats_or_notes": "",
        "question_for_homeowner": "",
        "missing_field_prompt": (
            "I didn't catch a time and a price. Reply with when you can come "
            "and a rough estimate, or type a question for the homeowner."
        ),
    }


def _missing_prompt(availability: str, price: str) -> str:
    if availability and not price:
        return (
            "Thanks! What's a rough ballpark estimate so I can share both "
            "with the homeowner?"
        )
    if price and not availability:
        return "Thanks! When is the earliest you could come out?"
    return (
        "Reply with when you can come and a rough estimate "
        "(or type a quick question if you need more info first)."
    )


def _needs_inspection(*parts: str) -> bool:
    return bool(_INSPECT.search(" ".join(part for part in parts if part)))


def _explicit_price(text: str) -> str:
    match = _PRICE.search(text or "")
    return match.group(0) if match else ""


def _price_is_just_the_fee(price: str, pro: dict[str, Any] | None) -> bool:
    """True when this is empty or only restates the saved visit fee, not a repair price."""
    text = _clean(price).lower().replace("–", "-").replace("—", "-")
    if not text:
        return True
    key = (pro or {}).get("dispatch_fee_tier")
    known = {
        fee_label(key).lower().replace("–", "-").replace("—", "-"),
        visit_price_label(key).lower().replace("–", "-").replace("—", "-"),
        "free",
        "free estimate",
        "free in-person estimate",
    }
    return text in {item for item in known if item}


def finalize_pro_reply(
    raw: dict[str, Any] | None,
    draft: dict[str, Any] | None = None,
    *,
    pro: dict[str, Any] | None = None,
    message: str = "",
) -> ProReply:
    """Combine this message with anything the pro already sent, then pick the intent."""
    data = raw or {}
    prior = draft or {}
    availability = _clean(data.get("availability_text") or prior.get("availability_text"))
    price = _clean(data.get("price_estimate") or prior.get("price_estimate"))
    notes = _clean(data.get("caveats_or_notes") or prior.get("caveats_or_notes"))
    question = _clean(data.get("question_for_homeowner") or prior.get("question_for_homeowner"))
    text = _clean(message, 500)
    if not question and "?" in text:
        question = text

    # A time plus "I need to inspect before I quote the repair" is still a quote.
    # The saved visit fee is the price. A dollar amount they actually wrote wins.
    if (
        availability
        and _needs_inspection(text, notes)
        and not _explicit_price(text)
        and _price_is_just_the_fee(price, pro)
    ):
        visit = visit_price_label((pro or {}).get("dispatch_fee_tier"))
        if visit:
            price = visit

    if availability and price and question:
        intent = QUOTE_WITH_QUESTION
    elif availability and price:
        intent = QUOTE_READY
    elif question:
        intent = QUESTION_ONLY
    else:
        intent = MISSING_INFO

    prompt = ""
    if intent == MISSING_INFO:
        prompt = _clean(data.get("missing_field_prompt"), 240) or _missing_prompt(availability, price)
    return ProReply(
        intent=intent,
        availability_text=availability,
        price_estimate=price,
        caveats_or_notes=notes,
        question_for_homeowner=question if intent != QUOTE_READY else "",
        missing_field_prompt=prompt,
    )


def estimate_bounds(price: str) -> tuple[float | None, float | None]:
    nums = [float(item) for item in re.findall(r"\d+(?:\.\d+)?", price or "")]
    if not nums:
        return None, None
    return min(nums), max(nums)


def years_experience(pro: dict[str, Any] | None) -> str:
    data = pro or {}
    explicit = _clean(data.get("years_experience"), 8)
    if explicit.isdigit():
        return explicit
    match = _YEARS.search(str(data.get("profile_notes") or ""))
    return match.group(1) if match else ""


def experience_sentence(pro: dict[str, Any] | None) -> str:
    years = years_experience(pro)
    if years:
        return f"They have {years} years of experience"
    return "They are a verified local pro"


def card_extra(
    notes: str,
    question: str,
    pro: dict[str, Any] | None,
    name: str,
    price: str = "",
) -> str:
    parts: list[str] = []
    if notes:
        parts.append(f"Note: {notes}.")
    fee = fee_label((pro or {}).get("dispatch_fee_tier"))
    visit = visit_price_label((pro or {}).get("dispatch_fee_tier"))
    blob = f"{notes} {question} {price}".lower()
    already = fee.lower() in blob or (visit and visit.lower() in blob)
    if fee and not already:
        parts.append(f"Service call: {fee}.")
    if question:
        parts.append(f"Quick question from {name}: {question}")
    return " ".join(parts)


def quote_card_text(
    *,
    name: str,
    trade: str,
    job_id: str,
    availability: str,
    price: str,
    pro: dict[str, Any] | None,
    notes: str = "",
    question: str = "",
) -> str:
    who = name or "A pro"
    experience = experience_sentence(pro)
    lines = [
        f"*New quote for {job_ref(job_id)} ({trade or 'repair'})*",
        (
            f"*{who}* responded — {experience[0].lower()}{experience[1:]} "
            f"and are available *{availability}*."
        ),
        f"*Rough estimate:* *{price}*",
    ]
    extra = card_extra(notes, question, pro, who, price)
    if extra:
        lines.append(extra)
    return "\n".join(lines)


def button_first_name(name: str) -> str:
    first = (name or "Pro").split()[0]
    return (first or "Pro")[:10]


def job_summary(job: dict[str, Any]) -> str:
    diagnosis = job.get("diagnosis") or {}
    brief = diagnosis.get("pro_brief") or {}
    issue = brief.get("issue") or diagnosis.get("homeowner_message") or ""
    return (
        f"Trade: {job.get('trade') or diagnosis.get('trade') or 'repair'}. "
        f"Area: {job.get('area_zip') or 'Houston'}. "
        f"Issue: {_clean(issue, 300) or 'home repair'}."
    )


def profile_summary(pro: dict[str, Any] | None) -> str:
    data = pro or {}
    trades = data.get("trades") or []
    trade_text = ", ".join(str(item) for item in trades) if isinstance(trades, list) else str(trades)
    return (
        f"Name: {data.get('name') or 'Pro'}. "
        f"Business: {data.get('business_name') or '—'}. "
        f"Trades: {trade_text or '—'}. "
        f"Years: {years_experience(data) or 'not listed'}. "
        f"Service call: {fee_label(data.get('dispatch_fee_tier')) or 'not listed'}. "
        f"Notes: {_clean(data.get('profile_notes'), 200) or '—'}"
    )


async def parse_pro_job_reply(
    message: str,
    pro: dict[str, Any] | None,
    job: dict[str, Any],
    draft: dict[str, Any] | None = None,
) -> ProReply:
    """LLM router. Falls back to a safe prompt when the call fails."""
    prior = draft or {}
    try:
        from app.services.gemini import gemini_service

        raw = await gemini_service.parse_pro_job_reply(
            message=message,
            profile_summary=profile_summary(pro),
            job_summary=job_summary(job),
            prior_draft=(
                f"Time: {prior.get('availability_text') or '—'}. "
                f"Price: {prior.get('price_estimate') or '—'}. "
                f"Notes: {prior.get('caveats_or_notes') or '—'}. "
                f"Question: {prior.get('question_for_homeowner') or '—'}"
            ),
        )
    except Exception as exc:
        logger.warning("pro reply parse failed: %s", exc)
        raw = fallback_pro_reply(message)
    return finalize_pro_reply(raw, prior, pro=pro, message=message)
