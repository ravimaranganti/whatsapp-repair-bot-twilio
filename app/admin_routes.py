"""Password-protected ops dashboard."""

from __future__ import annotations

import hmac
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.services.admin_auth import (
    COOKIE_NAME,
    TTL,
    admin_password,
    request_ok,
    sign_session,
)
from app.services.admin_ops import add_manual_quote, load_ops, save_pro_ops, set_pro_status

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))

_NOTICES = {
    "quoted": "Quote saved. The homeowner card was sent on WhatsApp.",
    "saved": "Pro updated.",
    "need_fields": "Name, phone, time, and price are all required.",
    "need_zone": "Choose at least one service zone.",
    "closed": "That job is no longer open for a quote.",
    "missing": "That pro was not found.",
    "bad_login": "That password did not match.",
}


def _redirect(path: str, notice: str = "") -> RedirectResponse:
    if notice:
        sep = "&" if "?" in path else "?"
        path = f"{path}{sep}notice={quote(notice)}"
    return RedirectResponse(path, status_code=303)


def _set_cookie(response: RedirectResponse, request: Request) -> None:
    secret = admin_password()
    expires = datetime.now(timezone.utc) + TTL
    response.set_cookie(
        COOKIE_NAME,
        sign_session(expires, secret),
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        max_age=int(TTL.total_seconds()),
        path="/",
    )


def _page_guard(request: Request) -> RedirectResponse | None:
    if not admin_password():
        return None
    if request_ok(request):
        return None
    return RedirectResponse("/admin/login", status_code=303)


def _api_guard(request: Request) -> None:
    if not admin_password():
        raise HTTPException(status_code=503, detail="Admin login is not configured")
    if not request_ok(request):
        raise HTTPException(status_code=401, detail="Unauthorized")


@router.get("/admin/login", response_class=HTMLResponse)
async def admin_login(request: Request, notice: str = ""):
    if not admin_password():
        return templates.TemplateResponse(
            request,
            "admin_login.html",
            {"configured": False, "notice": ""},
        )
    if request_ok(request):
        return RedirectResponse("/admin", status_code=303)
    return templates.TemplateResponse(
        request,
        "admin_login.html",
        {"configured": True, "notice": _NOTICES.get(notice, "")},
    )


@router.post("/admin/login")
async def admin_login_submit(request: Request, password: str = Form(default="")):
    secret = admin_password()
    if not secret:
        return RedirectResponse("/admin/login", status_code=303)
    if not hmac.compare_digest(password, secret):
        return _redirect("/admin/login", "bad_login")
    response = RedirectResponse("/admin", status_code=303)
    _set_cookie(response, request)
    return response


@router.post("/admin/logout")
async def admin_logout():
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


@router.get("/admin", response_class=HTMLResponse)
async def admin_home(
    request: Request,
    tab: str = "jobs",
    status: str = "all",
    trade: str = "",
    zone: str = "",
    q: str = "",
    notice: str = "",
):
    if not admin_password():
        return RedirectResponse("/admin/login", status_code=303)
    blocked = _page_guard(request)
    if blocked:
        return blocked
    data = await load_ops(tab=tab, status=status, trade=trade, zone=zone, query=q)
    data["notice"] = _NOTICES.get(notice, "")
    return templates.TemplateResponse(request, "admin.html", data)


@router.post("/admin/jobs/{job_id}/quote")
async def admin_manual_quote(
    request: Request,
    job_id: str,
    name: str = Form(default=""),
    phone: str = Form(default=""),
    availability: str = Form(default=""),
    price: str = Form(default=""),
):
    if not admin_password() or not request_ok(request):
        return RedirectResponse("/admin/login", status_code=303)
    result = await add_manual_quote(job_id, name, phone, availability, price)
    return _redirect("/admin?tab=jobs", result)


@router.post("/admin/pros/{phone}/status")
async def admin_pro_status(request: Request, phone: str, paused: str = Form(default="1")):
    if not admin_password() or not request_ok(request):
        return RedirectResponse("/admin/login", status_code=303)
    result = await set_pro_status(phone, paused in {"1", "true", "on", "yes"})
    return _redirect("/admin?tab=pros", result)


@router.post("/admin/pros/{phone}/profile")
async def admin_pro_profile(
    request: Request,
    phone: str,
    notes: str = Form(default=""),
    zones: list[str] | None = Form(default=None),
):
    if not admin_password() or not request_ok(request):
        return RedirectResponse("/admin/login", status_code=303)
    result = await save_pro_ops(phone, notes, zones or [])
    return _redirect("/admin?tab=pros", result)


@router.get("/api/admin/overview")
async def admin_overview(
    request: Request,
    status: str = "all",
    trade: str = "",
    zone: str = "",
    q: str = "",
):
    _api_guard(request)
    data = await load_ops(tab="jobs", status=status, trade=trade, zone=zone, query=q)
    return JSONResponse(
        {
            "metrics": data["metrics"],
            "jobs": data["jobs"],
            "pros": data["pros"],
        }
    )
