"""Trade selection during pro onboarding: taps, typed names, and Other."""

from app.services.onboarding import interpret_trade_turn, is_onboarding_tap


def test_tap_standard_trade():
    turn = interpret_trade_turn("Plumbing", "trade:plumbing", "choose", [])
    assert turn.action == "add"
    assert turn.trades == ["plumbing"]


def test_old_other_tap_is_recognized_after_setup():
    assert is_onboarding_tap("trade:other", "Other", interactive=True)
    assert is_onboarding_tap("", "Other", interactive=True)
    assert not is_onboarding_tap("", "other than the sink", interactive=False)


def test_tap_other_asks_for_text():
    turn = interpret_trade_turn("Other", "trade:other", "choose", [])
    assert turn.action == "ask_other"


def test_typed_custom_trade_after_other():
    turn = interpret_trade_turn("garage doors", "", "other", [])
    assert turn.action == "add"
    assert turn.trades == ["garage doors"]


def test_typed_known_and_custom_together():
    turn = interpret_trade_turn("plumbing and roofing", "", "choose", [])
    assert turn.action == "add"
    assert turn.trades == ["plumbing", "roofing"]


def test_add_another_and_thats_all():
    more = interpret_trade_turn("Add another", "trade_more:yes", "more", ["plumbing"])
    assert more.action == "show_list"
    done = interpret_trade_turn("That's all", "trade_more:no", "more", ["plumbing"])
    assert done.action == "done"
    typed = interpret_trade_turn("that's all", "", "more", ["plumbing"])
    assert typed.action == "done"


def test_duplicate_tap_adds_nothing_new():
    turn = interpret_trade_turn("Plumbing", "trade:plumbing", "choose", ["plumbing"])
    assert turn.action == "add"
    assert turn.trades == []


def test_expanded_trades_from_taps_and_text():
    paint = interpret_trade_turn("Painting", "trade:painting", "choose", [])
    assert paint.trades == ["painting"]
    both = interpret_trade_turn("pool and lawn care", "", "choose", [])
    assert both.trades == ["pool", "lawn"]
    assert interpret_trade_turn("pool service", "", "choose", []).trades == ["pool"]
    gc = interpret_trade_turn("general contractor", "", "choose", [])
    assert gc.trades == ["general_contractor"]


def test_mobile_auto_repair_from_tap_or_text():
    tapped = interpret_trade_turn("Mobile auto repair", "trade:mobile_auto", "choose", [])
    assert tapped.action == "add"
    assert tapped.trades == ["mobile_auto"]
    typed = interpret_trade_turn("I'm a mobile mechanic", "", "choose", [])
    assert typed.trades == ["mobile_auto"]


def test_language_and_insurance_taps_stay_on_setup():
    assert is_onboarding_tap("lang_both", "Both", interactive=True)
    assert is_onboarding_tap("insured_yes", "Yes, insured", interactive=True)
    assert not is_onboarding_tap("", "both of the sinks", interactive=False)


def test_blank_or_ok_is_not_a_trade():
    assert interpret_trade_turn("", "", "choose", []).action == "invalid"
    assert interpret_trade_turn("ok", "", "choose", []).action == "invalid"
    assert interpret_trade_turn("ok", "", "other", []).action == "invalid"
