"""Per-phone conversation mode for interpreting the next inbound message."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from app.models import MODE_IDLE
from app.utils.phones import normalize_phone


class ConversationStateStore:
    def __init__(self) -> None:
        self._collection = "conversation_state"

    def _ref(self, phone: str):
        from app.services.firestore_db import get_db

        return get_db().collection(self._collection).document(normalize_phone(phone))

    async def get(self, phone: str) -> dict[str, Any]:
        def _read() -> dict[str, Any]:
            snap = self._ref(phone).get()
            if not snap.exists:
                return {"mode": MODE_IDLE, "context": {}, "phone": normalize_phone(phone)}
            data = snap.to_dict() or {}
            data.setdefault("mode", MODE_IDLE)
            data.setdefault("context", {})
            return data

        try:
            return await asyncio.to_thread(_read)
        except Exception:
            return {"mode": MODE_IDLE, "context": {}, "phone": normalize_phone(phone)}

    async def set(
        self,
        phone: str,
        mode: str,
        context: dict[str, Any] | None = None,
    ) -> None:
        doc = {
            "phone": normalize_phone(phone),
            "mode": mode,
            "context": context or {},
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        def _write() -> None:
            self._ref(phone).set(doc, merge=True)

        try:
            await asyncio.to_thread(_write)
        except Exception:
            # Local without Firestore — ignore
            pass

    async def list_all(self) -> dict[str, dict[str, Any]]:
        """Phone → session, for the admin view of unfinished pro signup."""

        def _read() -> dict[str, dict[str, Any]]:
            from app.services.firestore_db import get_db

            out: dict[str, dict[str, Any]] = {}
            for snap in get_db().collection(self._collection).stream():
                out[normalize_phone(snap.id)] = snap.to_dict() or {}
            return out

        try:
            return await asyncio.to_thread(_read)
        except Exception:
            return {}

    async def clear(self, phone: str) -> None:
        await self.set(phone, MODE_IDLE, {})


conversation_state_store = ConversationStateStore()
