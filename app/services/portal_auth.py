"""Passwordless pro login: 24-hour magic link, then an HTTP-only session cookie.

Tokens stay valid until they expire. WhatsApp prefetches links, so a one-time
token would be burned before the pro taps it.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from app.config import settings
from app.utils.phones import normalize_phone

logger = logging.getLogger(__name__)

COOKIE_NAME = "hp_pro_session"
TOKEN_TTL = timedelta(hours=24)
LINK_TEXT = "Here is your HomePro Assistant dashboard link (valid for 24h):"


def portal_base_url() -> str:
    return (settings.app_base_url or settings.public_base_url or "").rstrip("/")


def session_secret() -> str:
    return (settings.dashboard_session_secret or "").strip()


def token_id(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


def sign_session(phone: str, expires_at: datetime, secret: str) -> str:
    payload = f"{normalize_phone(phone)}|{int(expires_at.timestamp())}"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}|{sig}"


def read_session(cookie: str, secret: str, now: datetime | None = None) -> str | None:
    if not cookie or not secret or cookie.count("|") != 2:
        return None
    phone, exp_s, sig = cookie.split("|", 2)
    payload = f"{phone}|{exp_s}"
    expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        exp = datetime.fromtimestamp(int(exp_s), tz=timezone.utc)
    except ValueError:
        return None
    if exp <= (now or datetime.now(timezone.utc)):
        return None
    phone_n = normalize_phone(phone)
    return phone_n or None


class MemoryMagicTokenStore:
    def __init__(self) -> None:
        self.docs: dict[str, dict[str, str]] = {}

    async def put(self, doc_id: str, phone: str, expires_at: datetime) -> None:
        self.docs[doc_id] = {
            "phone": normalize_phone(phone),
            "expires_at": expires_at.astimezone(timezone.utc).isoformat(),
        }

    async def get(self, doc_id: str) -> dict[str, str] | None:
        return self.docs.get(doc_id)


class FirestoreMagicTokenStore:
    def __init__(self) -> None:
        self._collection = "magic_tokens"

    async def put(self, doc_id: str, phone: str, expires_at: datetime) -> None:
        import asyncio

        def _write() -> None:
            from app.services.firestore_db import get_db

            get_db().collection(self._collection).document(doc_id).set(
                {
                    "phone": normalize_phone(phone),
                    "expires_at": expires_at.astimezone(timezone.utc).isoformat(),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            )

        await asyncio.to_thread(_write)

    async def get(self, doc_id: str) -> dict[str, Any] | None:
        import asyncio

        def _read() -> dict[str, Any] | None:
            from app.services.firestore_db import get_db

            snap = get_db().collection(self._collection).document(doc_id).get()
            if not snap.exists:
                return None
            return snap.to_dict() or {}

        return await asyncio.to_thread(_read)


def _default_store() -> MemoryMagicTokenStore | FirestoreMagicTokenStore:
    if (settings.conversation_backend or "").strip().lower() == "firestore":
        return FirestoreMagicTokenStore()
    return MemoryMagicTokenStore()


token_store: MemoryMagicTokenStore | FirestoreMagicTokenStore = _default_store()


def _parse_expiry(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        ts = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        ts = datetime.fromisoformat(text)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


async def issue_magic_link(phone: str) -> str | None:
    """Return the login URL, or None when the public URL or cookie secret is unset."""
    base = portal_base_url()
    secret = session_secret()
    if not base or not secret:
        logger.warning(
            "Dashboard link unavailable (base_url=%s, session_secret=%s)",
            bool(base),
            bool(secret),
        )
        return None
    raw = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + TOKEN_TTL
    await token_store.put(token_id(raw), phone, expires)
    return f"{base}/pro/auth?token={quote(raw, safe='')}"


async def phone_for_token(raw_token: str, now: datetime | None = None) -> tuple[str, datetime] | None:
    raw = (raw_token or "").strip()
    if len(raw) < 20:
        return None
    doc = await token_store.get(token_id(raw))
    if not doc:
        return None
    expires = _parse_expiry(doc.get("expires_at"))
    phone = normalize_phone(str(doc.get("phone") or ""))
    if not expires or not phone or expires <= (now or datetime.now(timezone.utc)):
        return None
    return phone, expires


async def send_dashboard_reply(phone: str) -> None:
    from app.services.pros import pro_store
    from app.services.whatsapp import whatsapp_service

    phone_n = normalize_phone(phone)
    pro = await pro_store.get(phone_n)
    if not pro or not (pro.get("name") or pro.get("business_name") or pro.get("trades")):
        await whatsapp_service.send_text_message(
            phone_n,
            "Reply *JOIN* or *Pro Signup* to create your pro profile. "
            "Then reply *dashboard* for your link.",
        )
        return
    url = await issue_magic_link(phone_n)
    if not url:
        await whatsapp_service.send_text_message(
            phone_n,
            "Your dashboard link isn't available right now. Please try again in a little while.",
        )
        return
    await whatsapp_service.send_text_message(phone_n, f"{LINK_TEXT} {url}")
