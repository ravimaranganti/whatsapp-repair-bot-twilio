"""Select and rank opted-in pros for a job."""

from __future__ import annotations

import re
from typing import Any

from app.config import settings
from app.services.pros import pro_store


def _rate_sort_key(pro: dict[str, Any]) -> int:
    text = str(pro.get("rate_text") or "")
    m = re.search(r"(\d+)", text.replace(",", ""))
    return int(m.group(1)) if m else 9999


def _availability_rank(pro: dict[str, Any], *, prefer_fast: bool) -> int:
    text = (pro.get("availability_text") or "").lower()
    if "24/7" in text or "within 1" in text or "same day" in text:
        return 0 if prefer_fast else 1
    if "next day" in text:
        return 1 if prefer_fast else 0
    if "week" in text:
        return 2
    return 3


def rank_pros(pros: list[dict[str, Any]], severity: str) -> list[dict[str, Any]]:
    sev = (severity or "medium").lower()
    if sev in ("high", "emergency"):
        return sorted(
            pros,
            key=lambda p: (
                _availability_rank(p, prefer_fast=True),
                -float(p.get("rating_avg") or 0),
            ),
        )
    if sev == "low":
        return sorted(
            pros,
            key=lambda p: (
                _rate_sort_key(p),
                -float(p.get("rating_avg") or 0),
            ),
        )
    # medium: rating then availability
    return sorted(
        pros,
        key=lambda p: (
            -float(p.get("rating_avg") or 0),
            _availability_rank(p, prefer_fast=True),
            _rate_sort_key(p),
        ),
    )


async def select_pros_for_job(
    *,
    trade: str,
    area_zip: str,
    severity: str,
    max_pros: int | None = None,
) -> list[dict[str, Any]]:
    """
    Filter opted_in + active + trade + zip, rank by severity, cap at MAX_PROS_PER_JOB.
    """
    cap = max_pros or int(settings.max_pros_per_job or 4)
    candidates = await pro_store.list_matching(trade=trade, area_zip=area_zip)
    ranked = rank_pros(candidates, severity)
    return ranked[:cap]


def public_pro_card(pro: dict[str, Any]) -> dict[str, Any]:
    """Fields safe to show before contact exchange (no phone)."""
    return {
        "name": pro.get("business_name") or pro.get("name") or "Local pro",
        "trade": (pro.get("trades") or ["?"])[0] if pro.get("trades") else "?",
        "rating": pro.get("rating_avg") or 0,
        "availability": pro.get("availability_text") or "See profile",
        "rate": pro.get("rate_text") or "",
    }
