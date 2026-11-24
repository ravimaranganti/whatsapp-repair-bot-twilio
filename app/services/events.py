"""Pilot event log — primary analysis data in Firestore `events` collection."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.utils.phones import normalize_phone

logger = logging.getLogger(__name__)


async def log_event(
    *,
    phone: str | None,
    role: str,
    type: str,
    payload: dict[str, Any] | None = None,
    job_id: str | None = None,
) -> None:
    """
    Write one event document.
    Types include: inbound_message, outbound_message, gemini_call,
    state_transition, button_tap, error, …
    """
    try:
        from app.services.firestore_db import get_db, server_timestamp
        import asyncio

        doc = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "phone": normalize_phone(phone) if phone else None,
            "role": role,
            "type": type,
            "job_id": job_id,
            "payload": payload or {},
            "created_at": server_timestamp(),
        }

        def _write() -> None:
            get_db().collection("events").add(doc)

        await asyncio.to_thread(_write)
    except Exception as e:
        # Never break the bot on analytics failure
        logger.warning("event log failed type=%s: %s", type, e)
