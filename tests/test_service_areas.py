"""Houston area selection for pro onboarding."""

from app.services.service_areas import (
    area_for_zip,
    interpret_area_turn,
    pro_covers_zip,
    zips_for_areas,
)


def test_tap_and_typed_areas():
    katy = interpret_area_turn("Katy", "area:katy", "choose", [])
    assert katy.areas == ["katy"]
    both = interpret_area_turn("Katy and The Woodlands", "", "choose", [])
    assert both.areas == ["katy", "the_woodlands"]
    friends = interpret_area_turn("friendswood", "", "choose", [])
    assert friends.areas == ["league_city"]


def test_all_areas_replaces_a_partial_list():
    turn = interpret_area_turn("all areas", "", "choose", ["katy"])
    assert turn.areas == ["all"]
    assert zips_for_areas(["all"]) == []
    assert "77494" in zips_for_areas(["katy"])


def test_thats_all_finishes_instead_of_selecting_everywhere():
    turn = interpret_area_turn("that's all", "", "more", ["houston"])
    assert turn.action == "done"


def test_zip_maps_to_the_named_suburb_before_houston():
    assert area_for_zip("77008") == "houston"
    assert area_for_zip("77084") == "katy"
    assert area_for_zip("77494") == "katy"
    assert area_for_zip("77380") == "the_woodlands"
    assert area_for_zip("77573") == "league_city"
    assert area_for_zip("77459") == "missouri_city"
    assert area_for_zip("10001") is None


def test_coverage_uses_areas_and_keeps_legacy_zips():
    houston = {"service_areas": ["houston"], "service_zips": []}
    assert pro_covers_zip(houston, "77008")
    assert not pro_covers_zip(houston, "77494")
    everyone = {"service_areas": ["all"], "service_zips": []}
    assert pro_covers_zip(everyone, "77494")
    legacy = {"service_areas": [], "service_zips": ["77006"]}
    assert pro_covers_zip(legacy, "77006")
    assert not pro_covers_zip(legacy, "77008")
    seeded = {"service_areas": [], "service_zips": []}
    assert pro_covers_zip(seeded, "77008")
