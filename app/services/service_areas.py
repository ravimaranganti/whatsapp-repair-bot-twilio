"""Houston-area choices for pro onboarding.

Pros pick cities instead of typing ZIP codes. Matching still uses the
homeowner ZIP: suburb ZIPs win, and other 770/772 codes count as Houston.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# WhatsApp list pickers allow 10 rows. Direction notes go in the row description.
AREA_CHOICES: list[tuple[str, str, str]] = [
    ("houston", "Houston", "Inside the city"),
    ("sugar_land", "Sugar Land", "Southwest"),
    ("katy", "Katy", "West"),
    ("the_woodlands", "The Woodlands", "North"),
    ("pearland", "Pearland", "South"),
    ("cypress", "Cypress", "Northwest"),
    ("league_city", "League City", "Friendswood, Southeast"),
    ("missouri_city", "Missouri City", "Southwest"),
    ("conroe", "Conroe", "North, Montgomery County"),
    ("all", "All areas", "Houston and every area above"),
]

AREA_LABELS = {key: label for key, label, _note in AREA_CHOICES}
AREA_ALL = "all"

_ALIASES: dict[str, tuple[str, ...]] = {
    "houston": ("houston",),
    "sugar_land": ("sugar land", "sugarland"),
    "katy": ("katy",),
    "the_woodlands": ("the woodlands", "woodlands"),
    "pearland": ("pearland",),
    "cypress": ("cypress",),
    "league_city": ("league city", "friendswood"),
    "missouri_city": ("missouri city", "mo city"),
    "conroe": ("conroe",),
    "all": ("all areas", "all of houston", "everywhere", "all"),
}

# Suburb ZIPs are checked before the Houston 770/772 rule.
_SUBURB_ZIPS: dict[str, frozenset[str]] = {
    "sugar_land": frozenset({"77478", "77479", "77487", "77496", "77497", "77498"}),
    "katy": frozenset({"77449", "77450", "77491", "77492", "77493", "77494", "77084"}),
    "the_woodlands": frozenset(
        {"77380", "77381", "77382", "77384", "77385", "77386", "77387", "77389"}
    ),
    "pearland": frozenset({"77581", "77584", "77588"}),
    "cypress": frozenset({"77429", "77433", "77095", "77065"}),
    "league_city": frozenset({"77573", "77574", "77546", "77549"}),
    "missouri_city": frozenset({"77459", "77489"}),
    "conroe": frozenset(
        {"77301", "77302", "77303", "77304", "77305", "77306", "77316"}
    ),
}

_DONE_WORDS = frozenset(
    {
        "no",
        "nope",
        "done",
        "finished",
        "that's all",
        "thats all",
        "that is all",
        "all set",
        "stop",
    }
)
_MORE_WORDS = frozenset({"yes", "y", "yeah", "add", "add another", "another", "more"})

AREA_MORE_YES = "area_more:yes"
AREA_MORE_NO = "area_more:no"


def _norm(text: str) -> str:
    raw = (text or "").strip().lower().replace("’", "'")
    return " ".join(raw.split())


def area_label(key: str) -> str:
    return AREA_LABELS.get(key, key.replace("_", " "))


def format_areas(keys: list[str] | None) -> str:
    labels = [area_label(key) for key in (keys or [])]
    return ", ".join(labels)


def _zip5(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    return digits[:5] if len(digits) >= 5 else ""


def area_for_zip(zip_code: str) -> str | None:
    """Map a homeowner ZIP to one service area. None if it is outside this list."""
    z = _zip5(zip_code)
    if not z:
        return None
    for key, zips in _SUBURB_ZIPS.items():
        if z in zips:
            return key
    if z.startswith(("770", "772")):
        return "houston"
    return None


def zips_for_areas(keys: list[str]) -> list[str]:
    """Concrete ZIPs for the selected areas. Houston and All stay area-based."""
    if not keys or AREA_ALL in keys:
        return []
    found: list[str] = []
    for key in keys:
        for z in sorted(_SUBURB_ZIPS.get(key, ())):
            if z not in found:
                found.append(z)
    return found


def pro_covers_zip(pro: dict, area_zip: str) -> bool:
    """True when this pro should see a job in area_zip."""
    zip_s = (area_zip or "").strip()
    areas = [str(a).strip() for a in (pro.get("service_areas") or []) if str(a).strip()]
    zips = [str(z).strip() for z in (pro.get("service_zips") or []) if str(z).strip()]
    if not zip_s:
        return True
    if AREA_ALL in areas:
        return True
    if areas:
        return area_for_zip(zip_s) in areas or zip_s in zips
    if not zips:
        return True
    return zip_s in zips


def areas_in_text(text: str) -> list[str]:
    low = _norm(text)
    if low in _DONE_WORDS or low in _MORE_WORDS:
        return []
    found: list[str] = []
    for key, _label, _note in AREA_CHOICES:
        phrases = sorted(_ALIASES[key], key=len, reverse=True)
        if any(re.search(rf"\b{re.escape(phrase)}\b", low) for phrase in phrases):
            found.append(key)
    if AREA_ALL in found:
        return [AREA_ALL]
    return found


@dataclass
class AreaTurn:
    action: str  # add | show_list | done | invalid
    areas: list[str] = field(default_factory=list)


def interpret_area_turn(
    text: str,
    payload: str,
    stage: str,
    selected: list[str] | None = None,
) -> AreaTurn:
    """stage is choose or more."""
    stage = stage or "choose"
    selected = list(selected or [])
    pl = (payload or "").strip().lower()
    phrase = _norm(text)

    if pl == AREA_MORE_NO or (stage == "more" and phrase in _DONE_WORDS):
        return AreaTurn("done")
    if pl == AREA_MORE_YES or (stage == "more" and phrase in _MORE_WORDS):
        return AreaTurn("show_list")

    if pl.startswith("area:"):
        key = pl.split(":", 1)[1].strip()
        if key not in AREA_LABELS:
            return AreaTurn("invalid")
        if key == AREA_ALL or key not in selected:
            return AreaTurn("add", [key])
        return AreaTurn("add", [])

    known = areas_in_text(text)
    adds = [key for key in known if key == AREA_ALL or key not in selected]
    if adds:
        return AreaTurn("add", adds)
    if known:
        return AreaTurn("add", [])
    return AreaTurn("invalid")
