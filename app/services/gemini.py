"""
Gemini AI Service — structured diagnosis + tool-calling chat.
Uses google-genai SDK with system_instruction (not concatenated into user content).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from google import genai
from google.genai import types

from app.config import settings
from app.models.diagnosis import Diagnosis
from app.prompts.persona import BASE_PERSONA, CHAT_INSTRUCTIONS, DIAGNOSIS_INSTRUCTIONS
from app.services.chat_tools import TOOL_DECLARATIONS, execute_tool
from app.services.events import log_event
from app.services.safety import apply_safety_overrides

logger = logging.getLogger(__name__)

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_TIMEOUT_SECONDS = 50


def _diagnosis_schema() -> dict[str, Any]:
    """JSON schema for Gemini structured output (from Pydantic)."""
    return Diagnosis.model_json_schema()


class GeminiService:
    def __init__(self) -> None:
        self._client: genai.Client | None = None

    @property
    def client(self) -> genai.Client:
        if self._client is None:
            self._client = genai.Client(api_key=settings.gemini_api_key)
        return self._client

    def _context_block(
        self,
        prior_turns: list[dict[str, str]] | None,
        memory_facts: list[str] | None,
        service_notes: list[str] | None,
    ) -> str:
        lines: list[str] = []
        if service_notes:
            lines.append("Service notes (homeowner-saved):")
            for n in service_notes[:15]:
                lines.append(f"- {n}")
            lines.append("")
        if memory_facts:
            lines.append("Long-term facts (may be stale; prefer recent conversation):")
            for f in memory_facts[:10]:
                lines.append(f"- {f}")
            lines.append("")
        if prior_turns:
            lines.append("Recent conversation:")
            for t in prior_turns[-10:]:
                role = "User" if t.get("role") == "user" else "Assistant"
                content = (t.get("content") or "").strip()
                if len(content) > 400:
                    content = content[:397] + "..."
                if content:
                    lines.append(f"{role}: {content}")
            lines.append("")
        return "\n".join(lines)

    async def analyze_image(
        self,
        image_bytes: bytes,
        user_message: str = "",
        mime_type: str = "image/jpeg",
        *,
        prior_turns: list[dict[str, str]] | None = None,
        memory_facts: list[str] | None = None,
        service_notes: list[str] | None = None,
        phone: str | None = None,
    ) -> Diagnosis:
        """Multimodal diagnosis → Diagnosis (with safety overrides). Retries once on parse failure."""
        ctx = self._context_block(prior_turns, memory_facts, service_notes)
        caption = f"\nUser caption: {user_message}" if user_message else ""
        prompt_text = (
            f"{ctx}Analyze the attached photo and return Diagnosis JSON.{caption}"
        )
        contents = [
            types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            prompt_text,
        ]
        system = f"{BASE_PERSONA}\n{DIAGNOSIS_INSTRUCTIONS}"
        last_err: Exception | None = None

        for attempt in range(2):
            t0 = time.monotonic()
            try:
                response = await asyncio.wait_for(
                    asyncio.to_thread(
                        self.client.models.generate_content,
                        model=GEMINI_MODEL,
                        contents=contents,
                        config=types.GenerateContentConfig(
                            system_instruction=system,
                            response_mime_type="application/json",
                            response_schema=Diagnosis,
                        ),
                    ),
                    timeout=GEMINI_TIMEOUT_SECONDS,
                )
                latency_ms = int((time.monotonic() - t0) * 1000)
                raw = (response.text or "").strip()
                diagnosis = Diagnosis.model_validate_json(raw)
                diagnosis = apply_safety_overrides(diagnosis)
                await log_event(
                    phone=phone,
                    role="system",
                    type="gemini_call",
                    payload={
                        "mode": "diagnosis",
                        "model": GEMINI_MODEL,
                        "latency_ms": latency_ms,
                        "attempt": attempt + 1,
                        "trade": diagnosis.trade.value,
                        "severity": diagnosis.severity.value,
                        "diy": diagnosis.diy_appropriate,
                        "confidence": diagnosis.confidence,
                    },
                )
                return diagnosis
            except Exception as e:
                last_err = e
                logger.error("Gemini diagnosis attempt %s failed: %s", attempt + 1, e)
                await log_event(
                    phone=phone,
                    role="system",
                    type="gemini_call",
                    payload={
                        "mode": "diagnosis",
                        "model": GEMINI_MODEL,
                        "attempt": attempt + 1,
                        "error": str(e),
                    },
                )

        raise RuntimeError(f"Diagnosis failed after retries: {last_err}")

    async def chat_reply(
        self,
        prior_turns: list[dict[str, str]],
        user_message: str,
        memory_facts: list[str] | None = None,
        service_notes: list[str] | None = None,
        *,
        phone: str | None = None,
    ) -> str:
        """Multi-turn chat with optional function calling."""
        user_message = (user_message or "").strip()
        if not user_message:
            return "Send me a message or a photo of the issue and I will help."

        system = f"{BASE_PERSONA}\n{CHAT_INSTRUCTIONS}"
        if service_notes:
            system += "\n\nKnown service notes:\n" + "\n".join(
                f"- {n}" for n in service_notes[:15]
            )
        if memory_facts:
            system += (
                "\n\nLong-term facts (possibly stale; prefer recent turns):\n"
                + "\n".join(f"- {f}" for f in memory_facts[:10])
            )

        contents: list[types.Content] = []
        for t in prior_turns[-20:]:
            role = "user" if t.get("role") == "user" else "model"
            text = (t.get("content") or "").strip()
            if not text:
                continue
            if len(text) > 500:
                text = text[:497] + "..."
            contents.append(
                types.Content(role=role, parts=[types.Part.from_text(text=text)])
            )
        contents.append(
            types.Content(
                role="user", parts=[types.Part.from_text(text=user_message)]
            )
        )

        # Build tool declarations for google-genai
        fn_decls = []
        for d in TOOL_DECLARATIONS:
            fn_decls.append(
                types.FunctionDeclaration(
                    name=d["name"],
                    description=d["description"],
                    parameters=d.get("parameters"),
                )
            )
        tools = [types.Tool(function_declarations=fn_decls)]

        t0 = time.monotonic()
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    self.client.models.generate_content,
                    model=GEMINI_MODEL,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=system,
                        tools=tools,
                    ),
                ),
                timeout=GEMINI_TIMEOUT_SECONDS,
            )
        except Exception as e:
            await log_event(
                phone=phone,
                role="system",
                type="gemini_call",
                payload={"mode": "chat", "error": str(e)},
            )
            raise

        # Handle function calls (single round-trip loop, max 3)
        for _ in range(3):
            fn_calls = []
            if response.candidates:
                for part in response.candidates[0].content.parts or []:
                    fc = getattr(part, "function_call", None)
                    if fc and getattr(fc, "name", None):
                        fn_calls.append(fc)
            if not fn_calls:
                break

            contents.append(response.candidates[0].content)
            fn_response_parts = []
            for fc in fn_calls:
                args = dict(fc.args) if fc.args else {}
                result = await execute_tool(
                    fc.name, args, homeowner_phone=phone or ""
                )
                fn_response_parts.append(
                    types.Part.from_function_response(
                        name=fc.name, response=result
                    )
                )
            contents.append(types.Content(role="user", parts=fn_response_parts))
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    self.client.models.generate_content,
                    model=GEMINI_MODEL,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=system,
                        tools=tools,
                    ),
                ),
                timeout=GEMINI_TIMEOUT_SECONDS,
            )

        latency_ms = int((time.monotonic() - t0) * 1000)
        text = (response.text or "").strip()
        if len(text) > 1500:
            text = text[:1497] + "..."
        await log_event(
            phone=phone,
            role="system",
            type="gemini_call",
            payload={
                "mode": "chat",
                "model": GEMINI_MODEL,
                "latency_ms": latency_ms,
                "chars": len(text),
            },
        )
        return text or "I am not sure how to answer that — try sending a photo of the issue."

    async def parse_pro_job_reply(
        self,
        *,
        message: str,
        profile_summary: str,
        job_summary: str,
        prior_draft: str,
    ) -> dict[str, Any]:
        """Classify a pro's free-form reply and pull out a quote or a question."""
        schema = {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "enum": [
                        "QUOTE_READY",
                        "QUESTION_ONLY",
                        "QUOTE_WITH_QUESTION",
                        "MISSING_INFO",
                    ],
                },
                "availability_text": {"type": "string"},
                "price_estimate": {"type": "string"},
                "caveats_or_notes": {"type": "string"},
                "question_for_homeowner": {"type": "string"},
                "missing_field_prompt": {"type": "string"},
            },
            "required": [
                "intent",
                "availability_text",
                "price_estimate",
                "caveats_or_notes",
                "question_for_homeowner",
                "missing_field_prompt",
            ],
        }
        prompt = (
            "Classify this professional's WhatsApp reply. They already tapped "
            "Interested on the job.\n"
            "QUOTE_READY: both a time they can come and a rough price, and no "
            "question for the homeowner.\n"
            "QUOTE_WITH_QUESTION: both a time and a price, plus a question the "
            "homeowner should answer.\n"
            "QUESTION_ONLY: they ask a clarifying question instead of quoting. "
            "Put that question in question_for_homeowner. Leave price empty. "
            "Do not treat this as a missing price or time.\n"
            "If they give a time and say they need to inspect, see the job, or "
            "visit before pricing the repair, extract the time, leave "
            "price_estimate empty, and put that note in caveats_or_notes. "
            "Do not copy the profile service-call fee into the price.\n"
            "MISSING_INFO: they are quoting a repair price but left out the time "
            "or the price, and they did not ask a question. "
            "missing_field_prompt asks only for what is still missing.\n"
            "Do not invent a price, a time, or a question. Keep their wording. "
            "A range stays a range. Combine the prior draft with this message.\n\n"
            f"Profile: {profile_summary}\n"
            f"Job: {job_summary}\n"
            f"Prior draft: {prior_draft}\n"
            f"New message: {message}"
        )
        response = await asyncio.wait_for(
            asyncio.to_thread(
                self.client.models.generate_content,
                model=GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=(
                        "You extract structured quote details from a professional's "
                        "short WhatsApp message. Return only the requested JSON."
                    ),
                    response_mime_type="application/json",
                    response_schema=schema,
                ),
            ),
            timeout=30,
        )
        return json.loads(response.text or "{}")

    async def extract_pro_details(self, free_text: str) -> dict[str, Any]:
        """Structured extract of availability + estimate from pro free text."""
        schema = {
            "type": "object",
            "properties": {
                "availability_text": {"type": "string"},
                "estimate_text": {"type": "string"},
                "estimate_low": {"type": "number"},
                "estimate_high": {"type": "number"},
            },
            "required": ["availability_text", "estimate_text"],
        }
        prompt = (
            "Extract the pro's availability and rough estimate from this message. "
            "If a single number, set low=high. Currency USD.\n\n"
            f"Message: {free_text}"
        )
        response = await asyncio.wait_for(
            asyncio.to_thread(
                self.client.models.generate_content,
                model=GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=BASE_PERSONA,
                    response_mime_type="application/json",
                    response_schema=schema,
                ),
            ),
            timeout=30,
        )
        return json.loads(response.text or "{}")


gemini_service = GeminiService()
