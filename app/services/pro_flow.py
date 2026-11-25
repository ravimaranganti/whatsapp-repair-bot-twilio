"""Two-screen WhatsApp Flow for pro trades and service areas.

Option ids come from the onboarding lists. "other" is the chat word for the
existing Other follow-up (the old list button id is still trade:other).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.services.onboarding import (
    TRADE_CHOICES,
    TRADE_LABELS,
    _TRADE_REDIRECTS,
    _leftover_custom,
    known_trades_in_text,
)
from app.services.service_areas import AREA_ALL, AREA_CHOICES, AREA_LABELS, zips_for_areas

FLOW_NAME = "pro_onboarding_profile_flow"
TRADE_PAGE = "SELECT_TRADES"
ZONE_PAGE = "SELECT_ZONES"
OTHER_TRADE = "other"
_OTHER_IDS = frozenset({OTHER_TRADE, "trade:other"})
_CHOICE_KEYS = {key for key, _label in TRADE_CHOICES}

TRADE_OPTIONS: list[tuple[str, str]] = [*TRADE_CHOICES, (OTHER_TRADE, "Other")]
ZONE_OPTIONS: list[tuple[str, str]] = [(key, label) for key, label, _note in AREA_CHOICES]


def _option_json(options: list[tuple[str, str]]) -> str:
    return json.dumps([{"id": key, "title": label} for key, label in options], separators=(",", ":"))


def flow_pages() -> list[dict[str, Any]]:
    """Static twilio/flows pages. MULTIPLE_SELECT is the in-form checkbox group."""
    return [
        {
            "id": TRADE_PAGE,
            "next_page_id": ZONE_PAGE,
            "title": "Your Services",
            "layout": [
                {
                    "type": "TEXT_SUBHEADING",
                    "text": "Which services do you offer? (Select all that apply)",
                },
                {
                    "type": "TEXT_CAPTION",
                    "text": "Handyman includes painting. Other covers pool, lawn, sprinkler, or another trade.",
                },
                {
                    "type": "MULTIPLE_SELECT",
                    "name": "selected_trades",
                    "label": "Services",
                    "text": "Select all that apply",
                    "required": True,
                    "options": _option_json(TRADE_OPTIONS),
                },
                {"type": "FOOTER", "label": "Next: Service Areas"},
            ],
        },
        {
            "id": ZONE_PAGE,
            "title": "Service Coverage",
            "layout": [
                {
                    "type": "TEXT_SUBHEADING",
                    "text": "Which areas do you cover? (Select all that apply)",
                },
                {
                    "type": "MULTIPLE_SELECT",
                    "name": "selected_zones",
                    "label": "Areas",
                    "text": "Select all that apply",
                    "required": True,
                    "options": _option_json(ZONE_OPTIONS),
                },
                {"type": "FOOTER", "label": "Save Profile"},
            ],
        },
    ]


def flow_content_body() -> dict[str, Any]:
    return {
        "friendly_name": FLOW_NAME,
        "language": "en",
        "types": {
            "twilio/flows": {
                "body": (
                    "Choose the services you offer and the Greater Houston areas you cover. "
                    "You can select more than one."
                ),
                "button_text": "Choose services",
                "subtitle": "Trades and service areas",
                "type": "SIGN_UP",
                "pages": flow_pages(),
            }
        },
    }


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                return _as_list(parsed)
        return [part.strip() for part in text.split(",") if part.strip()]
    return []


def _unwrap(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            return None
    if not isinstance(raw, dict):
        return None
    reply = raw.get("nfm_reply") or raw.get("nfmReply")
    if isinstance(reply, dict):
        nested = reply.get("response_json") or reply.get("responseJson")
        if nested:
            return _unwrap(nested)
    return raw


def _map_token(token: str, labels: dict[str, str]) -> str:
    key = token.strip().lower().replace(" ", "_")
    if key in labels or key in _OTHER_IDS:
        return key
    for known, label in labels.items():
        if token.strip().lower() == label.lower():
            return known
    return token.strip().lower()


@dataclass
class ProfilePick:
    trades: list[str] = field(default_factory=list)
    zones: list[str] = field(default_factory=list)
    wants_other: bool = False
    is_flow: bool = False

    @property
    def ok(self) -> bool:
        return self.is_flow and bool(self.zones) and (bool(self.trades) or self.wants_other)


def parse_flow_submission(raw: Any) -> ProfilePick | None:
    """Read a Flow reply. None when the payload is not this form."""
    data = _unwrap(raw)
    if not data:
        return None
    trades_raw: list[str] = []
    zones_raw: list[str] = []
    saw_flow = False
    if "selected_trades" in data or "selected_zones" in data:
        saw_flow = True
        trades_raw.extend(_as_list(data.get("selected_trades")))
        zones_raw.extend(_as_list(data.get("selected_zones")))
    pages = data.get("pages") or []
    if isinstance(pages, list):
        for page in pages:
            if not isinstance(page, dict):
                continue
            page_id = str(page.get("id") or page.get("page_id") or page.get("pageId") or "")
            if page_id not in {TRADE_PAGE, ZONE_PAGE}:
                continue
            saw_flow = True
            values: list[str] = []
            items = page.get("items") or page.get("layout") or []
            if isinstance(items, dict):
                items = [items]
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        name = str(item.get("name") or "")
                        if name == "selected_trades":
                            trades_raw.extend(_as_list(item.get("value")))
                            continue
                        if name == "selected_zones":
                            zones_raw.extend(_as_list(item.get("value")))
                            continue
                        values.extend(_as_list(item.get("value")))
                    elif isinstance(item, str):
                        values.append(item)
            if page_id == TRADE_PAGE:
                trades_raw.extend(values)
            else:
                zones_raw.extend(values)
    if not saw_flow:
        return None

    trades: list[str] = []
    wants_other = False
    for token in trades_raw:
        key = _map_token(token, TRADE_LABELS)
        key = _TRADE_REDIRECTS.get(key, key)
        if key in _OTHER_IDS or key == OTHER_TRADE:
            wants_other = True
            continue
        if key in TRADE_LABELS and key not in trades:
            trades.append(key)
    zones: list[str] = []
    for token in zones_raw:
        key = _map_token(token, AREA_LABELS)
        if key in AREA_LABELS and key not in zones:
            zones.append(key)
    if AREA_ALL in zones:
        zones = [AREA_ALL]
    return ProfilePick(trades=trades, zones=zones, wants_other=wants_other, is_flow=True)


def zones_for_storage(zones: list[str]) -> tuple[list[str], list[str]]:
    chosen = [zone for zone in zones if zone in AREA_LABELS]
    if AREA_ALL in chosen:
        return [AREA_ALL], []
    return chosen, zips_for_areas(chosen)


def normalize_saved_trades(raw: Any, custom_text: str) -> tuple[list[str], str] | str:
    """Checkbox keys plus the Other sentence. Returns an error sentence when empty."""
    incoming = raw if isinstance(raw, list) else ([raw] if raw else [])
    chosen: list[str] = []
    wants_other = False
    for item in incoming:
        key = _TRADE_REDIRECTS.get(str(item).strip().lower(), str(item).strip().lower())
        if key in _OTHER_IDS:
            wants_other = True
            continue
        if key in TRADE_LABELS and key not in chosen:
            chosen.append(key)
    description = " ".join((custom_text or "").split())[:80]
    if description:
        known = known_trades_in_text(description)
        leftover = _leftover_custom(description, known)
        for key in known:
            mapped = _TRADE_REDIRECTS.get(key, key)
            if mapped not in chosen:
                chosen.append(mapped)
        if leftover and leftover not in chosen:
            chosen.append(leftover)
        if not leftover and not known:
            return "Tell us the other trade in a few words."
    elif wants_other:
        return "Tell us the other trade in a few words."
    if not chosen:
        return "Choose at least one trade."
    extras = [trade for trade in chosen if trade not in _CHOICE_KEYS]
    if not extras:
        description = ""
    elif not description:
        description = ", ".join(TRADE_LABELS.get(trade, trade) for trade in extras)
    return chosen, description


def trade_form_state(trades: list[Any], description: str) -> dict[str, Any]:
    selected: list[str] = []
    extras: list[str] = []
    for trade in trades or []:
        key = _TRADE_REDIRECTS.get(str(trade), str(trade))
        if key in _CHOICE_KEYS:
            if key not in selected:
                selected.append(key)
        elif key and key not in extras:
            extras.append(key)
    stored = " ".join((description or "").split())
    if not stored and extras:
        stored = ", ".join(TRADE_LABELS.get(trade, trade) for trade in extras)
    return {
        "trade_choices": [
            {
                "key": key,
                "label": label,
                "note": "Includes painting" if key == "handyman" else "",
            }
            for key, label in TRADE_CHOICES
        ],
        "selected_trades": selected,
        "custom_trade_description": stored,
    }
