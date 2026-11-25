"""
WhatsApp Home Repair AI — FastAPI entrypoint.

Webhook: validate Twilio signature → dedupe → enqueue → return 200 empty TwiML immediately.
Heavy work runs via Cloud Tasks (/tasks/process-message) or FastAPI BackgroundTasks.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import BackgroundTasks, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from app.config import settings
from app.portal_routes import router as portal_router
from app.models import MessageContent
from app.services.agent import process_message
from app.services.idempotency import already_processed, mark_processed
from app.services.interactions import parse_interaction
from app.services.task_queue import enqueue_process_message, tasks_enabled

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    missing = [
        name
        for name, value in {
            "TWILIO_ACCOUNT_SID": settings.twilio_account_sid,
            "TWILIO_AUTH_TOKEN": settings.twilio_auth_token,
            "TWILIO_WHATSAPP_NUMBER": settings.twilio_whatsapp_number,
            "GEMINI_API_KEY": settings.gemini_api_key,
        }.items()
        if not value
    ]
    if missing:
        logger.error("Missing required credentials: %s — API calls will fail.", missing)
    else:
        logger.info("All credentials loaded. Service ready.")
    logger.info(
        "Conversation backend=%s dispatch=%s feedback=%s tasks=%s",
        settings.conversation_backend,
        settings.dispatch_enabled,
        settings.feedback_enabled,
        tasks_enabled(),
    )
    if not tasks_enabled():
        logger.info(
            "CLOUD_TASKS_QUEUE unset — using in-process BackgroundTasks. "
            "Requires Cloud Run CPU always allocated (--no-cpu-throttling)."
        )
    yield


app = FastAPI(
    title="WhatsApp Home Repair AI Agent",
    description="AI-powered household issue diagnosis via WhatsApp (Twilio)",
    version="3.0.0-pilot",
    lifespan=lifespan,
)
app.include_router(portal_router)


def _webhook_url(request: Request) -> str:
    """
    URL Twilio used when signing the request.
    Prefer PUBLIC_BASE_URL (must match Console webhook exactly).
    Docs: https://www.twilio.com/docs/usage/security
    """
    base = (settings.public_base_url or "").rstrip("/")
    if base:
        return f"{base}/webhook"
    # Fallback — may fail signature checks behind proxies if proto/host differ
    return str(request.url)


def _validate_twilio_signature(request: Request, form: dict[str, Any], signature: str) -> bool:
    if settings.skip_twilio_signature:
        return True
    if not settings.twilio_auth_token:
        logger.error("TWILIO_AUTH_TOKEN missing — rejecting webhook")
        return False
    from twilio.request_validator import RequestValidator

    validator = RequestValidator(settings.twilio_auth_token)
    url = _webhook_url(request)
    # Pass all POST params as strings (Twilio SDK requirement)
    params = {k: ("" if v is None else str(v)) for k, v in form.items()}
    return bool(validator.validate(url, params, signature or ""))


@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "service": "whatsapp-repair-bot",
        "version": "3.0.0-pilot",
        "dispatch_enabled": settings.dispatch_enabled,
        "feedback_enabled": settings.feedback_enabled,
    }


@app.post("/webhook")
async def handle_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    MessageSid: str = Form(...),
    From: str = Form(...),
    To: str = Form(...),
    Body: str = Form(""),
    NumMedia: int = Form(0),
    MediaUrl0: Optional[str] = Form(None),
    MediaContentType0: Optional[str] = Form(None),
    ButtonPayload: Optional[str] = Form(None),
    ButtonText: Optional[str] = Form(None),
    ListId: Optional[str] = Form(None),
    InteractiveData: Optional[str] = Form(None),
    x_twilio_signature: Optional[str] = Header(None, alias="X-Twilio-Signature"),
):
    """Validate, dedupe, enqueue, return 200 immediately (empty body / TwiML)."""
    form = dict(await request.form())
    # Ensure Form() params are in the dict for signature validation
    form.setdefault("MessageSid", MessageSid)
    form.setdefault("From", From)
    form.setdefault("To", To)
    form.setdefault("Body", Body)
    form.setdefault("NumMedia", str(NumMedia))
    if MediaUrl0 is not None:
        form.setdefault("MediaUrl0", MediaUrl0)
    if MediaContentType0 is not None:
        form.setdefault("MediaContentType0", MediaContentType0)
    if ButtonPayload is not None:
        form.setdefault("ButtonPayload", ButtonPayload)
    if ButtonText is not None:
        form.setdefault("ButtonText", ButtonText)

    if not _validate_twilio_signature(request, form, x_twilio_signature or ""):
        logger.warning("Invalid Twilio signature for SID=%s", MessageSid)
        raise HTTPException(status_code=403, detail="Invalid signature")

    if await already_processed(MessageSid):
        logger.info("Duplicate MessageSid=%s — skipping", MessageSid)
        return PlainTextResponse("", status_code=200)

    sender = From.removeprefix("whatsapp:")
    interaction = parse_interaction(form)

    if interaction.kind == "media" and interaction.media_url:
        content_type = (interaction.media_content_type or "").lower()
        msg_type = "video" if "video" in content_type else "image"
    elif interaction.kind in {"quick_reply", "list_picker"}:
        msg_type = "interactive"
    elif interaction.text or Body.strip():
        msg_type = "text"
    else:
        logger.warning("Empty message from %s — ignoring", sender)
        return PlainTextResponse("", status_code=200)

    message = MessageContent(
        id=MessageSid,
        from_=sender,
        type=msg_type,
        body=interaction.text or Body,
        media_url=interaction.media_url or MediaUrl0,
        media_content_type=interaction.media_content_type or MediaContentType0,
        button_payload=interaction.payload or ButtonPayload,
        button_text=ButtonText,
        list_id=ListId,
        interactive_data=InteractiveData,
    )

    await mark_processed(MessageSid, sender)

    payload = message.model_dump()
    enqueued = await enqueue_process_message(payload)
    if not enqueued:
        # Pilot fallback: in-process background task
        background_tasks.add_task(process_message, message, sender)

    return PlainTextResponse("", status_code=200)


@app.post("/tasks/process-message")
async def tasks_process_message(request: Request):
    """Cloud Tasks worker — process one inbound message."""
    # Soft auth: when OIDC is configured at the load balancer / Cloud Run IAM,
    # unauthenticated calls are blocked there. Optional header check for shared secret later.
    data = await request.json()
    message = MessageContent.model_validate(data)
    await process_message(message, message.from_)
    return {"ok": True}


@app.post("/tasks/collection-window")
async def tasks_collection_window(request: Request):
    data = await request.json()
    job_id = data.get("job_id")
    if not job_id:
        raise HTTPException(400, "job_id required")
    from app.services.dispatch import on_collection_window_closed

    await on_collection_window_closed(job_id)
    return {"ok": True}


@app.post("/tasks/job-followup")
async def tasks_job_followup(request: Request):
    data = await request.json()
    job_id = data.get("job_id")
    if not job_id:
        raise HTTPException(400, "job_id required")
    if not settings.feedback_enabled:
        return {"ok": True, "skipped": True}
    from app.services.jobs import job_store
    from app.services.whatsapp import whatsapp_service

    job = await job_store.get_job(job_id)
    if not job or job.get("followup_sent"):
        return {"ok": True, "skipped": True}
    from app.models import MODE_AWAITING_FEEDBACK
    from app.services.conversation_state import conversation_state_store
    from app.services.session_window import clean_template_value
    from app.utils.phones import normalize_phone

    homeowner = normalize_phone(job.get("homeowner_phone") or "")
    trade = clean_template_value(job.get("trade"), "repair", limit=40)
    when = clean_template_value((job.get("created_at") or "")[:10], "today", limit=32)
    for role_phone in {job.get("homeowner_phone"), job.get("selected_pro_phone")}:
        if not role_phone:
            continue
        role = "homeowner" if normalize_phone(role_phone) == homeowner else "pro"
        try:
            await whatsapp_service.send_business_message(
                role_phone,
                body=(
                    f"How did the {trade} job on {when} go? "
                    "Reply Great, Okay, or Not good."
                ),
                template_sid=settings.tpl_job_followup_sid,
                content_variables={"1": trade, "2": when, "3": clean_template_value(job_id, "job", limit=32)},
            )
        except Exception as e:
            logger.warning("follow-up send failed for %s: %s", role, e)
            continue
        await conversation_state_store.set(
            role_phone,
            MODE_AWAITING_FEEDBACK,
            {"job_id": job_id, "active_job_id": job_id, "role": role},
        )
    await job_store.update_fields(job_id, followup_sent=True)
    return {"ok": True}


@app.post("/tasks/weekly-digest")
async def tasks_weekly_digest():
    """Cloud Scheduler target (Monday morning)."""
    from app.services.feedback import run_weekly_digest

    summary = await run_weekly_digest()
    return {"ok": True, "chars": len(summary)}


@app.post("/admin/seed-pros")
async def admin_seed_pros():
    """One-shot seed from professionals.json (opted_in=False). Protect in production."""
    from app.services.pros import seed_pros_from_json

    n = await seed_pros_from_json()
    return {"seeded": n}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.port,
        log_level=settings.log_level.lower(),
    )
