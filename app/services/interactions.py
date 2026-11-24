"""
Parse Twilio WhatsApp inbound form fields into a single Interaction.

Verified against Twilio docs (2026):
- Quick replies / template buttons: ButtonText (visible), ButtonPayload (id)
- List pickers: item id returned in ButtonPayload; title often in Body / ButtonText
  (list-picker Content types are in-session only; ids support variables)
- Media: MediaUrl0 + MediaContentType0
See: https://www.twilio.com/docs/whatsapp/buttons
     https://www.twilio.com/docs/content/twiliocard
"""

from __future__ import annotations

from typing import Any, Mapping

from app.models import Interaction


def parse_interaction(form: Mapping[str, Any]) -> Interaction:
    """
    Normalize Twilio webhook form dict → Interaction(kind, payload, text, …).

    Accepts both raw Form keys and already-extracted values.
    """
    body = str(form.get("Body") or form.get("body") or "").strip()
    button_payload = str(
        form.get("ButtonPayload") or form.get("button_payload") or ""
    ).strip()
    button_text = str(form.get("ButtonText") or form.get("button_text") or "").strip()
    list_id = str(
        form.get("ListId")
        or form.get("list_id")
        or form.get("ListItemId")
        or ""
    ).strip()
    interactive = str(
        form.get("InteractiveData") or form.get("interactive_data") or ""
    ).strip()
    media_url = form.get("MediaUrl0") or form.get("media_url0") or form.get("media_url")
    media_type = (
        form.get("MediaContentType0")
        or form.get("media_content_type0")
        or form.get("media_content_type")
    )
    num_media = int(form.get("NumMedia") or form.get("num_media") or 0)

    # Prefer explicit button / list payloads
    if button_payload or list_id:
        return Interaction(
            kind="list_picker" if list_id and not button_payload else "quick_reply",
            payload=button_payload or list_id,
            text=button_text or body,
            media_url=str(media_url) if media_url else None,
            media_content_type=str(media_type) if media_type else None,
        )

    if interactive:
        return Interaction(
            kind="quick_reply",
            payload=interactive,
            text=body or button_text,
        )

    if num_media > 0 and media_url:
        return Interaction(
            kind="media",
            payload="",
            text=body,
            media_url=str(media_url),
            media_content_type=str(media_type) if media_type else None,
        )

    if body:
        return Interaction(kind="text", payload="", text=body)

    return Interaction(kind="unknown", payload="", text="")
