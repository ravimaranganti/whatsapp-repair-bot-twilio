"""Parse homeowner taps so each button names its job and quote."""

from __future__ import annotations

from dataclasses import dataclass

_BOOK_TEXT = frozenset(
    {"book", "book this pro", "sounds good", "i'll take it", "ill take it"}
)
_PASS_TEXT = frozenset({"pass", "decline quote"})
_YES_TEXT = frozenset({"yes", "y", "yeah", "sure"})


@dataclass
class HomeownerTap:
    action: str
    job_id: str = ""
    quote_id: str = ""
    outcome: str = ""


def parse_homeowner_payload(payload: str) -> HomeownerTap | None:
    """
    book_quote:job:quote, pass_quote:job:quote, decline_quote:job:quote,
    ask_quote:job:quote, reply_pro_q:job:pro, keep_looking:job, close_job:job, retry_pros:job,
    confirm_job:job, cancel_job:job, show_options:job,
    share:yes:job:quote, share:no:job:quote,
    new_issue:yes:job, new_issue:no:job,
    rating_great:job, rating_okay:job, rating_not_good:job,
    and the older pick:job:quote form.
    """
    pl = (payload or "").strip()
    if not pl or ":" not in pl:
        return None
    parts = pl.split(":")
    action = parts[0].strip().lower()
    rest = [part.strip() for part in parts[1:]]
    if action == "pick" and len(rest) >= 2:
        return HomeownerTap("book_quote", rest[0], rest[1])
    if action == "reply_pro_q" and len(rest) >= 2:
        return HomeownerTap("reply_pro_q", rest[0], rest[1])
    if action == "job_outcome" and len(rest) >= 2:
        outcome = rest[1].strip().upper()
        if outcome in {"COMPLETED", "SCHEDULED", "FELL_THROUGH"}:
            return HomeownerTap("job_outcome", rest[0], outcome=outcome)
    if action == "reopen_job" and rest and rest[0]:
        return HomeownerTap("reopen_job", rest[0])
    if action in {"book_quote", "ask_quote", "pass_quote", "decline_quote"} and len(rest) >= 2:
        name = "pass_quote" if action in {"pass_quote", "decline_quote"} else action
        return HomeownerTap(name, rest[0], rest[1])
    if action == "urgency" and len(rest) >= 2 and rest[0] in {"today", "flexible"}:
        name = "urgency_today" if rest[0] == "today" else "urgency_flexible"
        return HomeownerTap(name, rest[1])
    if action in {"keep_looking", "close_job", "retry_pros", "keep_waiting", "direct_numbers"} and rest and rest[0]:
        return HomeownerTap(action, rest[0])
    if action == "share" and len(rest) >= 3 and rest[0] in {"yes", "no"}:
        name = "share_yes" if rest[0] == "yes" else "share_no"
        return HomeownerTap(name, rest[1], rest[2])
    if action == "new_issue" and len(rest) >= 2 and rest[0] in {"yes", "no"}:
        name = "new_issue_yes" if rest[0] == "yes" else "new_issue_no"
        return HomeownerTap(name, rest[1])
    if action in {"confirm_job", "cancel_job", "show_options"} and rest and rest[0]:
        return HomeownerTap(action, rest[0])
    if action == "consent" and len(rest) >= 2 and rest[0] in {"yes", "no"}:
        name = "confirm_job" if rest[0] == "yes" else "cancel_job"
        return HomeownerTap(name, rest[1])
    if action in {"rating_great", "rating_okay", "rating_not_good"} and rest and rest[0]:
        return HomeownerTap(action, rest[0])
    return None


def typed_choice(text: str) -> str | None:
    """A generic reply such as Book, Pass, or Yes. None when it is a real sentence."""
    low = " ".join((text or "").lower().split())
    if low in _BOOK_TEXT:
        return "book"
    if low in _PASS_TEXT:
        return "pass"
    if low in _YES_TEXT:
        return "yes"
    return None


def quote_choice_message(options: list[dict]) -> str:
    lines = [
        f"You have {len(options)} open quotes. "
        "Please tap Book This Pro on the quote message above."
    ]
    for option in options[:5]:
        name = option.get("name") or "Pro"
        lines.append(f"- {name} (#{option.get('job_id')})")
    return "\n".join(lines)
