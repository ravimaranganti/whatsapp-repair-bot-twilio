"""WhatsApp 24-hour customer-care window and template vs free-form choice.

Business-initiated sends (job alert, not-selected, follow-up, reopen) must use
an approved Content Template when the recipient has not messaged us in the
last 24 hours. The window is based on the last inbound timestamp.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.utils.phones import normalize_phone

logger = logging.getLogger(__name__)

WINDOW = timedelta(hours=24)
_COLLECTION = "wa_sessions"

# Process-local fallback when Firestore is down. Never the only copy in production.
_memory_last_inbound: dict[str, datetime] = {}


def parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO string or datetime into timezone-aware UTC. None if unknown."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        ts = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            ts = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def window_is_open(
    last_inbound_at: Any,
    *,
    now: datetime | None = None,
) -> bool:
    """True when the recipient messaged us less than 24 hours ago."""
    ts = parse_timestamp(last_inbound_at)
    if ts is None:
        return False
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    age = current - ts
    # A couple of minutes of clock skew still counts as an open window.
    return -timedelta(minutes=2) <= age < WINDOW


def choose_delivery(*, window_open: bool, template_sid: str | None) -> str:
    """
    Return 'freeform' inside the 24h window, 'template' when it is closed
    and a Content SID is configured, otherwise 'freeform' (caller may hit 63016).
    """
    if window_open:
        return "freeform"
    if (template_sid or "").strip():
        return "template"
    return "freeform"


def should_retry_with_template(error_code: int | None, template_sid: str | None) -> bool:
    """Twilio 63016: free-form was rejected because the session window is closed."""
    return error_code == 63016 and bool((template_sid or "").strip())


def clean_template_value(value: object, fallback: str, *, limit: int = 200) -> str:
    """Collapse whitespace so a variable is never empty or full of newlines."""
    text = " ".join(str(value or "").split())
    if not text:
        text = fallback
    return text[:limit]


def _latest(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None:
        return b
    if b is None:
        return a
    return a if a >= b else b


async def record_inbound(phone: str, when: datetime | None = None) -> None:
    """Remember that this person just messaged us (opens the 24h window)."""
    key = normalize_phone(phone)
    if not key:
        return
    ts = when or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    ts = ts.astimezone(timezone.utc)
    _memory_last_inbound[key] = ts
    try:
        from app.services.firestore_db import get_db

        def _write() -> None:
            get_db().collection(_COLLECTION).document(key).set(
                {"phone": key, "last_inbound_at": ts.isoformat()},
                merge=True,
            )

        await asyncio.to_thread(_write)
    except Exception as e:
        logger.warning("session window write failed: %s", e)


async def get_last_inbound(phone: str) -> datetime | None:
    """Latest inbound timestamp, preferring the newer of Firestore and memory."""
    key = normalize_phone(phone)
    if not key:
        return None
    stored: datetime | None = None
    try:
        from app.services.firestore_db import get_db

        def _read() -> Any:
            snap = get_db().collection(_COLLECTION).document(key).get()
            if not snap.exists:
                return None
            return (snap.to_dict() or {}).get("last_inbound_at")

        stored = parse_timestamp(await asyncio.to_thread(_read))
    except Exception as e:
        logger.warning("session window read failed: %s", e)
    return _latest(stored, _memory_last_inbound.get(key))
