"""Unit tests for diagnosis safety overrides."""

from app.models.diagnosis import (
    EMERGENCY_HARDCODED,
    Diagnosis,
    ProBrief,
    SafetyFlag,
    Severity,
    Trade,
)
from app.services.safety import apply_safety_overrides


def _diag(**kwargs):
    base = dict(
        trade=Trade.electrical,
        severity=Severity.high,
        safety_flags=[],
        confidence="high",
        diy_appropriate=True,
        homeowner_message="Looks like a bad outlet.",
        diy_steps=["Unscrew the plate", "Replace outlet"],
        follow_up_question=None,
        pro_brief=ProBrief(
            issue="Outlet damage",
            likely_cause="Overload",
            parts_needed=["outlet"],
            est_time="1 hour",
        ),
    )
    base.update(kwargs)
    return Diagnosis.model_validate(base)


def test_electrical_hazard_forces_no_diy():
    d = apply_safety_overrides(
        _diag(safety_flags=[SafetyFlag.electrical_hazard])
    )
    assert d.diy_appropriate is False
    assert d.diy_steps == []


def test_gas_prepends_emergency_copy():
    d = apply_safety_overrides(_diag(safety_flags=[SafetyFlag.gas], trade=Trade.plumbing))
    assert d.diy_appropriate is False
    assert EMERGENCY_HARDCODED in d.homeowner_message


def test_safe_diy_untouched():
    d = apply_safety_overrides(
        _diag(
            trade=Trade.plumbing,
            severity=Severity.low,
            safety_flags=[],
            diy_appropriate=True,
            diy_steps=["Replace flapper"],
        )
    )
    assert d.diy_appropriate is True
    assert d.diy_steps == ["Replace flapper"]
