"""How fast a pro usually works. This is a routing filter, not a live calendar.

Urgent jobs only ping pros who said they take same-day calls. Everyone else
still gets a real-time 'can you take this?' message when a matching job comes in.
"""

from __future__ import annotations

EMERG_YES = "emerg_yes"
EMERG_NO = "emerg_no"
LEAD_SAME = "lead_same_day"
LEAD_48 = "lead_48h"
LEAD_3 = "lead_3plus"

LEAD_LABELS = {
    "same_day": "Same day",
    "next_48_hours": "Next 48 hours",
    "three_plus_days": "3+ days out",
}

_YES = frozenset({"yes", "y", "yeah", "yep", "sure", "same day", "same-day", "urgent"})
_NO = frozenset({"no", "nope", "nah", "scheduled", "scheduled only"})


def _norm(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def interpret_emergency(text: str, payload: str = "") -> bool | None:
    pl = (payload or "").strip().lower()
    if pl == EMERG_YES:
        return True
    if pl == EMERG_NO:
        return False
    phrase = _norm(text)
    if phrase in _YES:
        return True
    if phrase in _NO:
        return False
    return None


def interpret_lead_time(text: str, payload: str = "") -> str | None:
    pl = (payload or "").strip().lower()
    if pl == LEAD_SAME:
        return "same_day"
    if pl == LEAD_48:
        return "next_48_hours"
    if pl == LEAD_3:
        return "three_plus_days"
    phrase = _norm(text)
    if phrase in {"same day", "same-day", "today", "same day."}:
        return "same_day"
    if phrase in {"next 48 hours", "48 hours", "48", "two days", "next 48"}:
        return "next_48_hours"
    if phrase in {"3+ days", "3+ days out", "3 days", "three days", "within a week", "next week"}:
        return "three_plus_days"
    return None


def lead_label(key: str | None) -> str:
    return LEAD_LABELS.get(key or "", key or "")


def capacity_summary(accepts_emergency: bool | None, lead_time: str | None) -> str:
    urgent = "yes" if accepts_emergency else "no"
    return f"Urgent calls: {urgent}. Typical lead time: {lead_label(lead_time) or '—'}."


FEE_FREE = "fee_free"
FEE_50 = "fee_50_99"
FEE_100 = "fee_100_plus"

# Lower numbers sort as cheaper on low-urgency jobs.
FEE_SORT = {
    "free_estimate": 0,
    "tier_50_99": 75,
    "tier_100_plus": 150,
}

FEE_LABELS = {
    "free_estimate": "Free estimate",
    "tier_50_99": "$50–$99 service call fee",
    "tier_100_plus": "$100+ service call fee",
}

# Shown as the quote price when the pro will inspect before pricing the repair.
VISIT_PRICE_LABELS = {
    "free_estimate": "Free in-person estimate",
    "tier_50_99": "$50–$99 diagnostic fee, repair quoted on-site",
    "tier_100_plus": "$100+ diagnostic fee, repair quoted on-site",
}

_FEE_PHRASES = {
    "free estimate": "free_estimate",
    "free": "free_estimate",
    "$50 - $99 fee": "tier_50_99",
    "$50-$99 fee": "tier_50_99",
    "$50-$99": "tier_50_99",
    "$50-$99 service call fee": "tier_50_99",
    "$100+ fee": "tier_100_plus",
    "$100+": "tier_100_plus",
    "$100+ service call fee": "tier_100_plus",
}


def interpret_dispatch_fee(text: str, payload: str = "") -> str | None:
    pl = (payload or "").strip().lower()
    if pl == FEE_FREE:
        return "free_estimate"
    if pl == FEE_50:
        return "tier_50_99"
    if pl == FEE_100:
        return "tier_100_plus"
    phrase = _norm(text).replace("–", "-").replace("—", "-")
    return _FEE_PHRASES.get(phrase)


def fee_label(key: str | None) -> str:
    return FEE_LABELS.get(key or "", "")


def visit_price_label(key: str | None) -> str:
    """Price line for a visit when the repair itself is quoted after inspection."""
    return VISIT_PRICE_LABELS.get(key or "", "")
