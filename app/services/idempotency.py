"""Idempotency for Twilio MessageSid — skip duplicate webhook deliveries."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app.utils.phones import normalize_phone

logger = logging.getLogger(__name__)

# Soft TTL ~3 days (Firestore TTL policy can be configured on expire_at separately)
_TTL_DAYS = 3

# In-memory fallback when Firestore is unavailable (local / memory backend)
_memory_seen: set[str] = set()


async def already_processed(message_sid: str) -> bool:
    if not message_sid:
        return False
    try:
        from app.services.firestore_db import get_db

        def _check() -> bool:
            snap = get_db().collection("processed_messages").document(message_sid).get()
            return bool(snap.exists)

        return await asyncio.to_thread(_check)
    except Exception as e:
        logger.warning("idempotency check failed, using memory: %s", e)
        return message_sid in _memory_seen


async def mark_processed(message_sid: str, phone: str | None = None) -> None:
    if not message_sid:
        return
    _memory_seen.add(message_sid)
    try:
        from app.services.firestore_db import get_db, server_timestamp

        expire_at = datetime.now(timezone.utc) + timedelta(days=_TTL_DAYS)

        def _write() -> None:
            get_db().collection("processed_messages").document(message_sid).set(
                {
                    "message_sid": message_sid,
                    "phone": normalize_phone(phone) if phone else None,
                    "processed_at": server_timestamp(),
                    "expire_at": expire_at,
                }
            )

        await asyncio.to_thread(_write)
    except Exception as e:
        logger.warning("idempotency mark failed: %s", e)
