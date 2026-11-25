"""Extra pro profile fields: language, insurance, and a free-form note."""

from __future__ import annotations

import re

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
