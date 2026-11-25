"""Firestore pros/{phone} — opted-in professional directory."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from app.services.service_areas import pro_covers_zip
from app.utils.phones import normalize_phone, to_e164

logger = logging.getLogger(__name__)

# Painting is stored under handyman. Older profiles may still say painting.
_TRADE_MATCH_GROUPS = (
    frozenset({"handyman", "painting"}),
)


def _trades_that_match(trade: str) -> set[str]:
    key = (trade or "").lower().strip()
    for group in _TRADE_MATCH_GROUPS:
        if key in group:
            return set(group)
    return {key} if key else set()


# Map professionals.json categories → trade strings used in jobs/matching
JSON_CATEGORY_TO_TRADE = {
    "plumbers": "plumbing",
    "hvac": "hvac",
    "electricians": "electrical",
    "handyman": "handyman",
}


class ProStore:
    def __init__(self) -> None:
        self._collection = "pros"

    def _db(self):
        from app.services.firestore_db import get_db

        return get_db()

    def _ref(self, phone: str):
        return self._db().collection(self._collection).document(normalize_phone(phone))

    async def get(self, phone: str) -> dict[str, Any] | None:
        def _read() -> dict[str, Any] | None:
            snap = self._ref(phone).get()
            if not snap.exists:
                return None
            data = snap.to_dict() or {}
            data["phone"] = normalize_phone(phone)
            return data

        return await asyncio.to_thread(_read)

    async def upsert(self, phone: str, **fields: Any) -> dict[str, Any]:
        phone_id = normalize_phone(phone)
        existing = await self.get(phone_id)
        now = datetime.now(timezone.utc).isoformat()
        base = existing or {
            "phone": phone_id,
            "opted_in": False,
            "status": "active",
            "trades": [],
            "service_zips": [],
            "rating_avg": 0.0,
            "rating_count": 0,
            "response_stats": {
                "alerts_sent": 0,
                "responses": 0,
                "avg_response_min": 0,
            },
            "pending_alert_job_ids": [],
            "created_at": now,
        }
        base.update(fields)
        base["updated_at"] = now

        def _write() -> None:
            self._ref(phone_id).set(base, merge=True)

        await asyncio.to_thread(_write)
        return base

    async def set_opted_in(self, phone: str, opted_in: bool) -> None:
        fields: dict[str, Any] = {"opted_in": opted_in}
        if opted_in:
            fields["opted_in_at"] = datetime.now(timezone.utc).isoformat()
            fields["status"] = "active"
        await self.upsert(phone, **fields)

    async def list_matching(
        self,
        *,
        trade: str,
        area_zip: str,
        only_opted_in: bool = True,
    ) -> list[dict[str, Any]]:
        trade_l = (trade or "").lower().strip()
        zip_s = (area_zip or "").strip()

        def _read() -> list[dict[str, Any]]:
            # Pilot scale: scan collection (small). Index later if needed.
            out: list[dict[str, Any]] = []
            for snap in self._db().collection(self._collection).stream():
                p = snap.to_dict() or {}
                p["phone"] = snap.id
                if only_opted_in and not p.get("opted_in"):
                    continue
                if p.get("status", "active") != "active":
                    continue
                trades = [str(t).lower() for t in (p.get("trades") or [])]
                if trade_l and not set(trades).intersection(_trades_that_match(trade_l)):
                    continue
                if not pro_covers_zip(p, zip_s):
                    continue
                out.append(p)
            return out

        return await asyncio.to_thread(_read)

    async def list_all(self) -> list[dict[str, Any]]:
        """Pilot-scale scan of every pro profile."""

        def _read() -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            for snap in self._db().collection(self._collection).stream():
                data = snap.to_dict() or {}
                data["phone"] = snap.id
                out.append(data)
            return out

        return await asyncio.to_thread(_read)

    async def push_pending_alert(self, phone: str, job_id: str) -> None:
        def _write() -> None:
            from google.cloud import firestore

            self._ref(phone).set(
                {"pending_alert_job_ids": firestore.ArrayUnion([job_id])},
                merge=True,
            )

        await asyncio.to_thread(_write)

    async def pending_alert_ids(self, phone: str) -> list[str]:
        pro = await self.get(phone)
        if not pro:
            return []
        return [str(job_id) for job_id in (pro.get("pending_alert_job_ids") or []) if job_id]

    async def remove_pending_alert(self, phone: str, job_id: str) -> None:
        if not job_id:
            return

        def _write() -> None:
            from google.cloud import firestore

            self._ref(phone).set(
                {"pending_alert_job_ids": firestore.ArrayRemove([job_id])},
                merge=True,
            )

        await asyncio.to_thread(_write)

    async def pop_pending_alert(self, phone: str) -> str | None:
        pro = await self.get(phone)
        if not pro:
            return None
        pending = list(pro.get("pending_alert_job_ids") or [])
        if not pending:
            return None
        job_id = pending[-1]

        def _write() -> None:
            from google.cloud import firestore

            self._ref(phone).update(
                {"pending_alert_job_ids": firestore.ArrayRemove([job_id])}
            )

        await asyncio.to_thread(_write)
        return job_id

    async def apply_rating(self, phone: str, rating: int) -> None:
        rating = max(1, min(5, int(rating)))
        pro = await self.get(phone) or await self.upsert(phone)
        count = int(pro.get("rating_count") or 0)
        avg = float(pro.get("rating_avg") or 0.0)
        new_count = count + 1
        new_avg = ((avg * count) + rating) / new_count
        await self.upsert(phone, rating_avg=round(new_avg, 2), rating_count=new_count)

    async def record_completed_job(self, phone: str, rating: int | None) -> None:
        """Count a finished job, and fold in a 1–5 rating when the homeowner gave one."""
        pro = await self.get(phone) or await self.upsert(phone)
        if rating is not None:
            await self.apply_rating(phone, rating)
            pro = await self.get(phone) or pro
        completed = int(pro.get("completed_jobs_count") or 0) + 1
        average = float(pro.get("rating_avg") or 0.0)
        await self.upsert(
            phone,
            completed_jobs_count=completed,
            average_rating=round(average, 2),
        )


pro_store = ProStore()


def load_professionals_json() -> dict[str, list[dict]]:
    path = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "..", "professionals.json")
    )
    with open(path, encoding="utf-8") as f:
        return json.load(f)


async def seed_pros_from_json(*, force: bool = False) -> int:
    """
    Seed pros from professionals.json with opted_in=False.
    Skips existing docs unless force=True.
    """
    data = load_professionals_json()
    count = 0
    for category, pros in data.items():
        trade = JSON_CATEGORY_TO_TRADE.get(category, "handyman")
        for p in pros:
            phone = to_e164(p.get("phone", ""))
            if not normalize_phone(phone):
                continue
            existing = await pro_store.get(phone)
            if existing and not force:
                continue
            # Extract rough ZIPs from area names is not reliable — leave empty;
            # pros fill ZIPs during JOIN. Optionally stash neighborhood labels.
            await pro_store.upsert(
                phone,
                name=p.get("name"),
                business_name=p.get("name"),
                trades=[trade],
                service_zips=[],
                availability_text=p.get("availability", ""),
                rate_text=p.get("hourly_rate", ""),
                opted_in=False,
                status="active",
                rating_avg=float(p.get("rating") or 0),
                rating_count=0,
                seed_areas=p.get("areas") or [],
                certifications=p.get("certifications") or [],
            )
            count += 1
    logger.info("Seeded %d pros from professionals.json", count)
    return count
