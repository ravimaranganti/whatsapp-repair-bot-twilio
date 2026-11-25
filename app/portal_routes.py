"""Public landing page and the passwordless pro dashboard."""

from __future__ import annotations

import logging
import re
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from app.config import settings
from app.services.portal_auth import (
    COOKIE_NAME,
    phone_for_token,
    read_session,
    session_secret,
    sign_session,
)
from app.services.pro_portal import (
    load_dashboard,
    mark_completed,
    pass_on_job,
    save_profile,
    submit_quote,
)

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))

_NOTICES = {
    "completed": "Marked complete. The homeowner was told on WhatsApp.",
    "passed": "Passed. This job was removed from your open alerts.",
    "quoted": "Quote sent. The homeowner will see it in WhatsApp.",
    "saved": "Settings saved. New jobs will use these preferences.",
    "updated": "This job has already been updated.",
    "need_both": "Add both a time and an estimate.",
}


def _wa_url(text: str) -> str:
    digits = re.sub(r"\D", "", settings.twilio_whatsapp_number or "")
    base = f"https://wa.me/{digits}" if digits else "https://wa.me/"
    return f"{base}?text={quote(text)}"


def _session_phone(request: Request) -> str | None:
    secret = session_secret()
    if not secret:
        return None
    return read_session(request.cookies.get(COOKIE_NAME, ""), secret)


def _redirect(path: str, notice: str = "") -> RedirectResponse:
    if notice:
        sep = "&" if "?" in path else "?"
        path = f"{path}{sep}notice={quote(notice)}"
    return RedirectResponse(path, status_code=303)


@router.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    return templates.TemplateResponse(
        request,
        "landing.html",
        {
            "homeowner_url": _wa_url("Hi HomePro, I need help with a home project"),
            "pro_url": _wa_url("Pro Signup"),
        },
    )


@router.get("/pro/auth")
async def pro_auth(request: Request, token: str = ""):
    if not session_secret():
        return _message(
            request,
            "Dashboard sign-in is not configured yet.",
            status_code=503,
        )
    found = await phone_for_token(token)
    if not found:
        return _message(
            request,
            "This link has expired. Send dashboard on WhatsApp and we’ll text a new one.",
            status_code=401,
        )
    phone, expires = found
    from datetime import datetime, timezone

    max_age = max(0, int((expires - datetime.now(timezone.utc)).total_seconds()))
    response = RedirectResponse("/pro/dashboard", status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        sign_session(phone, expires, session_secret()),
        max_age=max_age,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        path="/",
    )
    return response


@router.get("/pro/logout")
async def pro_logout():
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


@router.get("/pro/dashboard", response_class=HTMLResponse)
async def pro_dashboard(request: Request, tab: str = "jobs", notice: str = ""):
    phone = _session_phone(request)
    if not phone:
        return _message(
            request,
            "Send dashboard on WhatsApp and open the link we text you. It lasts 24 hours.",
            status_code=401,
        )
    active = tab if tab in {"jobs", "history", "profile"} else "jobs"
    try:
        data = await load_dashboard(phone)
    except Exception as e:
        logger.error("dashboard load failed: %s", e, exc_info=True)
        return _message(request, "We couldn’t load your jobs just now. Try the link again.", status_code=500)
    data.update(
        {
            "tab": active,
            "notice": _NOTICES.get(notice, ""),
            "whatsapp_url": _wa_url("dashboard"),
        }
    )
    return templates.TemplateResponse(request, "dashboard.html", data)


@router.post("/pro/dashboard/profile")
async def pro_profile_save(request: Request):
    phone = _session_phone(request)
    if not phone:
        return _redirect("/pro/dashboard")
    form = await request.form()
    areas = form.getlist("service_areas")
    result = await save_profile(
        phone,
        {
            "accepts_emergency": form.get("accepts_emergency"),
            "standard_lead_time": form.get("standard_lead_time"),
            "dispatch_fee_tier": form.get("dispatch_fee_tier"),
            "service_areas": areas,
            "languages": form.getlist("languages"),
            "insured": form.get("insured"),
            "free_photo_estimate": form.get("free_photo_estimate"),
            "profile_notes": form.get("profile_notes"),
        },
    )
    if result:
        return _message(request, result, status_code=400)
    return _redirect("/pro/dashboard?tab=profile", "saved")


@router.post("/pro/dashboard/jobs/{job_id}/complete")
async def pro_complete(request: Request, job_id: str):
    phone = _session_phone(request)
    if not phone:
        return _redirect("/pro/dashboard")
    result = await mark_completed(phone, job_id)
    return _redirect("/pro/dashboard?tab=jobs", result)


@router.post("/pro/dashboard/jobs/{job_id}/pass")
async def pro_pass(request: Request, job_id: str):
    phone = _session_phone(request)
    if not phone:
        return _redirect("/pro/dashboard")
    result = await pass_on_job(phone, job_id)
    return _redirect("/pro/dashboard?tab=jobs", result)


@router.post("/pro/dashboard/jobs/{job_id}/quote")
async def pro_quote(request: Request, job_id: str):
    phone = _session_phone(request)
    if not phone:
        return _redirect("/pro/dashboard")
    form = await request.form()
    result = await submit_quote(
        phone,
        job_id,
        str(form.get("availability") or ""),
        str(form.get("estimate") or ""),
    )
    return _redirect("/pro/dashboard?tab=jobs", result)


def _message(request: Request, text: str, status_code: int):
    return templates.TemplateResponse(
        request,
        "portal_message.html",
        {"text": text, "whatsapp_url": _wa_url("dashboard")},
        status_code=status_code,
    )
