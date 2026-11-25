"""
Job lifecycle state machine and Firestore persistence.

States:
  diagnosed → awaiting_consent → dispatched → awaiting_pick → assigned → completed
                   ↘ cancelled        ↘ expired                ↘ cancelled
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import settings
from app.models.diagnosis import Diagnosis
from app.utils.phones import normalize_phone, phone_hash

logger = logging.getLogger(__name__)

# Allowed transitions: from → frozenset(to)
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "diagnosed": frozenset({"awaiting_consent", "cancelled"}),
    "awaiting_consent": frozenset({"dispatched", "cancelled", "UNMATCHED_FALLBACK"}),
    "dispatched": frozenset({"awaiting_pick", "expired", "cancelled"}),
    "awaiting_pick": frozenset({"assigned", "expired", "cancelled"}),
    "assigned": frozenset({"completed", "cancelled", "awaiting_pick", "dispatched"}),
    "expired": frozenset({"cancelled"}),
    "completed": frozenset(),
    "cancelled": frozenset(),
}

ACTIVE_STATES = frozenset(
    {
        "diagnosed",
        "awaiting_consent",
        "dispatched",
        "awaiting_pick",
        "assigned",
    }
)


class InvalidTransition(Exception):
    def __init__(self, from_state: str, to_state: str):
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(f"Invalid job transition {from_state!r} → {to_state!r}")


def can_transition(from_state: str, to_state: str) -> bool:
    return to_state in ALLOWED_TRANSITIONS.get(from_state, frozenset())


def transition(from_state: str, to_state: str) -> str:
    """Return to_state if allowed; raise InvalidTransition otherwise."""
    if not can_transition(from_state, to_state):
        raise InvalidTransition(from_state, to_state)
    return to_state


def new_job_id() -> str:
    return uuid.uuid4().hex[:16]


def resolve_pro_from_pick_payload(
    payload: str, alerted_phones: list[str]
) -> str | None:
    """
    Parse pick:{job_id}:{pro_phone_hash} and match against alerted phones.
    Returns E.164-ish phone digits of the matched pro, or None.
    """
    parts = (payload or "").split(":")
    if len(parts) < 3 or parts[0] != "pick":
        return None
    target_hash = parts[2]
    for phone in alerted_phones:
        if phone_hash(phone) == target_hash:
            return normalize_phone(phone)
    return None


class JobStore:
    """Firestore jobs/{job_id} + responses subcollection."""

    def __init__(self) -> None:
        self._collection = "jobs"

    def _db(self):
        from app.services.firestore_db import get_db

        return get_db()

    async def create_job(
        self,
        *,
        homeowner_phone: str,
        diagnosis: Diagnosis,
        area_zip: str,
        photo_url: str | None = None,
        state: str = "diagnosed",
    ) -> dict[str, Any]:
        job_id = new_job_id()
        now = datetime.now(timezone.utc)
        window = int(settings.collection_window_min or 60)
        doc = {
            "job_id": job_id,
            "homeowner_phone": normalize_phone(homeowner_phone),
            "trade": diagnosis.trade.value,
            "severity": diagnosis.severity.value,
            "diagnosis": diagnosis.model_dump(mode="json"),
            "photo_url": photo_url,
            "area_zip": area_zip,
            "state": state,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=window)).isoformat(),
            "alerted_pro_phones": [],
            "selected_pro_phone": None,
            "followup_sent": False,
        }

        def _write() -> None:
            from app.services.firestore_db import server_timestamp

            payload = {**doc, "created_at_ts": server_timestamp()}
            self._db().collection(self._collection).document(job_id).set(payload)

        await asyncio.to_thread(_write)
        return doc

    async def list_for_homeowner(
        self, phone: str, states: set[str] | None = None
    ) -> list[dict[str, Any]]:
        phone_n = normalize_phone(phone)

        def _read() -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            for snap in self._db().collection(self._collection).stream():
                data = snap.to_dict() or {}
                if normalize_phone(str(data.get("homeowner_phone") or "")) != phone_n:
                    continue
                if states and data.get("state") not in states:
                    continue
                data["job_id"] = snap.id
                out.append(data)
            return out

        return await asyncio.to_thread(_read)

    async def list_for_pro(self, phone: str) -> list[dict[str, Any]]:
        """Jobs this pro was alerted on or booked. Pilot-scale collection scan."""
        phone_n = normalize_phone(phone)

        def _read() -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            for snap in self._db().collection(self._collection).stream():
                data = snap.to_dict() or {}
                selected = normalize_phone(str(data.get("selected_pro_phone") or ""))
                alerted = [
                    normalize_phone(str(item))
                    for item in (data.get("alerted_pro_phones") or [])
                ]
                if selected != phone_n and phone_n not in alerted:
                    continue
                data["job_id"] = snap.id
                out.append(data)
            return out

        return await asyncio.to_thread(_read)

    async def list_in_states(self, states: set[str] | frozenset[str]) -> list[dict[str, Any]]:
        """Pilot-scale scan for jobs in the given states."""
        wanted = set(states)

        def _read() -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            for snap in self._db().collection(self._collection).stream():
                data = snap.to_dict() or {}
                if data.get("state") not in wanted:
                    continue
                data["job_id"] = snap.id
                out.append(data)
            return out

        return await asyncio.to_thread(_read)

    async def list_all(self) -> list[dict[str, Any]]:
        """Pilot-scale scan of every job, newest caller sorts them."""

        def _read() -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            for snap in self._db().collection(self._collection).stream():
                data = snap.to_dict() or {}
                data["job_id"] = snap.id
                out.append(data)
            return out

        return await asyncio.to_thread(_read)

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        def _read() -> dict[str, Any] | None:
            snap = self._db().collection(self._collection).document(job_id).get()
            if not snap.exists:
                return None
            data = snap.to_dict() or {}
            data["job_id"] = job_id
            return data

        return await asyncio.to_thread(_read)

    async def set_state(
        self, job_id: str, to_state: str, **extra: Any
    ) -> dict[str, Any]:
        job = await self.get_job(job_id)
        if not job:
            raise KeyError(f"job {job_id} not found")
        from_state = job.get("state", "")
        transition(from_state, to_state)
        now = datetime.now(timezone.utc).isoformat()
        updates = {"state": to_state, "updated_at": now, **extra}

        def _write() -> None:
            self._db().collection(self._collection).document(job_id).update(updates)

        await asyncio.to_thread(_write)
        job.update(updates)
        return job

    async def update_fields(self, job_id: str, **fields: Any) -> None:
        fields["updated_at"] = datetime.now(timezone.utc).isoformat()

        def _write() -> None:
            self._db().collection(self._collection).document(job_id).update(fields)

        await asyncio.to_thread(_write)

    async def upsert_response(
        self, job_id: str, pro_phone: str, **fields: Any
    ) -> None:
        pro_id = normalize_phone(pro_phone)
        now = datetime.now(timezone.utc).isoformat()
        payload = {"pro_phone": pro_id, "updated_at": now, **fields}

        def _write() -> None:
            ref = (
                self._db()
                .collection(self._collection)
                .document(job_id)
                .collection("responses")
                .document(pro_id)
            )
            snap = ref.get()
            if snap.exists:
                ref.update(payload)
            else:
                ref.set({**payload, "created_at": now})

        await asyncio.to_thread(_write)

    async def list_responses(self, job_id: str) -> list[dict[str, Any]]:
        def _read() -> list[dict[str, Any]]:
            refs = (
                self._db()
                .collection(self._collection)
                .document(job_id)
                .collection("responses")
                .stream()
            )
            return [doc.to_dict() or {} for doc in refs]

        return await asyncio.to_thread(_read)

    async def count_complete_responses(self, job_id: str) -> int:
        responses = await self.list_responses(job_id)
        return sum(
            1
            for r in responses
            if r.get("status") in ("details_given", "selected", "not_selected")
            and r.get("availability_text")
        )


job_store = JobStore()
