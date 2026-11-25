"""Select and rank opted-in pros for a job."""

from __future__ import annotations

import re
from typing import Any

from app.config import settings
from app.services.pro_capacity import FEE_SORT, fee_label
from app.services.pro_profile import insured_rank, note_overlap, pro_fit_line
from app.services.pros import pro_store


def _rate_sort_key(pro: dict[str, Any]) -> int:
    tier = str(pro.get("dispatch_fee_tier") or "")
    if tier in FEE_SORT:
        return FEE_SORT[tier]
    text = str(pro.get("rate_text") or "")
    m = re.search(r"(\d+)", text.replace(",", ""))
    return int(m.group(1)) if m else 9999


def _availability_rank(pro: dict[str, Any], *, prefer_fast: bool) -> int:
    lead = (pro.get("standard_lead_time") or "").lower()
    if lead == "same_day":
        return 0 if prefer_fast else 1
    if lead == "next_48_hours":
        return 1 if prefer_fast else 0
    if lead == "three_plus_days":
        return 2
    text = (pro.get("availability_text") or "").lower()
    if "24/7" in text or "within 1" in text or "same day" in text:
        return 0 if prefer_fast else 1
    if "next day" in text or "48" in text:
        return 1 if prefer_fast else 0
    if "week" in text or "3+" in text:
        return 2
    return 3


def filter_pros_for_severity(
    pros: list[dict[str, Any]], severity: str
) -> list[dict[str, Any]]:
    """Urgent jobs only go to pros who said they take same-day calls."""
    sev = (severity or "medium").lower()
    if sev not in {"high", "emergency"}:
        return pros
    return [pro for pro in pros if pro.get("accepts_emergency")]


def rank_pros(
    pros: list[dict[str, Any]],
    severity: str,
    *,
    trade: str = "",
    issue: str = "",
) -> list[dict[str, Any]]:
    """Insured plumbing, electrical, and HVAC pros sort first. Notes break ties."""
    sev = (severity or "medium").lower()

    def _tail(pro: dict[str, Any]) -> tuple[int, int]:
        return (insured_rank(pro, trade), -note_overlap(pro, issue))

    if sev in ("high", "emergency"):
        return sorted(
            pros,
            key=lambda p: (
                _tail(p)[0],
                _availability_rank(p, prefer_fast=True),
                -float(p.get("rating_avg") or 0),
                _tail(p)[1],
            ),
        )
    if sev == "low":
        return sorted(
            pros,
            key=lambda p: (
                _tail(p)[0],
                _rate_sort_key(p),
                -float(p.get("rating_avg") or 0),
                _tail(p)[1],
            ),
        )
    return sorted(
        pros,
        key=lambda p: (
            _tail(p)[0],
            -float(p.get("rating_avg") or 0),
            _availability_rank(p, prefer_fast=True),
            _rate_sort_key(p),
            _tail(p)[1],
        ),
    )


async def select_pros_for_job(
    *,
    trade: str,
    area_zip: str,
    severity: str,
    issue: str = "",
    max_pros: int | None = None,
) -> list[dict[str, Any]]:
    """
    Filter opted_in + active + trade + zip, rank by severity, cap at MAX_PROS_PER_JOB.
    """
    cap = max_pros or int(settings.max_pros_per_job or 4)
    candidates = await pro_store.list_matching(trade=trade, area_zip=area_zip)
    ranked = rank_pros(
        filter_pros_for_severity(candidates, severity),
        severity,
        trade=trade,
        issue=issue,
    )
    return ranked[:cap]


def public_pro_card(pro: dict[str, Any]) -> dict[str, Any]:
    """Fields safe to show before contact exchange (no phone)."""
    return {
        "name": pro.get("business_name") or pro.get("name") or "Local pro",
        "trade": (pro.get("trades") or ["?"])[0] if pro.get("trades") else "?",
        "rating": pro.get("rating_avg") or 0,
        "availability": pro.get("availability_text") or "See profile",
        "rate": fee_label(pro.get("dispatch_fee_tier")) or pro.get("rate_text") or "",
        "fit": pro_fit_line(pro),
    }
