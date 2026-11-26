"""Inbound WhatsApp limits applied before Gemini or a pro dispatch.

Per-phone counts live in this process. Cloud Run runs more than one instance,
so a burst can be split across them. That still stops a single instance from
burning tokens, and the webhook checks before any model call is queued.
"""

from __future__ import annotations

import threading
import time

MAX_BODY_CHARS = 1000
BURST_LIMIT = 15
BURST_WINDOW_SECONDS = 10 * 60
DAILY_LIMIT = 60
DAILY_WINDOW_SECONDS = 24 * 60 * 60
REPLY_COOLDOWN_SECONDS = BURST_WINDOW_SECONDS

RATE_LIMIT_REPLY = (
    "You're sending messages a bit too fast—please wait a few minutes and try again."
)


def cap_text(text: str, limit: int = MAX_BODY_CHARS) -> str:
    """Keep the first `limit` characters. Empty input stays empty."""
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit]


class InboundRateLimiter:
    """Sliding-window counter. `admit` returns ok, reply, or silent."""

    def __init__(self) -> None:
        self._events: dict[str, list[float]] = {}
        self._replied_at: dict[str, float] = {}
        self._lock = threading.Lock()

    def admit(self, phone: str, now: float | None = None) -> str:
        """Record one inbound message.

        ok — under both limits.
        reply — over a limit, and the static reply has not been sent in this cooldown.
        silent — over a limit, and a reply was already sent recently.
        """
        key = (phone or "").strip()
        if not key:
            return "ok"
        moment = time.time() if now is None else now
        with self._lock:
            self._drop_stale(moment)
            recent = [
                stamp
                for stamp in self._events.get(key, [])
                if moment - stamp < DAILY_WINDOW_SECONDS
            ]
            recent.append(moment)
            self._events[key] = recent
            burst = sum(1 for stamp in recent if moment - stamp < BURST_WINDOW_SECONDS)
            if burst <= BURST_LIMIT and len(recent) <= DAILY_LIMIT:
                return "ok"
            last_reply = self._replied_at.get(key, 0.0)
            if moment - last_reply < REPLY_COOLDOWN_SECONDS:
                return "silent"
            self._replied_at[key] = moment
            return "reply"

    def _drop_stale(self, now: float) -> None:
        stale = [
            phone
            for phone, stamps in self._events.items()
            if not stamps or now - stamps[-1] >= DAILY_WINDOW_SECONDS
        ]
        for phone in stale:
            self._events.pop(phone, None)
            self._replied_at.pop(phone, None)


inbound_limiter = InboundRateLimiter()
