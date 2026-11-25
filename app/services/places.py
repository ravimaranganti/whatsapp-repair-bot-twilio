"""Google Places Text Search when no partner pro matches a job."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import settings
from app.services.homeowner_profile import trade_phrase

logger = logging.getLogger(__name__)

_PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
# Google only accepts minRating in 0.5 steps and rounds up, so 4.6 would
# become 5.0. Ask for 4.5, then keep 4.6 and 30+ reviews in code.
_API_MIN_RATING = 4.5
_MIN_RATING = 4.6
_MIN_REVIEWS = 30
_FIELD_MASK = ",".join(
    [
        "places.displayName",
        "places.formattedAddress",
        "places.nationalPhoneNumber",
        "places.rating",
        "places.userRatingCount",
        "places.googleMapsUri",
    ]
)


def dispatch_confirmation(count: int, trade: str, zip_code: str) -> str:
    """Confidence copy. A small match does not reveal the exact count."""
    label = trade_phrase(trade)
    area = (zip_code or "").strip() or "your area"
    if count >= 5:
        return (
            f"On it! Reaching out to {count} neighbor-referred {label} pros near {area}"
            "—quotes usually start coming in within the hour."
        )
    return (
        f"On it! Checking availability with a few trusted, neighbor-referred {label} pros "
        f"serving {area}—I'll message you as soon as a quote comes in."
    )


def short_address(formatted: str) -> str:
    parts = [part.strip() for part in (formatted or "").split(",") if part.strip()]
    parts = [part for part in parts if part.lower() not in {"usa", "united states"}]
    if len(parts) >= 2:
        return ", ".join(parts[:2])
    return (formatted or "").strip()


def _place_score(place: dict[str, Any]) -> tuple[float, int]:
    try:
        rating = float(place.get("rating") or 0)
    except (TypeError, ValueError):
        rating = 0.0
    try:
        reviews = int(place.get("userRatingCount") or 0)
    except (TypeError, ValueError):
        reviews = 0
    return rating, reviews


def specialists_from_places(payload: dict[str, Any] | None, *, limit: int = 3) -> list[dict[str, Any]]:
    """Top 3 places at 4.6 stars or better, with 30+ reviews and a phone number."""
    places = [place for place in ((payload or {}).get("places") or []) if isinstance(place, dict)]
    qualified = [
        place
        for place in places
        if _place_score(place)[0] >= _MIN_RATING and _place_score(place)[1] >= _MIN_REVIEWS
    ]
    found: list[dict[str, Any]] = []
    for place in sorted(qualified, key=_place_score, reverse=True):
        phone = str(place.get("nationalPhoneNumber") or "").strip()
        display = place.get("displayName") or {}
        if isinstance(display, dict):
            name = str(display.get("text") or "").strip()
        else:
            name = str(display).strip()
        if not name or not phone:
            continue
        address = short_address(str(place.get("formattedAddress") or ""))
        found.append(
            {
                "name": name[:120],
                "phone": phone[:40],
                "address": address[:160],
                "rating": place.get("rating"),
                "reviews": place.get("userRatingCount") or 0,
                "maps_uri": str(place.get("googleMapsUri") or "")[:300],
            }
        )
        if len(found) >= limit:
            break
    return found


def _rating_label(place: dict[str, Any]) -> str:
    rating = place.get("rating")
    try:
        reviews = int(place.get("reviews") or 0)
    except (TypeError, ValueError):
        reviews = 0
    if rating in (None, ""):
        return f"no public rating yet · {reviews} reviews"
    try:
        shown = f"{float(rating):.1f}"
    except (TypeError, ValueError):
        return f"no public rating yet · {reviews} reviews"
    return f"{shown}★ · {reviews} reviews"


def places_offer_message(trade: str, zip_code: str, places: list[dict[str, Any]]) -> str:
    label = trade_phrase(trade)
    area = (zip_code or "").strip() or "your area"
    lines = [
        (
            f"I don't have a friend- or neighbor-referred {label} pro on standby in {area} "
            "just yet—so while I ask around my local network, here are the top-rated "
            "local specialists on Google (4.6★+ with 30+ reviews):"
        ),
        "",
    ]
    for index, place in enumerate(places[:3], start=1):
        lines.append(f"{index}. *{place.get('name') or 'Local pro'}* ({_rating_label(place)})")
        lines.append(f"   📞 {place.get('phone') or 'phone not listed'}")
        lines.append(f"   📍 {place.get('address') or area}")
        lines.append("")
    lines.append(
        f"I've also flagged this specialty so we can add a vetted {label} pro to our roster soon!"
    )
    return "\n".join(lines).strip()


def no_partner_message(trade: str, zip_code: str) -> str:
    label = trade_phrase(trade)
    area = (zip_code or "").strip() or "your area"
    return (
        f"I don't have a neighbor-referred {label} pro on standby in {area} "
        "just yet—give me a little time to ask around my local network and find someone solid for you!"
    )


async def search_local_specialists(trade: str, zip_code: str) -> list[dict[str, Any]]:
    """Places API (New) text search. Empty when the key is missing or the call fails."""
    key = (settings.google_places_api_key or "").strip()
    area = (zip_code or "").strip()
    if not key or not area:
        return []
    query = f"{trade_phrase(trade)} near {area}, TX"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                _PLACES_URL,
                json={
                    "textQuery": query,
                    "pageSize": 10,
                    "minRating": _API_MIN_RATING,
                    "regionCode": "US",
                },
                headers={
                    "Content-Type": "application/json",
                    "X-Goog-Api-Key": key,
                    "X-Goog-FieldMask": _FIELD_MASK,
                },
                timeout=20.0,
            )
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        logger.warning("places lookup failed: %s", exc)
        return []
    return specialists_from_places(payload if isinstance(payload, dict) else {})
