"""Twilio WhatsApp webhook / internal message models."""

from typing import Optional

from pydantic import BaseModel, Field


class TwilioWebhookPayload(BaseModel):
    """Parsed fields from a Twilio WhatsApp webhook POST."""

    message_sid: str
    from_: str
    to: str
    body: str = ""
    num_media: int = 0
    media_url0: Optional[str] = None
    media_content_type0: Optional[str] = None


class MessageContent(BaseModel):
    """Normalised internal message representation for the agent layer."""

    id: str
    from_: str
    type: str  # text | image | video | interactive
    body: str = ""
    media_url: Optional[str] = None
    media_content_type: Optional[str] = None
    button_payload: Optional[str] = None
    button_text: Optional[str] = None
    list_id: Optional[str] = None
    interactive_data: Optional[str] = None


class Interaction(BaseModel):
    """Parsed button / list / text interaction from an inbound webhook."""

    kind: str  # text | quick_reply | list_picker | media | unknown
    payload: str = ""
    text: str = ""
    media_url: Optional[str] = None
    media_content_type: Optional[str] = None


# conversation_state.mode values
MODE_IDLE = "idle"
MODE_AWAITING_AREA = "awaiting_area"
MODE_AWAITING_DISPATCH_CONSENT = "awaiting_dispatch_consent"
MODE_AWAITING_PICK = "awaiting_pick"
MODE_AWAITING_SHOW_OPTIONS = "awaiting_show_options"
MODE_AWAITING_SHARE_NOTES = "awaiting_share_notes"
MODE_AWAITING_VISIT_DETAILS = "awaiting_visit_details"
MODE_AWAITING_QUOTE_QUESTION = "awaiting_quote_question"
MODE_AWAITING_NEW_ISSUE = "awaiting_new_issue_confirm"
MODE_PRO_ONBOARDING = "pro_onboarding"
MODE_PRO_AWAITING_DETAILS = "pro_awaiting_details"
MODE_AWAITING_FEEDBACK = "awaiting_feedback"
MODE_AWAITING_FEEDBACK_COMMENT = "awaiting_feedback_comment"
MODE_AWAITING_ROLE = "awaiting_role"
MODE_AWAITING_JOIN_CODE = "awaiting_join_code"
