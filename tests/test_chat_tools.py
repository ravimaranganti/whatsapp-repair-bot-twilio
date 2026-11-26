"""A trade with no signed-up pro should offer Google listings, not a job send."""

import asyncio

import app.services.chat_tools as tools


def test_no_hvac_pros_returns_places_to_call(monkeypatch):
    async def no_pros(**kwargs):
        return []

    async def places(trade, zip_code):
        assert trade == "hvac"
        assert zip_code == "77479"
        return [{"name": "Cool Air", "phone": "281-555-0100", "rating": 4.8}]

    monkeypatch.setattr(tools, "select_pros_for_job", no_pros)
    monkeypatch.setattr(tools, "search_local_specialists", places)
    result = asyncio.run(
        tools.execute_tool(
            "search_pros",
            {"trade": "hvac", "area": "77479"},
            homeowner_phone="15551230000",
        )
    )
    assert result["count"] == 0
    assert result["places"][0]["phone"] == "281-555-0100"
    assert "Do not offer a job" in result["note"]


def test_request_quotes_stops_when_no_pro_is_signed_up(monkeypatch):
    async def no_pros(**kwargs):
        return []

    async def places(trade, zip_code):
        return [{"name": "Cool Air", "phone": "281-555-0100"}]

    async def ensure(phone):
        return {"zip_code": "77479"}

    monkeypatch.setattr(tools.settings, "dispatch_enabled", True)
    monkeypatch.setattr(tools, "select_pros_for_job", no_pros)
    monkeypatch.setattr(tools, "search_local_specialists", places)
    monkeypatch.setattr(tools.homeowner_store, "ensure", ensure)
    result = asyncio.run(
        tools.execute_tool(
            "request_quotes",
            {
                "job_summary": "The heater is not working at all in the house",
                "trade": "hvac",
            },
            homeowner_phone="15551230000",
        )
    )
    assert result["ok"] is False
    assert result["places"][0]["name"] == "Cool Air"
    assert "will be sent" in result["error"]
