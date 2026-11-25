"""Pro dashboard auth, job ownership, and landing page."""

import asyncio
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import app.portal_routes as routes
import app.services.portal_auth as auth
from app.main import app
from app.services.portal_auth import MemoryMagicTokenStore, read_session, sign_session
from app.services.portal_commands import is_dashboard_command, is_pro_signup
from app.services.pro_portal import (
    bucket_for_pro,
    can_answer_alert,
    can_complete,
    clean_profile_form,
    history_badge,
)
from app.services.welcome import decide


PHONE = "15551230000"
SECRET = "test-dashboard-secret"
JOB = "a1b2c3d4e5f67890"


def test_commands_match_the_whole_message():
    assert is_dashboard_command("dashboard")
    assert is_dashboard_command("My Jobs")
    assert is_dashboard_command("portal")
    assert not is_dashboard_command("my jobs are full this week")
    assert is_pro_signup("Pro Signup")
    assert is_pro_signup("pro sign up")
    assert not is_pro_signup("I need a pro for a signup")


def test_welcome_routes_signup_and_dashboard_before_homeowner_intake():
    signup = decide(
        role="unknown", text="Pro Signup", message_type="text", mode="idle"
    )
    assert signup.action == "start_pro_onboarding"
    dashboard = decide(
        role="unknown", text="dashboard", message_type="text", mode="awaiting_pick"
    )
    assert dashboard.action == "send_dashboard"
    homeowner = decide(
        role="unknown",
        text="Hi HomePro, I need help with a home project",
        message_type="text",
        mode="idle",
    )
    assert homeowner.action == "create_homeowner_and_continue"


def test_session_cookie_expires():
    later = datetime.now(timezone.utc) + timedelta(hours=2)
    cookie = sign_session(PHONE, later, SECRET)
    assert read_session(cookie, SECRET) == PHONE
    assert read_session(cookie, "other-secret") is None
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    expired = sign_session(PHONE, past, SECRET)
    assert read_session(expired, SECRET) is None


def test_job_buckets_hide_other_pros_and_open_alerts():
    job = {
        "state": "assigned",
        "selected_pro_phone": PHONE,
        "alerted_pro_phones": [PHONE, "15559990000"],
    }
    assert bucket_for_pro(PHONE, job, {"status": "selected", "pro_phone": PHONE}) == "booked"
    assert can_complete(PHONE, job)
    assert not can_complete("15559990000", job)
    assert history_badge(PHONE, {"state": "completed", "selected_pro_phone": PHONE}, {}) == "Completed"

    open_job = {
        "state": "dispatched",
        "selected_pro_phone": "",
        "alerted_pro_phones": [PHONE],
    }
    assert bucket_for_pro(PHONE, open_job, {"status": "alerted", "pro_phone": PHONE}) == "open"
    assert can_answer_alert(PHONE, open_job, {"status": "alerted"})
    assert not can_answer_alert(PHONE, open_job, {"status": "details_given"})
    assert bucket_for_pro("15559990000", open_job, None) is None
    passed = dict(open_job)
    assert history_badge(PHONE, passed, {"status": "declined"}) == "Declined"
    assert history_badge(PHONE, passed, {"status": "not_selected"}) == "Not Selected"
    assert history_badge(PHONE, passed, {"status": "details_given"}) == "Quoted - Pending"


def test_profile_form_uses_existing_area_and_fee_keys():
    saved = clean_profile_form(
        {
            "accepts_emergency": "1",
            "standard_lead_time": "next_48_hours",
            "dispatch_fee_tier": "tier_50_99",
            "service_areas": ["sugar_land", "katy", "inner_loop"],
            "languages": ["en"],
        }
    )
    assert saved["service_areas"] == ["sugar_land", "katy"]
    assert saved["standard_lead_time"] == "next_48_hours"
    assert saved["dispatch_fee_tier"] == "tier_50_99"
    assert "77478" in saved["service_zips"]
    assert saved["accepts_emergency"] is True
    all_areas = clean_profile_form(
        {
            "standard_lead_time": "same_day",
            "dispatch_fee_tier": "free_estimate",
            "service_areas": ["all", "houston"],
            "languages": ["es"],
        }
    )
    assert all_areas["service_areas"] == ["all"]
    assert all_areas["service_zips"] == []
    assert clean_profile_form({"standard_lead_time": "SAME_DAY"}) == "Choose a lead time."
    with_language = clean_profile_form({
        "accepts_emergency": "1",
        "standard_lead_time": "next_48_hours",
        "dispatch_fee_tier": "tier_50_99",
        "service_areas": ["sugar_land"],
        "languages": ["en", "es"],
        "insured": "yes",
        "free_photo_estimate": "yes",
        "profile_notes": "12 years, tankless heaters",
    })
    assert with_language["languages"] == ["en", "es"]
    assert with_language["insured"] is True
    assert with_language["free_photo_estimate"] is True
    assert "tankless" in with_language["profile_notes"]


def _client(monkeypatch):
    monkeypatch.setattr(routes, "session_secret", lambda: SECRET)
    monkeypatch.setattr(auth, "token_store", MemoryMagicTokenStore())
    return TestClient(app)


def test_landing_and_magic_link_login(monkeypatch):
    client = _client(monkeypatch)
    landing = client.get("/")
    assert landing.status_code == 200
    assert "Chat on WhatsApp" in landing.text
    assert "Join as a Pro" in landing.text
    assert "Pro%20Signup" in landing.text
    assert "without the phone tag" in landing.text

    locked = client.get("/pro/dashboard")
    assert locked.status_code == 401
    assert "Send dashboard" in locked.text

    raw = "magic-token-value-with-enough-length"
    expires = datetime.now(timezone.utc) + timedelta(hours=5)
    asyncio.run(auth.token_store.put(auth.token_id(raw), PHONE, expires))
    started = client.get(f"/pro/auth?token={raw}", follow_redirects=False)
    assert started.status_code == 303
    assert started.headers["location"] == "/pro/dashboard"
    assert "httponly" in started.headers["set-cookie"].lower()

    async def fake_dashboard(phone: str):
        assert phone == PHONE
        return {
            "pro": {},
            "business": "Mike's Plumbing",
            "booked": [
                {
                    "job_id": JOB,
                    "ref": f"#{JOB}",
                    "trade": "plumbing",
                    "place": "Sugar Land, 77478",
                    "issue": "Leak under the sink",
                    "when": "Tomorrow",
                    "estimate": "$150",
                    "date": "Nov 25, 2026",
                    "badge": "",
                    "needs_quote": False,
                    "show_contact": True,
                    "homeowner_name": "Sam",
                    "homeowner_phone": "+15557654321",
                    "access_notes": ["Gate 4455"],
                }
            ],
            "open_alerts": [
                {
                    "job_id": "bbbbbbbbbbbbbbbb",
                    "ref": "#bbbbbbbbbbbbbbbb",
                    "trade": "hvac",
                    "place": "Katy, 77494",
                    "issue": "AC not cooling",
                    "when": "",
                    "estimate": "",
                    "date": "",
                    "badge": "",
                    "needs_quote": False,
                    "show_contact": False,
                    "homeowner_name": "",
                    "homeowner_phone": "",
                    "access_notes": [],
                }
            ],
            "history": [],
            "areas": [{"key": "sugar_land", "label": "Sugar Land", "note": "Southwest", "checked": True}],
            "lead": "same_day",
            "fee": "free_estimate",
            "emergency": True,
            "lead_choices": [{"key": "same_day", "label": "Same day"}],
            "fee_choices": [{"key": "free_estimate", "label": "Free estimate"}],
        }

    monkeypatch.setattr(routes, "load_dashboard", fake_dashboard)
    client.cookies.update(started.cookies)
    page = client.get("/pro/dashboard")
    assert page.status_code == 200
    assert "Leak under the sink" in page.text
    assert "Mark completed" in page.text
    profile = client.get("/pro/dashboard?tab=profile")
    assert profile.status_code == 200
    assert "English" in profile.text
    assert "better leads" in profile.text
    assert "Accept / Quote" in page.text
    assert "Gate 4455" in page.text
    assert page.text.count("+15557654321") == 1

    calls = {}

    async def fake_complete(phone: str, job_id: str):
        calls["phone"] = phone
        calls["job_id"] = job_id
        return "updated"

    monkeypatch.setattr(routes, "mark_completed", fake_complete)
    done = client.post(f"/pro/dashboard/jobs/{JOB}/complete", follow_redirects=False)
    assert done.status_code == 303
    assert calls == {"phone": PHONE, "job_id": JOB}

    stale = client.get("/pro/auth?token=not-a-real-token", follow_redirects=False)
    assert stale.status_code == 401
