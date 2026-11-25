"""Phase 5 — feedback capture + weekly digest helpers."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import settings
from app.models import (
    MODE_AWAITING_FEEDBACK,
    MODE_AWAITING_FEEDBACK_COMMENT,
    MODE_IDLE,
)
from app.services.conversation_state import conversation_state_store
from app.services.events import log_event
from app.services.pros import pro_store
from app.services.whatsapp import whatsapp_service
from app.utils.phones import normalize_phone

logger = logging.getLogger(__name__)


async def store_feedback(
    *,
    phone: str,
    role: str,
    kind: str,
    rating: int | None = None,
    text: str | None = None,
    job_id: str | None = None,
    media_url: str | None = None,
) -> str:
    fid = uuid.uuid4().hex[:12]
    doc = {
        "id": fid,
        "job_id": job_id,
        "phone": normalize_phone(phone),
        "role": role,
        "kind": kind,
        "rating": rating,
        "text": text,
        "media_url": media_url,
        "ts": datetime.now(timezone.utc).isoformat(),
    }

    def _write() -> None:
        from app.services.firestore_db import get_db, server_timestamp

        get_db().collection("feedback").document(fid).set(
            {**doc, "created_at": server_timestamp()}
        )

    await asyncio.to_thread(_write)

    if role == "homeowner" and rating and job_id:
        # Update pro rating when job rating includes selected pro — caller may pass
        pass

    await log_event(
        phone=phone,
        role=role,
        type="feedback",
        job_id=job_id,
        payload={"kind": kind, "rating": rating},
    )
    return fid


async def handle_feedback_keyword(phone: str, body: str) -> bool:
    if not settings.feedback_enabled:
        return False
    if (body or "").strip().upper() != "FEEDBACK":
        return False
    await conversation_state_store.set(
        phone, MODE_AWAITING_FEEDBACK_COMMENT, {"kind": "freeform"}
    )
    await whatsapp_service.send_text_message(
        phone,
        "Thanks — send your feedback as a text or voice note. We'll review it.",
    )
    return True


async def capture_pending_feedback(
    phone: str, body: str, media_url: str | None, state: dict[str, Any]
) -> bool:
    if state.get("mode") != MODE_AWAITING_FEEDBACK_COMMENT:
        return False
    transcript = body
    if media_url and not body:
        try:
            from app.services.gemini import gemini_service
            from app.services.whatsapp import whatsapp_service as wa

            audio = await wa.download_media(media_url)
            # Best-effort: ask Gemini to transcribe (audio part)
            from google.genai import types
            import asyncio

            def _transcribe() -> str:
                resp = gemini_service.client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=[
                        types.Part.from_bytes(data=audio, mime_type="audio/ogg"),
                        "Transcribe this voice note verbatim.",
                    ],
                )
                return (resp.text or "").strip()

            transcript = await asyncio.to_thread(_transcribe)
        except Exception as e:
            logger.warning("voice transcript failed: %s", e)
            transcript = "(voice note — transcription unavailable)"

    await store_feedback(
        phone=phone,
        role="homeowner",
        kind="freeform",
        text=transcript,
        media_url=media_url,
    )
    await conversation_state_store.clear(phone)
    await whatsapp_service.send_text_message(phone, "Feedback saved — thank you!")
    return True


_RATING_BY_PAYLOAD = {
    "rating_great": 5,
    "rating_okay": 3,
    "rating_not_good": 1,
}
_RATING_BY_TEXT = {
    "great": 5,
    "okay": 3,
    "ok": 3,
    "not good": 1,
    "5": 5,
    "4": 4,
    "3": 3,
    "2": 2,
    "1": 1,
}


async def handle_followup_rating(phone: str, body: str, payload: str) -> bool:
    """
    job_followup quick replies: Great / Okay / Not good.
    Button ids are static; the job id comes from conversation state set when
    the follow-up was sent.
    """
    pl = (payload or "").strip()
    from app.services.homeowner_actions import parse_homeowner_payload

    tap = parse_homeowner_payload(pl)
    rating = None
    tapped_job = ""
    if tap and tap.action in _RATING_BY_PAYLOAD:
        rating = _RATING_BY_PAYLOAD[tap.action]
        tapped_job = tap.job_id
    if rating is None:
        rating = _RATING_BY_PAYLOAD.get(pl)
    state = await conversation_state_store.get(phone)
    ctx = dict(state.get("context") or {})
    if rating is None and state.get("mode") == MODE_AWAITING_FEEDBACK:
        rating = _RATING_BY_TEXT.get((body or "").strip().lower())
    if rating is None:
        return False

    job_id = tapped_job or ctx.get("active_job_id") or ctx.get("job_id")
    role = ctx.get("role") or "homeowner"
    await store_feedback(
        phone=phone,
        role=role,
        kind="job_followup",
        rating=rating,
        text=(body or "").strip() or pl,
        job_id=job_id,
    )
    if role == "homeowner" and job_id:
        try:
            from app.services.jobs import job_store

            job = await job_store.get_job(job_id)
            pro_phone = (job or {}).get("selected_pro_phone")
            if pro_phone:
                await pro_store.apply_rating(pro_phone, rating)
        except Exception as e:
            logger.warning("pro rating update failed: %s", e)

    await conversation_state_store.clear(phone)
    await whatsapp_service.send_text_message(phone, "Thanks — rating saved.")
    return True


async def run_weekly_digest() -> str:
    """Summarize last 7 days and message OPERATOR_PHONE."""
    since = datetime.now(timezone.utc) - timedelta(days=7)

    def _query() -> dict[str, Any]:
        from app.services.firestore_db import get_db

        db = get_db()
        feedback = []
        for snap in db.collection("feedback").stream():
            d = snap.to_dict() or {}
            ts = d.get("ts") or ""
            if ts >= since.isoformat():
                feedback.append(d)
        jobs = []
        for snap in db.collection("jobs").stream():
            d = snap.to_dict() or {}
            if (d.get("created_at") or "") >= since.isoformat():
                jobs.append(d)
        return {"feedback": feedback, "jobs": jobs}

    data = await asyncio.to_thread(_query)
    prompt = (
        "Summarize this WhatsApp home-repair pilot week for the operator. "
        "Cover: volume, DIY vs pro split, DIY helpful rate, pro response rate, "
        "ratings, top complaints, notable misdiagnoses. Keep under 1200 chars.\n\n"
        f"DATA:\n{data}"
    )
    from app.services.gemini import gemini_service
    import asyncio as aio

    def _gen() -> str:
        resp = gemini_service.client.models.generate_content(
            model="gemini-2.5-flash", contents=prompt
        )
        return (resp.text or "").strip()[:1500]

    summary = await aio.to_thread(_gen)
    digest_id = datetime.now(timezone.utc).strftime("%Y%m%d")

    def _save() -> None:
        from app.services.firestore_db import get_db, server_timestamp

        get_db().collection("digests").document(digest_id).set(
            {
                "summary": summary,
                "created_at": server_timestamp(),
                "stats": {
                    "feedback_n": len(data["feedback"]),
                    "jobs_n": len(data["jobs"]),
                },
            }
        )

    await asyncio.to_thread(_save)

    if settings.operator_phone:
        await whatsapp_service.send_text_message(
            settings.operator_phone, f"*Weekly pilot digest*\n\n{summary}"
        )
    return summary
