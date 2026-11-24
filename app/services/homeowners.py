"""Firestore homeowners/{phone} — profile + service notes."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from app.utils.phones import normalize_phone


class HomeownerStore:
    def __init__(self) -> None:
        self._collection = "homeowners"

    def _ref(self, phone: str):
        from app.services.firestore_db import get_db

        return get_db().collection(self._collection).document(normalize_phone(phone))

    async def get(self, phone: str) -> dict[str, Any] | None:
        def _read() -> dict[str, Any] | None:
            snap = self._ref(phone).get()
            if not snap.exists:
                return None
            data = snap.to_dict() or {}
            data["phone"] = normalize_phone(phone)
            return data

        return await asyncio.to_thread(_read)

    async def ensure(self, phone: str, **fields: Any) -> dict[str, Any]:
        existing = await self.get(phone)
        if existing:
            if fields:
                await self.update(phone, **fields)
                return (await self.get(phone)) or existing
            return existing
        now = datetime.now(timezone.utc).isoformat()
        doc = {
            "phone": normalize_phone(phone),
            "name": fields.get("name"),
            "area_zip": fields.get("area_zip"),
            "neighborhood": fields.get("neighborhood"),
            "service_notes": [],
            "created_at": now,
            "active_job_id": None,
            **{k: v for k, v in fields.items() if k not in ("service_notes",)},
        }

        def _write() -> None:
            from app.services.firestore_db import server_timestamp

            self._ref(phone).set({**doc, "created_at_ts": server_timestamp()})

        await asyncio.to_thread(_write)
        return doc

    async def update(self, phone: str, **fields: Any) -> None:
        def _write() -> None:
            self._ref(phone).set(fields, merge=True)

        await asyncio.to_thread(_write)

    async def add_service_note(self, phone: str, note: str) -> None:
        text = (note or "").strip()
        if not text:
            return
        await self.ensure(phone)
        entry = {
            "text": text[:500],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        def _write() -> None:
            from google.cloud import firestore

            self._ref(phone).update({"service_notes": firestore.ArrayUnion([entry])})

        await asyncio.to_thread(_write)

    async def list_service_notes(self, phone: str) -> list[str]:
        doc = await self.get(phone)
        if not doc:
            return []
        notes = doc.get("service_notes") or []
        out: list[str] = []
        for n in notes:
            if isinstance(n, dict) and n.get("text"):
                out.append(str(n["text"]))
            elif isinstance(n, str) and n.strip():
                out.append(n.strip())
        return out[-20:]


homeowner_store = HomeownerStore()
