"""Code-level safety overrides for Diagnosis (do not trust the model alone)."""

from __future__ import annotations

from app.models.diagnosis import (
    EMERGENCY_FLAGS,
    EMERGENCY_HARDCODED,
    FORCE_NO_DIY_FLAGS,
    Diagnosis,
)


def apply_safety_overrides(diagnosis: Diagnosis) -> Diagnosis:
    """
    Force diy_appropriate=False and clear diy_steps for hazardous flags.
    Prepend hardcoded emergency copy for gas / CO / fire.
    """
    flags = set(diagnosis.safety_flags or [])
    data = diagnosis.model_dump()

    if flags & FORCE_NO_DIY_FLAGS:
        data["diy_appropriate"] = False
        data["diy_steps"] = []

    if flags & EMERGENCY_FLAGS:
        msg = (data.get("homeowner_message") or "").strip()
        if EMERGENCY_HARDCODED not in msg:
            combined = f"{EMERGENCY_HARDCODED}\n\n{msg}".strip()
            data["homeowner_message"] = combined[:500]

    return Diagnosis.model_validate(data)
