"""ZIP, first name, and visit details captured during a homeowner request."""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_ZIP = re.compile(r"\b(\d{5})(?:-\d{4})?\b")
_INTRO = re.compile(
    r"^(?:hi|hey|hello)[,!]?\s+|(?:my name is|i am|i'm|im|this is|name is|it's|its)\s+",
    re.IGNORECASE,
)
_NAME_NOISE = frozenset(
    {
        "zip",
        "zipcode",
        "code",
        "my",
        "name",
        "is",
        "i'm",
        "im",
        "this",
        "the",
        "and",
        "in",
        "at",
        "near",
        "from",
        "a",
        "for",
        "please",
        "its",
        "it's",
    }
)
_ZIP_FILLER = _NAME_NOISE | frozenset(
    {
        "different",
        "new",
        "change",
        "changed",
        "use",
        "actually",
        "instead",
        "area",
        "that",
        "to",
    }
)
_YES = frozenset(
    {"yes", "y", "yeah", "ok", "okay", "correct", "that's right", "thats right"}
)
_SKIP = frozenset(
    {"skip", "none", "no gate", "no code", "n/a", "na", "nothing", "no notes"}
)

TRADE_PHRASE = {
    "plumbing": "plumbing",
    "hvac": "HVAC",
    "electrical": "electrical",
    "handyman": "handyman",
    "appliance": "appliance",
    "painting": "painting",
    "general_contractor": "general contractor",
    "pool": "pool",
    "lawn": "lawn",
    "sprinkler": "sprinkler",
    "mobile_auto": "mobile mechanic",
    "cleaning": "cleaning",
    "other": "repair",
}


def trade_phrase(trade: str) -> str:
    key = (trade or "").strip().lower()
    return TRADE_PHRASE.get(key, key.replace("_", " ") or "repair")


def saved_zip(homeowner: dict[str, Any] | None) -> str:
    data = homeowner or {}
    return str(data.get("zip_code") or data.get("area_zip") or "").strip()


def zip_reply_effect(previous: str, changed: str, state: str | None) -> str:
    """How a ZIP-only reply should affect an existing request.

    continue — same ZIP, so the chat should keep going instead of dead-ending.
    update_job — the request has not gone out yet.
    already_out — a different ZIP arrived after pros were already contacted.
    save_only — remember it and let the chat respond.
    """
    if state in {"diagnosed", "awaiting_consent"}:
        return "update_job"
    if state in {"dispatched", "awaiting_pick"} and previous and previous != changed:
        return "already_out"
    if previous == changed:
        return "continue"
    return "save_only"


def chat_profile_note(homeowner: dict[str, Any] | None, job: dict[str, Any] | None) -> str:
    """Facts the chat needs so it does not re-ask for a ZIP or ignore an open request."""
    data = homeowner or {}
    zip_code = saved_zip(data)
    lines = [f"Saved ZIP: {zip_code or 'none'}"]
    name = saved_first_name(data)
    if name:
        lines.append(f"First name: {name}")
    if job:
        lines.append(
            "Active request: "
            f"{job.get('trade') or 'repair'} ({job.get('state') or 'open'}) "
            f"near {job.get('area_zip') or zip_code or 'unknown ZIP'}"
        )
    else:
        lines.append("Active request: none")
    return "\n".join(lines)


def saved_first_name(homeowner: dict[str, Any] | None) -> str:
    data = homeowner or {}
    first = " ".join(str(data.get("first_name") or "").split())
    if first:
        return first.split()[0][:40]
    name = " ".join(str(data.get("name") or "").split())
    return name.split()[0][:40] if name else ""


def first_time_zip_prompt(trade: str) -> str:
    label = trade_phrase(trade)
    return (
        f"Got it—I can check with local {label} pros for you. "
        "What is your *5-digit ZIP code* (and first name) so I match pros in your area?"
    )


def returning_zip_message(first_name: str, trade: str, zip_code: str) -> str:
    label = trade_phrase(trade)
    who = (first_name or "").strip()
    hello = f"On it, {who}!" if who else "On it!"
    return (
        f"{hello} Checking with local {label} pros near *{zip_code}* "
        "(just reply if this is for a different ZIP)."
    )


def parse_zip_and_name(text: str) -> tuple[str, str]:
    """5-digit ZIP and an optional first name. Empty strings when missing."""
    raw = " ".join((text or "").split())
    found = _ZIP.search(raw)
    if not found:
        return "", ""
    zip_code = found.group(1)
    rest = f"{raw[:found.start()]} {raw[found.end():]}"
    rest = _INTRO.sub("", rest).strip(" ,.-")
    first = ""
    for token in rest.split():
        word = token.strip(".,!?;:()\"'")
        if len(word) < 2 or word.lower() in _NAME_NOISE or word.isdigit():
            continue
        first = word[:40]
        break
    return zip_code, first


def zip_only_update(text: str) -> str:
    """A reply that only changes the ZIP, not a new job description."""
    zip_code, first = parse_zip_and_name(text)
    if not zip_code:
        return ""
    rest = _ZIP.sub(" ", text or "")
    words = [word.strip(".,!?;:()\"'").lower() for word in rest.split()]
    words = [word for word in words if word]
    allowed = set(_ZIP_FILLER)
    if first:
        allowed.add(first.lower())
    if any(word not in allowed for word in words):
        return ""
    return zip_code


def parse_visit_reply(text: str) -> dict[str, Any]:
    """Yes, skip, or a street address plus optional gate notes."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    joined = " ".join(lines)
    low = joined.lower().strip(" .!")
    if low in _YES:
        return {"confirm": True}
    if low in _SKIP:
        return {"skip_notes": True}
    if len(lines) >= 2:
        return {
            "street_address": lines[0][:200],
            "saved_access_notes": " ".join(lines[1:])[:500],
        }
    gate = re.split(r",|\bgate\b|\bcode\b", joined, maxsplit=1, flags=re.IGNORECASE)
    if len(gate) == 2 and re.search(r"\d", gate[0]) and gate[1].strip():
        return {
            "street_address": gate[0].strip(" ,")[:200],
            "saved_access_notes": joined[len(gate[0]):].strip(" ,")[:500],
        }
    if re.search(r"\d", joined):
        return {"street_address": joined[:200]}
    if joined:
        return {"saved_access_notes": joined[:500]}
    return {}


def visit_details_prompt(pro_name: str, street: str, notes: str) -> str:
    who = pro_name or "the pro"
    if street or notes:
        return (
            f"I'll send {who} this visit info:\n"
            f"Address: *{street or '—'}*\n"
            f"Access notes: *{notes or 'none'}*\n\n"
            "Reply *Yes* to use this, or send the updated street address and gate code."
        )
    return (
        f"What's the street address for this visit, and any gate code or access notes "
        f"for {who}? Reply *Skip* if there's no gate code — I still need the street address."
    )


async def read_zip_and_name(text: str) -> tuple[str, str]:
    """Regex first. Gemini only when the reply is a sentence the regex cannot split."""
    raw = " ".join((text or "").split())
    zip_code, first = parse_zip_and_name(raw)
    needs_model = (not zip_code and len(raw.split()) > 2) or (
        bool(zip_code) and not first and len(raw.split()) > 6
    )
    if needs_model:
        modeled = await _model_zip_and_name(raw)
        if modeled and modeled[0]:
            return modeled
    return zip_code, first


async def _model_zip_and_name(text: str) -> tuple[str, str] | None:
    try:
        from app.services.gemini import gemini_service

        data = await gemini_service.parse_homeowner_intro(text)
    except Exception as exc:
        logger.warning("homeowner zip parse failed: %s", exc)
        return None
    if not isinstance(data, dict):
        return None
    zip_code = ""
    found = _ZIP.search(str(data.get("zip_code") or ""))
    if found:
        zip_code = found.group(1)
    first = " ".join(str(data.get("first_name") or "").split())
    first = first.split()[0][:40] if first else ""
    if not zip_code:
        return None
    return zip_code, first


async def remember_homeowner(phone: str, **fields: Any) -> None:
    """Write profile fields. A ZIP also refreshes area_zip and default_zone."""
    from app.services.homeowners import homeowner_store
    from app.services.service_areas import area_for_zip

    payload: dict[str, Any] = {}
    zip_code = str(fields.get("zip_code") or "").strip()
    if zip_code:
        payload["zip_code"] = zip_code
        payload["area_zip"] = zip_code
        payload["default_zone"] = area_for_zip(zip_code)
    first = " ".join(str(fields.get("first_name") or "").split())
    if first:
        payload["first_name"] = first.split()[0][:40]
        existing = await homeowner_store.get(phone)
        if not str((existing or {}).get("name") or "").strip():
            payload["name"] = payload["first_name"]
    if "street_address" in fields:
        payload["street_address"] = " ".join(str(fields.get("street_address") or "").split())[:200]
    if "saved_access_notes" in fields:
        payload["saved_access_notes"] = " ".join(
            str(fields.get("saved_access_notes") or "").split()
        )[:500]
    if not payload:
        return
    await homeowner_store.ensure(phone)
    await homeowner_store.update(phone, **payload)
