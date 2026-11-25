"""Role-aware welcome: homeowner, pro, or ask on the first vague message.

The allowlist check stays in the agent and runs before this module.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.config import settings
from app.models import (
    MODE_AWAITING_JOIN_CODE,
    MODE_AWAITING_PICK,
    MODE_AWAITING_ROLE,
    MODE_AWAITING_SHOW_OPTIONS,
    MODE_IDLE,
    MODE_PRO_AWAITING_DETAILS,
    MODE_PRO_ONBOARDING,
)
from app.services.conversation_state import conversation_state_store
from app.services.events import log_event
from app.services.homeowners import homeowner_store
from app.services.jobs import ACTIVE_STATES, job_store
from app.services.onboarding import start_pro_onboarding
from app.services.portal_commands import is_dashboard_command, is_pro_signup
from app.services.pros import pro_store
from app.services.whatsapp import whatsapp_service
from app.utils.phones import normalize_phone, to_e164

logger = logging.getLogger(__name__)

WELCOME_TEXT = (
    "👋 Hi! I'm Houston Home Repair AI. I help with repairs, installs, mounting, "
    "painting, and other home projects, and I can connect you with local pros.\n\n"
    "Which describes you?"
)
HOMEOWNER_READY = (
    "Great! Send me a photo of the issue with a short description, like "
    "'leak under the kitchen sink.' I'll tell you if it's a quick DIY fix or "
    "find a local pro for you."
)
HELP_HOMEOWNER = (
    "*Homeowner menu*\n"
    "- Send a photo of a repair, install, mount, or paint job\n"
    "- Ask a question about the work\n"
    "- Reply *STATUS* to check your job\n"
    "- Tell me access notes to save for a visit (gate code, parking, pets)\n"
    "- Reply *FEEDBACK* anytime"
)


def _support_display(phone: str) -> str:
    digits = normalize_phone(phone)
    if len(digits) == 10:
        digits = f"1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+1 {digits[1:4]}-{digits[4:7]}-{digits[7:]}"
    if len(digits) >= 10:
        return to_e164(digits)
    return ""


def pro_support_line() -> str:
    """Questions line for pros. Named contacts win over a single phone."""
    raw = (settings.pro_support_contacts or "").strip()
    contacts: list[tuple[str, str]] = []
    if raw:
        for part in raw.split(","):
            piece = part.strip()
            if not piece:
                continue
            name, phone = piece.split(":", 1) if ":" in piece else ("", piece)
            shown = _support_display(phone)
            if shown:
                contacts.append((name.strip(), shown))
    else:
        shown = _support_display(settings.pro_support_phone)
        if shown:
            contacts.append(("", shown))
    if not contacts:
        return ""
    bits = [f"{name} at {phone}" if name else phone for name, phone in contacts]
    if len(bits) == 1:
        return f"Questions? Call or text {bits[0]}."
    if len(bits) == 2:
        return f"Questions? Call or text {bits[0]} or {bits[1]}."
    return "Questions? Call or text " + ", ".join(bits[:-1]) + f", or {bits[-1]}."


def help_pro_text() -> str:
    """Pro HELP menu, including who to call."""
    lines = [
        "*Pro menu*",
        "- *PROFILE* — see your profile",
        "- *DASHBOARD* — your jobs, plus areas, urgent calls, timing, fee, photo estimates, language, insurance, and your note",
        "- *JOIN* — change your name, business, or trades",
        "- *PAUSE* / *RESUME* — pause or resume alerts",
        "- *STOP* — unsubscribe",
        "- *FEEDBACK* — send feedback",
    ]
    line = pro_support_line()
    if line:
        lines.append(line)
    return "\n".join(lines)
RETURNING_HOMEOWNER = "Welcome back! Send a photo of whatever needs fixing."
PRO_IDLE = (
    "No open jobs right now. I'll message you when one matches your trade and area. "
    "Reply *DASHBOARD* for your job list, *PROFILE* to review it, or *HELP* for the menu."
)
PRO_UNSUBSCRIBED = (
    "You're unsubscribed from job alerts. Reply *JOIN* to resubscribe."
)
JOIN_CODE_PROMPT = "Reply with the pro join code to finish signing up."
JOIN_CODE_WRONG = "That code didn't match. "
JOIN_CODE_LOCKED = (
    "Too many incorrect codes. Reply JOIN later if you still want to sign up as a pro."
)

HOMEOWNER_JOB_MODES = frozenset(
    {
        "awaiting_area",
        "awaiting_dispatch_consent",
        "awaiting_pick",
        "awaiting_show_options",
        "awaiting_share_notes",
        "awaiting_quote_question",
        "awaiting_new_issue_confirm",
    }
)
PRO_FLOW_MODES = frozenset(
    {
        MODE_PRO_ONBOARDING,
        MODE_PRO_AWAITING_DETAILS,
        MODE_AWAITING_JOIN_CODE,
    }
)
IN_PROGRESS_MODES = HOMEOWNER_JOB_MODES | PRO_FLOW_MODES | frozenset(
    {
        "awaiting_feedback",
        "awaiting_feedback_comment",
    }
)

_VAGUE = frozenset(
    {
        "hi",
        "hello",
        "hey",
        "hiya",
        "howdy",
        "yo",
        "sup",
        "hola",
        "start",
        "hey there",
        "hi there",
        "hello there",
        "good morning",
        "good afternoon",
        "good evening",
        "what is this",
        "whats this",
        "what's this",
        "who is this",
        "who are you",
    }
)
_HOMEOWNER_ANSWERS = frozenset(
    {
        "1",
        "homeowner",
        "owner",
        "i'm a homeowner",
        "im a homeowner",
        "i am a homeowner",
    }
)
_PRO_ANSWERS = frozenset(
    {
        "2",
        "pro",
        "professional",
        "contractor",
        "i'm a pro",
        "im a pro",
        "i am a pro",
    }
)


@dataclass
class WelcomeDecision:
    action: str
    clear_state: bool = False
    next_mode: str | None = None
    next_context: dict[str, Any] | None = None
    reminder: str = ""
    menu_role: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def _norm(text: str) -> str:
    raw = (text or "").strip().lower().replace("’", "'").replace("`", "'")
    cleaned = "".join(ch if (ch.isalnum() or ch.isspace() or ch == "'") else " " for ch in raw)
    return " ".join(cleaned.split())


def is_help(text: str) -> bool:
    return _norm(text) in {"help", "menu"}


def is_greeting_or_vague(text: str) -> bool:
    """Greeting, vague one-liner, or emoji-only. Help/menu is separate."""
    original = (text or "").strip()
    if not original:
        return True
    norm = _norm(original)
    if not norm:
        return True
    return norm in _VAGUE


def is_status_keyword(text: str) -> bool:
    return _norm(text) == "status"


def parse_role_choice(text: str, payload: str) -> str | None:
    pl = (payload or "").strip().lower()
    if pl == "role:homeowner":
        return "homeowner"
    if pl == "role:pro":
        return "pro"
    norm = _norm(text)
    if norm in _HOMEOWNER_ANSWERS:
        return "homeowner"
    if norm in _PRO_ANSWERS:
        return "pro"
    return None


def _looks_like_join(text: str) -> bool:
    parts = (text or "").strip().split()
    return bool(parts) and parts[0].upper() == "JOIN"


def _submitted_code(text: str) -> str:
    parts = (text or "").strip().split(maxsplit=1)
    if parts and parts[0].upper() == "JOIN":
        return parts[1].strip() if len(parts) > 1 else ""
    return (text or "").strip()


def context_points_to_homeowner_job(mode: str, context: dict | None) -> bool:
    ctx = context or {}
    if mode in HOMEOWNER_JOB_MODES:
        return True
    return bool(ctx.get("option_phones") or ctx.get("diagnosis") or ctx.get("diy_pending"))


def resolve_actor(
    pro: dict | None,
    homeowner: dict | None,
    *,
    message_type: str,
    mode: str,
    context: dict | None,
) -> str:
    """pro, homeowner, or unknown. Dual-role numbers are pros unless a photo or homeowner job."""
    has_pro = bool(pro)
    has_ho = bool(homeowner)
    if has_pro and has_ho:
        if message_type == "image":
            return "homeowner"
        if context_points_to_homeowner_job(mode, context):
            return "homeowner"
        return "pro"
    if has_pro:
        return "pro"
    if has_ho:
        return "homeowner"
    return "unknown"


def waiting_reminder(mode: str, context: dict | None) -> str:
    ctx = context or {}
    if mode in {MODE_AWAITING_PICK, MODE_AWAITING_SHOW_OPTIONS}:
        n = len(ctx.get("option_phones") or [])
        if n:
            return f"You still have {n} pro options to choose from."
        return "You still have pro options to choose from. Reply with a number or a name."
    if mode == MODE_PRO_AWAITING_DETAILS:
        return "Reply with when you can come and a rough estimate."
    if mode == MODE_PRO_ONBOARDING:
        return "You're still setting up your pro profile. Reply with the answer to the last question."
    if mode == MODE_AWAITING_JOIN_CODE:
        return "Reply with your pro join code to continue."
    if mode == "awaiting_area":
        return "Send your 5-digit ZIP code to continue."
    if mode == "awaiting_dispatch_consent":
        return "Reply *Yes* to find pros, or *No thanks*."
    if mode == "awaiting_share_notes":
        return "Reply *Yes* or *No* about sharing your access notes."
    if mode == "awaiting_quote_question":
        return "Send the question you want me to pass to that pro."
    if mode == "awaiting_new_issue_confirm":
        return "Reply *Yes* if this is a new issue, or *No* to keep the current one."
    if mode == "awaiting_feedback":
        return "Tap *Great*, *Okay*, or *Not good* to rate the visit."
    if mode == "awaiting_feedback_comment":
        return "Send your feedback as a text or voice note."
    return "Reply when you're ready and I'll pick up where we left off."


def _menu_role(mode: str, role: str) -> str:
    if mode in PRO_FLOW_MODES or role == "pro":
        return "pro"
    return "homeowner"


def _fail_join(context: dict) -> WelcomeDecision:
    attempts = int(context.get("join_attempts") or 0) + 1
    if attempts >= 3:
        return WelcomeDecision("join_locked", clear_state=True, extra={"attempts": attempts})
    return WelcomeDecision(
        "join_fail",
        next_mode=MODE_AWAITING_JOIN_CODE,
        next_context={"join_attempts": attempts},
        extra={"attempts": attempts, "left": 3 - attempts},
    )


def decide(
    *,
    role: str,
    text: str,
    payload: str = "",
    message_type: str = "text",
    mode: str = MODE_IDLE,
    context: dict | None = None,
    has_active_job: bool = False,
    pending_count: int = 0,
    opted_in: bool = False,
    join_code: str = "",
    flow_reply: bool = False,
) -> WelcomeDecision:
    """Pure routing for the welcome flow. passthrough means the normal pipeline continues."""
    mode = mode or MODE_IDLE
    ctx = dict(context or {})
    text = text or ""
    payload = payload or ""
    join_code = (join_code or "").strip()

    if is_dashboard_command(text):
        return WelcomeDecision("send_dashboard")

    if is_pro_signup(text) and not (
        mode in IN_PROGRESS_MODES and mode != MODE_AWAITING_JOIN_CODE
    ):
        return WelcomeDecision("start_pro_onboarding")

    if _looks_like_join(text) and not (
        mode in IN_PROGRESS_MODES and mode != MODE_AWAITING_JOIN_CODE
    ):
        supplied = _submitted_code(text)
        if not join_code or supplied == join_code:
            return WelcomeDecision("start_pro_onboarding")
        if mode == MODE_AWAITING_JOIN_CODE:
            return _fail_join(ctx)
        attempts = 1 if supplied else 0
        action = "join_fail" if supplied else "ask_join_code"
        return WelcomeDecision(
            action,
            next_mode=MODE_AWAITING_JOIN_CODE,
            next_context={"join_attempts": attempts},
            extra={"attempts": attempts, "left": max(0, 3 - attempts)},
        )

    if message_type == "image" and (
        role == "unknown"
        or mode == MODE_AWAITING_ROLE
        or (role == "homeowner" and mode in PRO_FLOW_MODES)
    ):
        return WelcomeDecision("create_homeowner_and_continue", clear_state=True)

    if flow_reply:
        return WelcomeDecision("passthrough")

    if mode in IN_PROGRESS_MODES and (
        is_help(text)
        or (is_greeting_or_vague(text) and not (payload or "").strip() and not flow_reply)
    ):
        kind = "midflow_help" if is_help(text) else "midflow_status"
        return WelcomeDecision(
            kind,
            clear_state=False,
            reminder=waiting_reminder(mode, ctx),
            menu_role=_menu_role(mode, role),
        )

    if is_help(text):
        if role == "unknown" or mode == MODE_AWAITING_ROLE:
            return WelcomeDecision(
                "show_welcome",
                next_mode=MODE_AWAITING_ROLE,
                next_context=ctx if mode == MODE_AWAITING_ROLE else {"role_reprompts": 0},
            )
        if role == "pro":
            return WelcomeDecision("help_pro")
        return WelcomeDecision("help_homeowner")

    if (
        is_greeting_or_vague(text)
        and mode != MODE_AWAITING_ROLE
        and not (payload or "").strip().lower().startswith("role:")
    ):
        if role == "unknown":
            return WelcomeDecision(
                "show_welcome",
                next_mode=MODE_AWAITING_ROLE,
                next_context={"role_reprompts": 0},
            )
        if role == "pro":
            if not opted_in:
                return WelcomeDecision("status_pro_unsubscribed")
            if pending_count:
                return WelcomeDecision("status_pro_pending")
            return WelcomeDecision("status_pro_idle")
        if has_active_job:
            return WelcomeDecision("status_homeowner_job", clear_state=False)
        return WelcomeDecision("status_homeowner_idle")

    if mode == MODE_AWAITING_ROLE or (payload or "").strip().lower().startswith("role:"):
        choice = parse_role_choice(text, payload)
        if choice == "homeowner":
            return WelcomeDecision("confirm_homeowner", clear_state=True)
        if choice == "pro":
            if join_code:
                return WelcomeDecision(
                    "ask_join_code",
                    next_mode=MODE_AWAITING_JOIN_CODE,
                    next_context={"join_attempts": 0},
                )
            return WelcomeDecision("start_pro_onboarding")
        if mode == MODE_AWAITING_ROLE and is_greeting_or_vague(text):
            return WelcomeDecision(
                "show_welcome",
                next_mode=MODE_AWAITING_ROLE,
                next_context=ctx or {"role_reprompts": 0},
            )
        if mode == MODE_AWAITING_ROLE:
            reps = int(ctx.get("role_reprompts") or 0)
            if reps >= 1:
                return WelcomeDecision("default_homeowner", clear_state=True)
            ctx["role_reprompts"] = reps + 1
            return WelcomeDecision(
                "reprompt_role",
                next_mode=MODE_AWAITING_ROLE,
                next_context=ctx,
            )

    if mode == MODE_AWAITING_JOIN_CODE:
        supplied = _submitted_code(text)
        if join_code and supplied == join_code:
            return WelcomeDecision("join_ok")
        if not join_code:
            return WelcomeDecision("start_pro_onboarding")
        return _fail_join(ctx)

    if (
        is_status_keyword(text)
        and role == "homeowner"
        and mode in {MODE_IDLE, MODE_AWAITING_PICK, MODE_AWAITING_SHOW_OPTIONS}
    ):
        if has_active_job:
            return WelcomeDecision("status_homeowner_job", clear_state=False)
        return WelcomeDecision("status_homeowner_idle", clear_state=False)

    if role == "unknown" and mode in {MODE_IDLE, MODE_AWAITING_ROLE}:
        if message_type == "image":
            return WelcomeDecision("create_homeowner_and_continue", clear_state=True)
        if (text or "").strip() and not is_greeting_or_vague(text) and not is_help(text):
            return WelcomeDecision("create_homeowner_and_continue", clear_state=True)
        return WelcomeDecision(
            "show_welcome",
            next_mode=MODE_AWAITING_ROLE,
            next_context={"role_reprompts": 0},
        )

    return WelcomeDecision("passthrough")


def _job_is_active(job: dict | None) -> bool:
    return bool(job) and (job or {}).get("state") in ACTIVE_STATES


async def _load_actor(phone: str, message_type: str, state: dict) -> dict[str, Any]:
    mode = state.get("mode") or MODE_IDLE
    ctx = dict(state.get("context") or {})
    try:
        pro = await pro_store.get(phone)
    except Exception:
        pro = None
    try:
        homeowner = await homeowner_store.get(phone)
    except Exception:
        homeowner = None
    role = resolve_actor(
        pro, homeowner, message_type=message_type, mode=mode, context=ctx
    )
    job = None
    response_count = 0
    if homeowner and homeowner.get("active_job_id"):
        try:
            job = await job_store.get_job(homeowner["active_job_id"])
        except Exception:
            job = None
        if _job_is_active(job):
            try:
                responses = await job_store.list_responses(job["job_id"])
            except Exception:
                responses = []
            response_count = sum(
                1 for r in responses if r.get("status") not in {None, "", "alerted"}
            )
        else:
            job = None
    pending: list[dict] = []
    if pro:
        for job_id in list(pro.get("pending_alert_job_ids") or [])[:5]:
            try:
                item = await job_store.get_job(job_id)
            except Exception:
                item = None
            if item:
                pending.append(item)
            else:
                pending.append({"job_id": job_id, "trade": "repair", "area_zip": ""})
    return {
        "role": role,
        "pro": pro,
        "homeowner": homeowner,
        "job": job,
        "response_count": response_count,
        "pending": pending,
    }


def _status_lines(job: dict, response_count: int) -> str:
    trade = job.get("trade") or "repair"
    state = job.get("state") or "open"
    return (
        f"Your *{trade}* request is *{state}*.\n"
        f"Pro responses so far: {response_count}."
    )


def _pending_lines(pending: list[dict]) -> str:
    lines = ["You have open job alerts:"]
    for job in pending[:5]:
        trade = job.get("trade") or "repair"
        area = job.get("area_zip") or "your area"
        lines.append(f"- {trade} near {area}")
    lines.append("Reply *Interested* or *Not available*.")
    return "\n".join(lines)


async def _send_role_prompt(phone: str) -> None:
    sid = (settings.tpl_welcome_role_sid or "").strip()
    if sid:
        try:
            await whatsapp_service.send_content_template(phone, content_sid=sid)
            return
        except Exception as e:
            logger.warning("welcome role template failed: %s", e)
    await whatsapp_service.send_text_message(
        phone,
        WELCOME_TEXT + "\n\nReply *1* if you're a homeowner or *2* if you're a pro.",
    )


async def _remember_role(phone: str, mode: str | None, context: dict | None, *, clear: bool) -> None:
    if clear:
        await conversation_state_store.clear(phone)
        return
    if mode:
        await conversation_state_store.set(phone, mode, context or {})


async def handle_welcome(message: Any, phone: str, state: dict) -> bool:
    """
    Handle role welcome, help, and returning-user greetings.
    Return True when the message is consumed. False to continue diagnosis / pro flows.
    """
    phone_n = normalize_phone(phone)
    mode = state.get("mode") or MODE_IDLE
    ctx = dict(state.get("context") or {})
    loaded = await _load_actor(phone_n, message.type or "text", state)
    decision = decide(
        role=loaded["role"],
        text=message.body or "",
        payload=message.button_payload or "",
        message_type=message.type or "text",
        mode=mode,
        context=ctx,
        has_active_job=loaded["job"] is not None,
        pending_count=len(loaded["pending"]),
        opted_in=bool((loaded["pro"] or {}).get("opted_in")),
        join_code=settings.pro_join_code or "",
        flow_reply=bool((getattr(message, "interactive_data", None) or "").strip())
        or (
            (message.type or "") == "interactive"
            and not (message.body or "").strip()
            and not (message.button_payload or "").strip()
        ),
    )
    if decision.action == "passthrough":
        return False

    if decision.action == "create_homeowner_and_continue":
        await homeowner_store.ensure(phone_n)
        if decision.clear_state or mode == MODE_AWAITING_ROLE:
            await conversation_state_store.clear(phone_n)
        await log_event(
            phone=phone_n,
            role="homeowner",
            type="role_selected",
            payload={"role": "homeowner", "via": "issue_or_photo"},
        )
        return False

    await _apply(phone_n, decision, loaded)
    return True


async def _apply(phone: str, decision: WelcomeDecision, loaded: dict[str, Any]) -> None:
    action = decision.action
    job = loaded.get("job")
    pending = loaded.get("pending") or []

    if action in {"show_welcome", "reprompt_role"}:
        await _send_role_prompt(phone)
        await _remember_role(phone, decision.next_mode, decision.next_context, clear=False)
        await log_event(
            phone=phone,
            role="system",
            type="welcome_shown",
            payload={"reprompt": action == "reprompt_role"},
        )
        return

    if action == "confirm_homeowner":
        await homeowner_store.ensure(phone)
        await conversation_state_store.clear(phone)
        await whatsapp_service.send_text_message(phone, HOMEOWNER_READY)
        await log_event(
            phone=phone,
            role="homeowner",
            type="role_selected",
            payload={"role": "homeowner"},
        )
        return

    if action == "default_homeowner":
        await homeowner_store.ensure(phone)
        await conversation_state_store.clear(phone)
        await whatsapp_service.send_text_message(phone, HOMEOWNER_READY)
        await log_event(
            phone=phone,
            role="homeowner",
            type="role_defaulted",
            payload={"role": "homeowner"},
        )
        return

    if action in {"ask_join_code", "join_fail"}:
        left = decision.extra.get("left")
        if action == "join_fail":
            text = JOIN_CODE_WRONG + (
                f"You have {left} attempt{'s' if left != 1 else ''} left."
                if left
                else "Reply with the pro join code."
            )
        else:
            text = JOIN_CODE_PROMPT
        await _remember_role(phone, decision.next_mode, decision.next_context, clear=False)
        await whatsapp_service.send_text_message(phone, text)
        if action == "join_fail":
            await log_event(
                phone=phone,
                role="pro",
                type="join_code_failed",
                payload={"attempts": decision.extra.get("attempts")},
            )
        return

    if action == "join_locked":
        await conversation_state_store.clear(phone)
        await whatsapp_service.send_text_message(phone, JOIN_CODE_LOCKED)
        await log_event(
            phone=phone,
            role="pro",
            type="join_code_locked",
            payload={"attempts": decision.extra.get("attempts")},
        )
        return

    if action == "send_dashboard":
        from app.services.portal_auth import send_dashboard_reply

        await send_dashboard_reply(phone)
        return

    if action in {"start_pro_onboarding", "join_ok"}:
        await start_pro_onboarding(phone)
        await log_event(
            phone=phone,
            role="pro",
            type="role_selected",
            payload={"role": "pro"},
        )
        return

    if action == "help_homeowner":
        await whatsapp_service.send_text_message(phone, HELP_HOMEOWNER)
        return

    if action == "help_pro":
        await whatsapp_service.send_text_message(phone, help_pro_text())
        return

    if action == "status_homeowner_idle":
        await whatsapp_service.send_text_message(phone, RETURNING_HOMEOWNER)
        return

    if action == "status_homeowner_job" and job:
        await whatsapp_service.send_text_message(
            phone, _status_lines(job, loaded.get("response_count") or 0)
        )
        if job.get("state") in {MODE_AWAITING_PICK, "awaiting_pick"}:
            from app.services.dispatch import maybe_send_homeowner_options

            await maybe_send_homeowner_options(job["job_id"], force=True)
        return

    if action == "status_pro_unsubscribed":
        await whatsapp_service.send_text_message(phone, PRO_UNSUBSCRIBED)
        return

    if action == "status_pro_pending":
        await whatsapp_service.send_text_message(phone, _pending_lines(pending))
        return

    if action == "status_pro_idle":
        await whatsapp_service.send_text_message(phone, PRO_IDLE)
        return

    if action in {"midflow_help", "midflow_status"}:
        if action == "midflow_help":
            menu = help_pro_text() if decision.menu_role == "pro" else HELP_HOMEOWNER
            await whatsapp_service.send_text_message(phone, menu)
        elif job and decision.menu_role != "pro":
            await whatsapp_service.send_text_message(
                phone, _status_lines(job, loaded.get("response_count") or 0)
            )
        if decision.reminder:
            await whatsapp_service.send_text_message(phone, decision.reminder)
        if job and job.get("state") == "awaiting_pick" and decision.menu_role != "pro":
            from app.services.dispatch import maybe_send_homeowner_options

            await maybe_send_homeowner_options(job["job_id"], force=True)
        return

    logger.warning("unhandled welcome action %s", action)
