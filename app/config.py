"""
Configuration management using Pydantic Settings
Loads environment variables from .env file
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Twilio credentials
    twilio_account_sid: str = Field(default="", description="Twilio Account SID")
    twilio_auth_token: str = Field(default="", description="Twilio Auth Token")
    twilio_whatsapp_number: str = Field(
        default="",
        description="Twilio WhatsApp sender number, e.g. whatsapp:+14155238886",
    )

    # Public HTTPS base URL used for Twilio signature validation (no trailing slash).
    # On Cloud Run set to https://<service>-xxxxx.run.app — must match the webhook URL Twilio calls.
    public_base_url: str = Field(
        default="",
        description="Public base URL of this service for Twilio RequestValidator",
    )
    app_base_url: str = Field(
        default="",
        description="Public origin for pro dashboard links. Falls back to PUBLIC_BASE_URL.",
    )
    dashboard_session_secret: str = Field(
        default="",
        description="HMAC secret for the pro dashboard session cookie.",
    )

    # Gemini AI credentials
    gemini_api_key: str = Field(default="", description="Google Gemini API key")
    google_places_api_key: str = Field(
        default="",
        description="Places API (New) key. Used only when no partner pro matches a job.",
    )

    # Conversation memory: memory (default, single-process) or firestore (Cloud Run)
    conversation_backend: str = Field(
        default="memory",
        description='Conversation persistence: "memory" or "firestore"',
    )
    conversation_max_messages: int = Field(
        default=24,
        ge=4,
        le=200,
        description="Max stored messages per user (user + assistant turns).",
    )
    google_cloud_project: str = Field(
        default="",
        description="GCP project id for Firestore (optional; Cloud Run sets GOOGLE_CLOUD_PROJECT).",
    )

    # Vertex AI Memory Bank (optional; set on Cloud Run when Reasoning Engine exists)
    vertex_agent_engine_name: str = Field(
        default="",
        description="Full resource name: projects/.../locations/.../reasoningEngines/...",
    )
    google_cloud_region: str = Field(
        default="us-central1",
        description="Vertex AI region (must match Reasoning Engine location).",
    )

    # Phase 0 — pilot controls
    pilot_allowlist: str = Field(
        default="",
        description="Comma-separated E.164 numbers allowed in the pilot. Empty = allow all (dev).",
    )
    dispatch_enabled: bool = Field(
        default=False,
        description="Feature flag: send pro job alerts / brokered dispatch",
    )
    feedback_enabled: bool = Field(
        default=False,
        description="Feature flag: DIY/job ratings and freeform feedback",
    )
    skip_twilio_signature: bool = Field(
        default=False,
        description="Skip X-Twilio-Signature checks (local curl tests only).",
    )

    # Phase 3 — pro join
    pro_join_code: str = Field(
        default="",
        description="Optional JOIN <code>; when set, pros with the code bypass allowlist",
    )
    pro_support_phone: str = Field(
        default="",
        description="Single phone shown when a pro replies HELP, if PRO_SUPPORT_CONTACTS is empty.",
    )
    pro_support_contacts: str = Field(
        default="Ravi:7134088948,Jay:3466819558",
        description="Comma-separated Name:phone pairs shown when a pro replies HELP.",
    )

    # Phase 4 — dispatch
    max_pros_per_job: int = Field(default=4, ge=1, le=10)
    collection_window_min: int = Field(default=60, ge=5, le=1440)

    # Twilio Content Template SIDs (submit via Content Template Builder; Utility category)
    tpl_job_alert_sid: str = Field(default="", description="job_alert Content SID")
    tpl_job_not_selected_sid: str = Field(
        default="", description="job_not_selected Content SID"
    )
    tpl_job_followup_sid: str = Field(default="", description="job_followup Content SID")
    tpl_job_reopen_sid: str = Field(default="", description="job_reopen Content SID (optional)")
    tpl_welcome_role_sid: str = Field(
        default="",
        description="In-session welcome quick reply (role:homeowner / role:pro). No WhatsApp approval.",
    )
    tpl_pro_trades_sid: str = Field(
        default="",
        description="In-session list picker for pro trades, including Other. No WhatsApp approval.",
    )
    tpl_pro_trade_more_sid: str = Field(
        default="",
        description="In-session quick reply: add another trade or that's all. No WhatsApp approval.",
    )
    pro_profile_flow_content_sid: str = Field(
        default="",
        description="WhatsApp Flow for multi-select trades and service areas. Needs approval.",
    )
    tpl_pro_areas_sid: str = Field(
        default="",
        description="In-session list picker for Houston service areas. No WhatsApp approval.",
    )
    tpl_pro_area_more_sid: str = Field(
        default="",
        description="In-session quick reply: add another area or that's all. No WhatsApp approval.",
    )
    tpl_pro_emergency_sid: str = Field(
        default="",
        description="In-session yes/no for same-day or urgent calls. No WhatsApp approval.",
    )
    tpl_pro_lead_time_sid: str = Field(
        default="",
        description="In-session lead-time buttons for standard jobs. No WhatsApp approval.",
    )
    tpl_pro_fee_sid: str = Field(
        default="",
        description="In-session service-call fee buttons. No WhatsApp approval.",
    )
    tpl_pro_trades_extra_sid: str = Field(
        default="",
        description="Unused. Mobile mechanic is a row on the main trade list.",
    )
    tpl_pro_language_sid: str = Field(
        default="",
        description="In-session English / Spanish / Both buttons. No WhatsApp approval.",
    )
    tpl_pro_photo_estimate_sid: str = Field(
        default="",
        description="In-session yes/no for a free rough estimate from photos. No WhatsApp approval.",
    )
    tpl_pro_insured_sid: str = Field(
        default="",
        description="In-session insured yes/no for plumbing, electrical, and HVAC.",
    )
    tpl_quote_choice_sid: str = Field(
        default="",
        description="Per-quote Book / Pass / Ask buttons. Utility, includes job and quote ids.",
    )
    tpl_quote_card_sid: str = Field(
        default="",
        description="In-session polished quote card. Book / Ask / Reply / Pass, with job and quote ids.",
    )
    tpl_pro_question_sid: str = Field(
        default="",
        description="In-session homeowner reply button for a pro's pre-quote question.",
    )
    tpl_dispatch_consent_sid: str = Field(
        default="",
        description="Homeowner yes/no to contact pros. Button id includes the job id.",
    )
    tpl_share_notes_sid: str = Field(
        default="",
        description="Share access notes yes/no. Button id includes job and quote ids.",
    )
    tpl_new_issue_sid: str = Field(
        default="",
        description="New issue vs keep current request. Button id includes the open job id.",
    )

    # Phase 5
    cron_secret: str = Field(
        default="",
        description="Shared secret for Cloud Scheduler calls to /api/cron/followups.",
    )
    tpl_keep_looking_sid: str = Field(
        default="",
        description="In-session Keep Looking / Cancel Request after the only quote is passed.",
    )
    tpl_no_reply_followup_sid: str = Field(
        default="",
        description="In-session Ask Again / Leave It when no quote has arrived.",
    )
    tpl_quote_reminder_sid: str = Field(
        default="",
        description="In-session reminder for a quote the homeowner has not booked or passed.",
    )
    post_job_checkin_content_sid: str = Field(
        default="",
        description="Utility template for the 48-hour how-did-it-go check-in. Needs WhatsApp approval.",
    )
    tpl_job_rescue_sid: str = Field(
        default="",
        description="In-session Find Another Pro / Close Request after a booked job falls through.",
    )
    admin_password: str = Field(
        default="",
        description="Password for the /admin ops dashboard. Empty disables the page.",
    )
    operator_phone: str = Field(
        default="",
        description="E.164 phone that receives the weekly digest",
    )

    # Optional Cloud Tasks (preferred over in-process background work)
    # When unset, webhook uses FastAPI BackgroundTasks.
    # Tradeoff: requires Cloud Run CPU always allocated (--no-cpu-throttling), already set in cloudbuild.yaml.
    cloud_tasks_queue: str = Field(
        default="",
        description="Cloud Tasks queue id, e.g. whatsapp-inbound. Empty = in-process background task.",
    )
    cloud_tasks_location: str = Field(default="us-central1")
    task_handler_audience: str = Field(
        default="",
        description="OIDC audience for /tasks/* (usually the Cloud Run service URL).",
    )

    # Server configuration — Cloud Run injects PORT=8080
    port: int = Field(default=8080, description="Server port")
    log_level: str = Field(default="INFO", description="Logging level")

    def allowlist_numbers(self) -> set[str]:
        """Normalized digit-only phones from PILOT_ALLOWLIST."""
        out: set[str] = set()
        for part in (self.pilot_allowlist or "").split(","):
            digits = "".join(c for c in part.strip() if c.isdigit())
            if digits:
                out.add(digits)
        return out


settings = Settings()
