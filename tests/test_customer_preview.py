"""Paused intake diagnoses a problem and lists Google companies, without posting a job."""

import asyncio

import app.services.chat_tools as tools
from app.config import settings
from app.services.customer_preview import preview_places_message


def test_places_list_names_three_companies_and_posts_nothing():
    text = preview_places_message(
        "hvac",
        "77479",
        [
            {"name": "Cool Air", "phone": "281-555-0100", "rating": 4.8, "reviews": 120},
            {"name": "Bayou HVAC", "phone": "281-555-0101", "rating": 4.7, "reviews": 80},
        ],
    )
    assert "Cool Air" in text
    assert "281-555-0100" in text
    assert "Nothing was sent to our pros." in text
    assert "77479" in text


def test_request_quotes_does_not_post_while_onboarding(monkeypatch):
    monkeypatch.setattr(settings, "customer_requests_enabled", False)

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("quotes were requested")

    monkeypatch.setattr(tools, "select_pros_for_job", fail_if_called)
    result = asyncio.run(
        tools.execute_tool(
            "request_quotes",
            {"job_summary": "The kitchen sink has been leaking under the cabinet all day"},
            homeowner_phone="15551230000",
        )
    )
    assert result["ok"] is False
    assert "paused" in result["error"]
