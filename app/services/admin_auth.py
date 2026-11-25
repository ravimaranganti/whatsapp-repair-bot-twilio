"""Signed cookie for the ops dashboard. The password is the HMAC key."""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import datetime, timedelta, timezone

from fastapi import Request

from app.config import settings

COOKIE_NAME = "hp_admin_session"
TTL = timedelta(hours=12)


def admin_password() -> str:
    return (settings.admin_password or "").strip()


def sign_session(expires_at: datetime, secret: str) -> str:
    payload = f"admin|{int(expires_at.timestamp())}"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}|{sig}"


def session_ok(cookie: str, secret: str, now: datetime | None = None) -> bool:
    if not secret or not cookie:
        return False
    parts = cookie.split("|")
    if len(parts) != 3 or parts[0] != "admin":
        return False
    payload = f"{parts[0]}|{parts[1]}"
    expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, parts[2]):
        return False
    try:
        expires = int(parts[1])
    except ValueError:
        return False
    current = now or datetime.now(timezone.utc)
    return current.timestamp() < expires


def basic_ok(header: str | None, secret: str) -> bool:
    if not secret or not header or not header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1].strip()).decode()
    except Exception:
        return False
    _user, _, given = decoded.partition(":")
    if not given:
        return False
    return hmac.compare_digest(given, secret)


def request_ok(request: Request) -> bool:
    secret = admin_password()
    if not secret:
        return False
    if session_ok(request.cookies.get(COOKIE_NAME, ""), secret):
        return True
    return basic_ok(request.headers.get("authorization"), secret)
