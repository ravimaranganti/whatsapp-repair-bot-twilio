"""Extra pro profile fields: language, insurance, and a free-form note."""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

LICENSED_TRADES = frozenset({"plumbing", "electrical", "hvac"})

LANG_EN = "lang_en"
LANG_ES = "lang_es"
LANG_BOTH = "lang_both"
INSURED_YES = "insured_yes"
INSURED_NO = "insured_no"

_SKIP_NOTES = frozenset(
    {"skip", "none", "nothing", "n/a", "na", "no thanks", "no note", "pass"}
)


def _norm(text: str) -> str:
    return " ".join((text or "").strip().lower().replace("’", "'").split())


def needs_insurance_question(trades: list[str] | None) -> bool:
    return any(str(trade) in LICENSED_TRADES for trade in (trades or []))


def interpret_languages(text: str, payload: str = "") -> list[str] | None:
    pl = (payload or "").strip().lower()
    if pl == LANG_BOTH:
        return ["en", "es"]
    if pl == LANG_EN:
        return ["en"]
    if pl == LANG_ES:
        return ["es"]
    phrase = _norm(text)
    if phrase in {"both", "english and spanish", "spanish and english", "bilingual"}:
        return ["en", "es"]
    if phrase in {"english", "en", "ingles", "inglés"}:
        return ["en"]
    if phrase in {"spanish", "es", "espanol", "español"}:
        return ["es"]
    return None


def language_label(languages: list[str] | None) -> str:
    chosen = set(languages or [])
    has_en = "en" in chosen
    has_es = "es" in chosen
    if has_en and has_es:
        return "English and Spanish"
    if has_es:
        return "Spanish"
    if has_en:
        return "English"
    return ""


def interpret_photo_estimate(text: str, payload: str = "") -> bool | None:
    """Yes means a rough price from photos is free. None means the reply was unclear."""
    pl = (payload or "").strip().lower()
    if pl == "photo_est_yes":
        return True
    if pl == "photo_est_no":
        return False
    phrase = _norm(text).replace(",", "")
    if phrase in {"yes", "y", "yeah", "sure", "yes for free", "free"}:
        return True
    if phrase in {"no", "nope", "not free"}:
        return False
    return None


def interpret_insured(text: str, payload: str = "") -> bool | None:
    pl = (payload or "").strip().lower()
    if pl == INSURED_YES:
        return True
    if pl == INSURED_NO:
        return False
    phrase = _norm(text).replace(",", "")
    if phrase in {"yes", "y", "yeah", "insured", "yes insured"}:
        return True
    if phrase in {"no", "nope", "not insured", "uninsured"}:
        return False
    return None


_NAME_INTRO = re.compile(
    r"^(?:hi|hey|hello)[,!]?\s+|"
    r"^(?:i am|i'm|im|this is|my name is|it's|its)\s+",
    re.IGNORECASE,
)
_NAME_SEPARATORS = (
    " / ",
    "/",
    " from ",
    " at ",
    " @ ",
    " — ",
    "—",
    " – ",
    "–",
    " - ",
    " | ",
    ", ",
)
_NAME_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv"})


def split_pro_name(text: str) -> tuple[str, str] | None:
    """
    (contact name, business name) from a plain reply.

    A separator such as "from", "at", or "/" sets the business. A name on its
    own leaves the business empty, which means they work solo.
    """
    raw = " ".join((text or "").split())
    raw = _NAME_INTRO.sub("", raw).strip(" ,")
    if not raw:
        return None
    lowered = raw.lower()
    for sep in _NAME_SEPARATORS:
        at = lowered.find(sep)
        if at <= 0:
            continue
        contact = raw[:at].strip(" -|,.")
        business = raw[at + len(sep):].strip(" -|,.")
        if sep == ", " and business.lower().rstrip(".") in _NAME_SUFFIXES:
            continue
        if contact:
            return contact[:80], business[:80]
    return raw[:80], ""


def _name_needs_model(text: str, business: str) -> bool:
    """A short reply with a clear separator does not need the model."""
    if business:
        return False
    words = text.split()
    if len(words) > 4:
        return True
    return bool(re.search(r"\b(and|run|company|business|called|named)\b", text, re.I))


async def read_pro_name(text: str) -> tuple[str, str] | None:
    """
    Contact name and business name.

    A slash, "from", "at", or a dash is parsed directly. A longer sentence
    goes through Gemini structured output, then the same fallback.
    A solo name is stored as the business name too.
    """
    raw = " ".join((text or "").split())
    if not raw:
        return None
    simple = split_pro_name(raw)
    if simple and _name_needs_model(raw, simple[1]):
        modeled = await _model_pro_name(raw)
        if modeled:
            return modeled
    if not simple or not simple[0].strip():
        return None
    contact, business = simple
    return contact, business or contact


async def _model_pro_name(text: str) -> tuple[str, str] | None:
    try:
        from app.services.gemini import gemini_service

        data = await gemini_service.parse_pro_name(text)
    except Exception as exc:
        logger.warning("pro name parse failed: %s", exc)
        return None
    if not isinstance(data, dict):
        return None
    contact = " ".join(str(data.get("contact_name") or "").split())[:80]
    business = " ".join(str(data.get("business_name") or "").split())[:80]
    if not contact:
        return None
    return contact, business or contact


def clean_profile_note(text: str) -> str | None:
    """Return a stored note, '' when they skip, or None when the reply is empty."""
    raw = " ".join((text or "").split())
    if not raw:
        return None
    if _norm(raw) in _SKIP_NOTES:
        return ""
    return raw[:500]


def pro_fit_line(pro: dict | None) -> str:
    """Short homeowner-facing facts. Insurance is mentioned only when they said yes."""
    data = pro or {}
    parts: list[str] = []
    label = language_label(data.get("languages"))
    if label:
        parts.append(label)
    if data.get("insured") is True:
        parts.append("Insured")
    if data.get("free_photo_estimate") is True:
        parts.append("Free photo estimate")
    return " · ".join(parts)


def note_overlap(pro: dict | None, issue: str) -> int:
    """How many meaningful words from the job show up in the pro's free-form note."""
    notes = str((pro or {}).get("profile_notes") or "").lower()
    if not notes or not issue:
        return 0
    words = [word for word in re.findall(r"[a-z0-9]+", issue.lower()) if len(word) > 3]
    return sum(1 for word in dict.fromkeys(words) if word in notes)


def insured_rank(pro: dict | None, trade: str) -> int:
    """0 sorts first. Insurance only changes order for plumbing, electrical, and HVAC."""
    if trade not in LICENSED_TRADES:
        return 0
    return 0 if (pro or {}).get("insured") is True else 1
