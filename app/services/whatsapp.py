"""
Twilio WhatsApp Service — media download, freeform text, Content templates.
All outbound replies use the REST API (not TwiML).
"""

from __future__ import annotations

import json
import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_TWILIO_API_BASE = "https://api.twilio.com/2010-04-01"


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
        except httpx.HTTPError as e:
            logger.error("Error sending message to %s: %s", to, e)
            raise

    async def send_content_template(
        self,
        to: str,
        *,
        content_sid: str,
        content_variables: dict | None = None,
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


whatsapp_service = WhatsAppService()
