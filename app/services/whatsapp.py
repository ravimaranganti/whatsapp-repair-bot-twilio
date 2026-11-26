"""
Twilio WhatsApp Service — media download, freeform text, Content templates.
All outbound replies use the REST API (not TwiML).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_TWILIO_API_BASE = "https://api.twilio.com/2010-04-01"
OUTSIDE_WINDOW_CODE = 63016


def twilio_error_code(exc: BaseException) -> int | None:
    """Pull Twilio's numeric error code from an HTTP error, if present."""
    response = getattr(exc, "response", None)
    if response is None:
        return None
    try:
        body = response.json()
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    try:
        return int(body["code"])
    except (KeyError, TypeError, ValueError):
        return None


class WhatsAppService:
    @property
    def _auth(self):
        return (settings.twilio_account_sid, settings.twilio_auth_token)

    @property
    def _messages_url(self):
        return f"{_TWILIO_API_BASE}/Accounts/{settings.twilio_account_sid}/Messages.json"

    def _addrs(self, to: str) -> tuple[str, str]:
        to_addr = to if to.startswith("whatsapp:") else f"whatsapp:{to}"
        from_addr = settings.twilio_whatsapp_number
        if not from_addr.startswith("whatsapp:"):
            from_addr = f"whatsapp:{from_addr}"
        return to_addr, from_addr

    async def download_media(self, media_url: str) -> bytes:
        try:
            async with httpx.AsyncClient() as client:
                logger.info("Downloading media from: %s", media_url)
                response = await client.get(
                    media_url,
                    auth=self._auth,
                    timeout=60.0,
                    follow_redirects=True,
                )
                response.raise_for_status()
                logger.info("Media downloaded: %d bytes", len(response.content))
                return response.content
        except httpx.HTTPError as e:
            logger.error("HTTP error downloading media: %s", e)
            raise

    async def send_text_message(self, to: str, message: str) -> dict:
        to_addr, from_addr = self._addrs(to)
        payload = {"From": from_addr, "To": to_addr, "Body": message}
        try:
            async with httpx.AsyncClient() as client:
                logger.info("Sending message to %s", to_addr)
                response = await client.post(
                    self._messages_url,
                    data=payload,
                    auth=self._auth,
                    timeout=30.0,
                )
                response.raise_for_status()
                result = response.json()
                logger.info("Message sent — SID: %s", result.get("sid"))
                try:
                    from app.services.events import log_event

                    await log_event(
                        phone=to,
                        role="system",
                        type="outbound_message",
                        payload={
                            "sid": result.get("sid"),
                            "body_preview": (message or "")[:200],
                        },
                    )
                except Exception:
                    pass
                return result
        except httpx.HTTPStatusError as e:
            code = twilio_error_code(e)
            logger.error("Error sending message to %s: %s", to, e)
            if code == OUTSIDE_WINDOW_CODE:
                await self._log_outside_window(to, message)
            raise
        except httpx.HTTPError as e:
            logger.error("Error sending message to %s: %s", to, e)
            raise

    async def send_content_template(
        self,
        to: str,
        *,
        content_sid: str,
        content_variables: dict | None = None,
        status_callback: str | None = None,
    ) -> dict:
        """
        Send an approved Content Template (WhatsApp).
        Docs: Messages API ContentSid + ContentVariables (JSON string).
        https://www.twilio.com/docs/content/send-templates-with-the-content-api
        """
        to_addr, from_addr = self._addrs(to)
        payload: dict = {
            "From": from_addr,
            "To": to_addr,
            "ContentSid": content_sid,
        }
        if content_variables:
            payload["ContentVariables"] = json.dumps(content_variables)
        if status_callback:
            payload["StatusCallback"] = status_callback

        async with httpx.AsyncClient() as client:
            response = await client.post(
                self._messages_url,
                data=payload,
                auth=self._auth,
                timeout=30.0,
            )
            response.raise_for_status()
            result = response.json()
            try:
                from app.services.events import log_event

                await log_event(
                    phone=to,
                    role="system",
                    type="outbound_message",
                    payload={
                        "sid": result.get("sid"),
                        "content_sid": content_sid,
                        "template": True,
                    },
                )
            except Exception:
                pass
            return result

    async def delivery_state(self, message_sid: str) -> tuple[str, int | None]:
        """Current Twilio status and error code for one outbound message."""
        url = (
            f"{_TWILIO_API_BASE}/Accounts/{settings.twilio_account_sid}"
            f"/Messages/{message_sid}.json"
        )
        async with httpx.AsyncClient() as client:
            response = await client.get(url, auth=self._auth, timeout=20.0)
            response.raise_for_status()
            data = response.json()
        status = str(data.get("status") or "")
        code = data.get("error_code")
        try:
            error_code = int(code) if code not in (None, "") else None
        except (TypeError, ValueError):
            error_code = None
        return status, error_code

    async def wait_for_delivery(self, message_sid: str) -> tuple[str, int | None]:
        """Poll briefly. Meta rejects some templates after Twilio has already accepted them."""
        last = ("queued", None)
        for _ in range(4):
            await asyncio.sleep(2)
            try:
                last = await self.delivery_state(message_sid)
            except Exception as exc:
                logger.warning("delivery check failed for %s: %s", message_sid, exc)
                return last
            if last[0] in {"delivered", "read", "undelivered", "failed"}:
                return last
        return last

    async def send_session_buttons(
        self,
        to: str,
        body: str,
        buttons: list[tuple[str, str]],
    ) -> dict:
        """
        In-session quick replies. The body is the message itself, so it can
        include the pro's own summary. No Meta approval inside the 24h window.
        """
        actions = [
            {"title": title.strip()[:20], "id": button_id.strip()[:200]}
            for button_id, title in buttons[:3]
            if button_id.strip() and title.strip()
        ]
        if not actions:
            raise ValueError("session buttons need at least one action")
        content = {
            "friendly_name": f"session_buttons_{uuid.uuid4().hex[:12]}",
            "language": "en",
            "types": {
                "twilio/quick-reply": {
                    "body": (body or "").strip()[:1024],
                    "actions": actions,
                }
            },
        }
        async with httpx.AsyncClient() as client:
            created = await client.post(
                "https://content.twilio.com/v1/Content",
                json=content,
                auth=self._auth,
                timeout=30.0,
            )
            created.raise_for_status()
            content_sid = str(created.json().get("sid") or "")
        if not content_sid:
            raise RuntimeError("Twilio did not return a content sid")
        return await self.send_content_template(to, content_sid=content_sid)

    async def send_business_message(
        self,
        to: str,
        *,
        body: str,
        template_sid: str = "",
        content_variables: dict | None = None,
    ) -> dict:
        """
        Free-form text while the recipient's 24h window is open.
        Approved template when it is closed. On Twilio 63016, log and retry
        once with the template if one is configured.
        """
        from app.services.session_window import (
            choose_delivery,
            get_last_inbound,
            should_retry_with_template,
            window_is_open,
        )

        sid = (template_sid or "").strip()
        last = await get_last_inbound(to)
        channel = choose_delivery(
            window_open=window_is_open(last),
            template_sid=sid,
        )
        if channel == "template":
            return await self.send_content_template(
                to, content_sid=sid, content_variables=content_variables
            )
        try:
            return await self.send_text_message(to, body)
        except httpx.HTTPStatusError as e:
            if should_retry_with_template(twilio_error_code(e), sid):
                logger.warning(
                    "Twilio 63016 for %s; retrying with content template", to
                )
                return await self.send_content_template(
                    to, content_sid=sid, content_variables=content_variables
                )
            raise

    async def _log_outside_window(self, to: str, message: str) -> None:
        try:
            from app.services.events import log_event

            await log_event(
                phone=to,
                role="system",
                type="error",
                payload={
                    "code": OUTSIDE_WINDOW_CODE,
                    "error": "freeform_outside_window",
                    "body_preview": (message or "")[:200],
                },
            )
        except Exception:
            pass


whatsapp_service = WhatsAppService()
