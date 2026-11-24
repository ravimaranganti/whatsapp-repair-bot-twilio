"""Phone number helpers."""

from __future__ import annotations

import hashlib
import re


def normalize_phone(phone: str) -> str:
    """Digits-only id; keep leading country code when present."""
    digits = re.sub(r"\D", "", phone or "")
    return digits or (phone or "").strip()


def to_e164(phone: str) -> str:
    """Return +E.164 when possible."""
    digits = normalize_phone(phone)
    if not digits:
        return phone or ""
    return f"+{digits}" if not digits.startswith("+") else digits


def phone_hash(phone: str, length: int = 10) -> str:
    """Short stable hash for button payloads (not reversible at this length)."""
    digest = hashlib.sha256(normalize_phone(phone).encode()).hexdigest()
    return digest[:length]
