"""Exact WhatsApp phrases that open the pro dashboard or pro signup."""

from __future__ import annotations

_DASHBOARD = frozenset({"dashboard", "portal", "my jobs", "my job"})
_SIGNUP = frozenset({"pro signup", "pro sign up"})


def _norm(text: str) -> str:
    raw = (text or "").strip().lower().replace("’", "'").replace("`", "'")
    cleaned = "".join(ch if (ch.isalnum() or ch.isspace() or ch == "'") else " " for ch in raw)
    return " ".join(cleaned.split())


def is_dashboard_command(text: str) -> bool:
    """Whole message only, so a sentence that mentions jobs is left alone."""
    return _norm(text) in _DASHBOARD


def is_pro_signup(text: str) -> bool:
    return _norm(text) in _SIGNUP
