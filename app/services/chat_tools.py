"""Gemini function-calling tool implementations for chat mode."""

from __future__ import annotations

import logging
from typing import Any

from app.config import settings
from app.services.homeowner_profile import saved_zip
from app.services.homeowners import homeowner_store
from app.services.jobs import job_store
from app.services.matching import public_pro_card, select_pros_for_job
from app.services.places import search_local_specialists
from app.utils.phones import normalize_phone

logger = logging.getLogger(__name__)

_TRADE_HINTS = (
    ("hvac", ("hvac", "heater", "heating", "furnace", "thermostat", "air condition", "a/c", " ac ")),
    ("plumbing", ("plumb", "leak", "sink", "toilet", "faucet", "drain", "water heater")),
    ("electrical", ("electric", "outlet", "breaker", "wiring", "light switch")),
    ("appliance", ("appliance", "washer", "dryer", "dishwasher", "refrigerator", "fridge", "oven")),
    ("handyman", ("handyman", "mount", "paint", "drywall", "furniture")),
)


def _trade_hint(summary: str) -> str:
    low = f" {(summary or '').lower()} "
    if "water heater" in low:
        return "plumbing"
    for trade, words in _TRADE_HINTS:
        if any(word in low for word in words):
            return trade
    return ""


TOOL_DECLARATIONS = [
    {
        "name": "search_pros",
        "description": (
            "Search local opted-in professionals by trade and area ZIP. "
            "Returns display names, trade, rating, availability — never phone numbers."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "trade": {
                    "type": "string",
                    "description": "plumbing|hvac|electrical|handyman|appliance",
                },
                "area": {
                    "type": "string",
                    "description": "5-digit ZIP or neighborhood; ZIP preferred",
                },
            },
            "required": ["trade", "area"],
        },
    },
    {
        "name": "request_quotes",
        "description": (
            "Start quotes only after search_pros returned signed-up pros, the problem "
            "is specific, and the homeowner agreed. Do not call this when search_pros "
            "returned places. Do not call this on a vague first message such as "
            "'it is not working'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "job_summary": {
                    "type": "string",
                    "description": "Short summary of the issue to send to pros",
                },
                "trade": {
                    "type": "string",
                    "description": "plumbing|hvac|electrical|handyman|appliance",
                },
            },
            "required": ["job_summary"],
        },
    },
    {
        "name": "get_job_status",
        "description": "Get the current open job status for this homeowner.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "save_service_note",
        "description": (
            "Save a service note the homeowner wants remembered for a home visit "
            "(gate code, unit, parking, pets, preferred times)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "note": {"type": "string", "description": "The note to store"},
            },
            "required": ["note"],
        },
    },
]


async def execute_tool(
    name: str,
    args: dict[str, Any],
    *,
    homeowner_phone: str,
) -> dict[str, Any]:
    phone = normalize_phone(homeowner_phone)
    try:
        if name == "search_pros":
            trade = str(args.get("trade") or "handyman")
            area = str(args.get("area") or "")
            # Prefer ZIP digits if present
            import re

            zips = re.findall(r"\b\d{5}\b", area)
            area_zip = zips[0] if zips else area
            pros = await select_pros_for_job(
                trade=trade, area_zip=area_zip, severity="medium"
            )
            # If none opted-in yet, still search with only_opted_in relaxed for pilot visibility?
            # Spec: only opted_in for alerts; for search show opted-in only.
            if pros:
                return {
                    "count": len(pros),
                    "pros": [public_pro_card(p) for p in pros],
                    "places": [],
                    "note": "Phone numbers are hidden until the homeowner picks a pro.",
                }
            places = await search_local_specialists(trade, area_zip)
            return {
                "count": 0,
                "pros": [],
                "places": places,
                "note": (
                    "No signed-up pros. Read these Google listings to the customer now "
                    "with name, rating, and phone so they can call. Do not offer a job "
                    "request and do not say you will contact professionals."
                    if places
                    else "No signed-up pros and no public listings. Say that plainly. "
                    "Do not offer to send the job to professionals."
                ),
            }

        if name == "request_quotes":
            if not settings.dispatch_enabled:
                return {
                    "ok": False,
                    "error": "Dispatch is not enabled yet. Ask them to send a photo for diagnosis.",
                }
            summary = str(args.get("job_summary") or "").strip()
            if len(summary.split()) < 8:
                return {
                    "ok": False,
                    "error": (
                        "Too vague to contact a pro. Ask one specific symptom question "
                        "and do not offer pros until they answer."
                    ),
                }
            trade = str(args.get("trade") or _trade_hint(summary) or "").strip()
            ho = await homeowner_store.ensure(phone)
            if trade:
                area_zip = saved_zip(ho)
                pros = await select_pros_for_job(
                    trade=trade, area_zip=area_zip, severity="medium"
                )
                if not pros:
                    places = await search_local_specialists(trade, area_zip) if area_zip else []
                    return {
                        "ok": False,
                        "places": places,
                        "error": (
                            "No signed-up pros for this trade. Share the places list with "
                            "phone numbers. Do not say a job summary will be sent to professionals."
                        ),
                    }
            # Mark intent in conversation_state; agent/dispatch layer completes consent flow
            from app.models import MODE_AWAITING_DISPATCH_CONSENT
            from app.services.conversation_state import conversation_state_store

            await conversation_state_store.set(
                phone,
                MODE_AWAITING_DISPATCH_CONSENT,
                {
                    "pending_summary": summary,
                    "active_job_id": ho.get("active_job_id"),
                },
            )
            return {
                "ok": True,
                "next_step": "ask_consent",
                "message_for_user": (
                    "Want me to ask 3–5 local pros for availability and a rough estimate? "
                    "Your number stays private until you choose one. Reply *Yes* or *No*."
                ),
            }

        if name == "get_job_status":
            ho = await homeowner_store.get(phone)
            job_id = (ho or {}).get("active_job_id")
            if not job_id:
                return {"status": "none", "detail": "No active job."}
            job = await job_store.get_job(job_id)
            if not job:
                return {"status": "none", "detail": "No active job found."}
            return {
                "status": job.get("state"),
                "job_id": job_id,
                "trade": job.get("trade"),
                "severity": job.get("severity"),
            }

        if name == "save_service_note":
            note = str(args.get("note") or "").strip()
            if not note:
                return {"ok": False, "error": "Empty note"}
            await homeowner_store.add_service_note(phone, note)
            return {"ok": True, "saved": note[:200]}

        return {"ok": False, "error": f"Unknown tool {name}"}
    except Exception as e:
        logger.exception("tool %s failed", name)
        return {"ok": False, "error": str(e)}
