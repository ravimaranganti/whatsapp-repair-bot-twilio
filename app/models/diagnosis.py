"""Structured diagnosis schema for Gemini JSON output."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class Trade(str, Enum):
    plumbing = "plumbing"
    hvac = "hvac"
    electrical = "electrical"
    handyman = "handyman"
    appliance = "appliance"
    painting = "painting"
    general_contractor = "general_contractor"
    pool = "pool"
    lawn = "lawn"
    other = "other"
    not_home_issue = "not_home_issue"


class Severity(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"
    emergency = "emergency"


class SafetyFlag(str, Enum):
    gas = "gas"
    electrical_hazard = "electrical_hazard"
    active_flooding = "active_flooding"
    sewage = "sewage"
    mold = "mold"
    structural = "structural"
    fire_risk = "fire_risk"
    carbon_monoxide = "carbon_monoxide"


class ProBrief(BaseModel):
    issue: str = Field(description="1 sentence summary of the issue")
    likely_cause: str = Field(description="1 sentence likely cause")
    parts_needed: list[str] = Field(default_factory=list, max_length=5)
    est_time: str = Field(description='e.g. "1-2 hours"')
    notes: str | None = None


class Diagnosis(BaseModel):
    trade: Trade
    severity: Severity
    safety_flags: list[SafetyFlag] = Field(default_factory=list)
    confidence: Literal["low", "medium", "high"]
    diy_appropriate: bool
    homeowner_message: str = Field(
        description="<= 500 chars, plain English, what it is + immediate safety advice"
    )
    diy_steps: list[str] = Field(default_factory=list)
    follow_up_question: str | None = None
    pro_brief: ProBrief

    @field_validator("homeowner_message")
    @classmethod
    def _trim_homeowner(cls, v: str) -> str:
        v = (v or "").strip()
        return v[:500] if len(v) > 500 else v

    @field_validator("diy_steps")
    @classmethod
    def _cap_diy(cls, v: list[str]) -> list[str]:
        return list(v or [])[:5]


# Safety flags that force DIY off (code-level override; do not trust the model alone)
FORCE_NO_DIY_FLAGS = frozenset(
    {
        SafetyFlag.gas,
        SafetyFlag.carbon_monoxide,
        SafetyFlag.electrical_hazard,
        SafetyFlag.active_flooding,
        SafetyFlag.sewage,
        SafetyFlag.structural,
        SafetyFlag.fire_risk,
    }
)

EMERGENCY_FLAGS = frozenset(
    {SafetyFlag.gas, SafetyFlag.carbon_monoxide, SafetyFlag.fire_risk}
)

EMERGENCY_HARDCODED = (
    "*Safety first:* Leave the area now. Do not operate light switches or appliances. "
    "From outside, call 911 and your gas utility if you smell gas or suspect carbon monoxide."
)

# Map diagnosis trade → Firestore/pros trade key / matcher category
TRADE_TO_PRO_CATEGORY = {
    Trade.plumbing: "plumbing",
    Trade.hvac: "hvac",
    Trade.electrical: "electrical",
    Trade.handyman: "handyman",
    Trade.appliance: "appliance",
    Trade.painting: "painting",
    Trade.general_contractor: "general_contractor",
    Trade.pool: "pool",
    Trade.lawn: "lawn",
    Trade.other: "handyman",
    Trade.not_home_issue: "handyman",
}
