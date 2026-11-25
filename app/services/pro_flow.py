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
        if isinstance(raw, str):
            return _unwrap(raw)
    if isinstance(raw, list):
        return {"pages": raw}
    if not isinstance(raw, dict):
        return None
    for key in ("interactive", "nfm_reply", "nfmReply", "flow_data", "FlowData"):
        nested = raw.get(key)
        if isinstance(nested, (dict, str)):
            inner = _unwrap(nested)
            if inner and inner is not raw:
                raw = {**raw, **inner}
    reply = raw.get("response_json") or raw.get("responseJson")
    if isinstance(reply, (dict, str)):
        inner = _unwrap(reply)
        if inner:
            raw = {**raw, **inner}
    return raw


def _collect_flow_values(node: Any, bag: dict[str, list[str]], page_id: str = "") -> None:
    """Pull checkbox values out of Twilio's page payload or Meta's response_json."""
    if isinstance(node, str):
        text = node.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return
            _collect_flow_values(parsed, bag, page_id)
        return
    if isinstance(node, list):
        for item in node:
            _collect_flow_values(item, bag, page_id)
        return
    if not isinstance(node, dict):
        return
    current = str(node.get("id") or node.get("page_id") or node.get("pageId") or page_id)
    name = str(node.get("name") or "")
    label = str(node.get("label") or "").lower()
    if "value" in node:
        target = ""
        if name == "selected_trades" or current == TRADE_PAGE or "service" in label:
            target = "selected_trades"
        elif name == "selected_zones" or current == ZONE_PAGE or "area" in label:
            target = "selected_zones"
        if target:
            bag[target].extend(_as_list(node.get("value")))
    for key in ("selected_trades", "selected_zones"):
        if key in node:
            bag[key].extend(_as_list(node.get(key)))
    for value in node.values():
        if isinstance(value, (dict, list, str)):
            _collect_flow_values(value, bag, current)


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
    bag: dict[str, list[str]] = {"selected_trades": [], "selected_zones": []}
    _collect_flow_values(data, bag)
    trades_raw = bag["selected_trades"]
    zones_raw = bag["selected_zones"]
    if not trades_raw and not zones_raw:
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
