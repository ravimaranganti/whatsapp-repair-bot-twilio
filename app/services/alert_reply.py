"""Match a pro's Interested / Not available reply to one open job alert."""

from __future__ import annotations

import re
from dataclasses import dataclass

_ACCEPT_PAYLOADS = frozenset({"interested", "yes"})
_DECLINE_PAYLOADS = frozenset({"not_available", "declined", "no"})
_DECLINE_TEXT = frozenset({"no", "decline", "declined", "pass", "not available"})
_ACCEPT_TEXT = frozenset({"yes", "y", "interested"})
_VIEW_CMD = re.compile(r"^view\s+#?([a-f0-9]{8,32})\b", re.IGNORECASE)
_PASS_CMD = re.compile(r"^pass\s+#?([a-f0-9]{8,32})\b", re.IGNORECASE)


@dataclass
class AlertReply:
    handled: bool = False
    status: str | None = None  # interested | declined
    job_id: str | None = None
    clarify: str | None = None


def job_ref(job_id: str) -> str:
    return f"#{job_id}" if job_id else "#unknown"


def _split_payload(payload: str) -> tuple[str, str]:
    pl = (payload or "").strip()
    if ":" not in pl:
        return "", ""
    action, job_id = pl.split(":", 1)
    return action.strip().lower(), job_id.strip()


def _mentioned_jobs(text: str, open_job_ids: list[str]) -> list[str]:
    raw = (text or "").lower().replace("#", " ")
    found: list[str] = []
    for job_id in open_job_ids:
        jid = job_id.lower()
        if jid and jid in raw and job_id not in found:
            found.append(job_id)
    if found:
        return found
    for token in re.findall(r"\b[a-f0-9]{4,16}\b", raw):
        matches = [job_id for job_id in open_job_ids if job_id.lower().endswith(token)]
        if len(matches) == 1 and matches[0] not in found:
            found.append(matches[0])
    return found


def _clarify(open_job_ids: list[str]) -> str:
    shown = ", ".join(job_ref(job_id) for job_id in open_job_ids[:5])
    count = len(open_job_ids)
    return (
        f"You have {count} open job alerts ({shown}). "
        "Please tap Interested directly on the job message above, "
        "or reply with the job number."
    )


def _from_text(
    status: str, body: str, open_job_ids: list[str], *, from_button: bool
) -> AlertReply:
    matched = _mentioned_jobs(body, open_job_ids)
    if len(matched) == 1:
        return AlertReply(handled=True, status=status, job_id=matched[0])
    if len(matched) > 1:
        return AlertReply(handled=True, clarify=_clarify(matched))
    if len(open_job_ids) == 1:
        return AlertReply(handled=True, status=status, job_id=open_job_ids[0])
    if len(open_job_ids) > 1:
        return AlertReply(handled=True, clarify=_clarify(open_job_ids))
    if from_button:
        return AlertReply(
            handled=True,
            clarify="You don't have an open job alert right now.",
        )
    return AlertReply()


def resolve_alert_reply(
    payload: str, body: str, open_job_ids: list[str] | None = None
) -> AlertReply:
    """
    A button that carries a job id always wins.
    A typed Interested / Not available uses a reference in the text, or the
    only open alert. Several open alerts and no reference asks them to choose.
    """
    open_ids = [job_id for job_id in (open_job_ids or []) if job_id]
    action, job_id = _split_payload(payload)
    if action in {"accept_job", "int"} and job_id:
        return AlertReply(handled=True, status="interested", job_id=job_id)
    if action in {"decline_job", "dec"} and job_id:
        return AlertReply(handled=True, status="declined", job_id=job_id)

    pl = (payload or "").strip().lower()
    low = " ".join((body or "").lower().split())
    view = _VIEW_CMD.match(low)
    if view:
        return AlertReply(handled=True, status="view", job_id=view.group(1).lower())
    passed = _PASS_CMD.match(low)
    if passed:
        return AlertReply(handled=True, status="declined", job_id=passed.group(1).lower())
    if pl in _ACCEPT_PAYLOADS:
        return _from_text("interested", body, open_ids, from_button=True)
    if pl in _DECLINE_PAYLOADS:
        return _from_text("declined", body, open_ids, from_button=True)
    if "not available" in low or low in _DECLINE_TEXT:
        return _from_text("declined", body, open_ids, from_button=False)
    if low in _ACCEPT_TEXT or low.startswith("interested"):
        return _from_text("interested", body, open_ids, from_button=False)
    return AlertReply()
