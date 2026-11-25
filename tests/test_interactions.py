"""Unit tests for Twilio interaction parsing."""

from app.services.interactions import parse_interaction


def test_text():
    i = parse_interaction({"Body": "hello", "NumMedia": "0"})
    assert i.kind == "text"
    assert i.text == "hello"


def test_quick_reply_button():
    i = parse_interaction(
        {
            "Body": "Interested",
            "ButtonText": "Interested",
            "ButtonPayload": "int:jobdeadbeef",
            "NumMedia": "0",
        }
    )
    assert i.kind == "quick_reply"
    assert i.payload == "int:jobdeadbeef"
    assert i.text == "Interested"


def test_list_picker_id():
    i = parse_interaction(
        {
            "Body": "Plumbing",
            "ListId": "trade:plumbing",
            "NumMedia": 0,
        }
    )
    assert i.kind == "list_picker"
    assert i.payload == "trade:plumbing"


def test_media_image():
    i = parse_interaction(
        {
            "Body": "leaking",
            "NumMedia": 1,
            "MediaUrl0": "https://api.twilio.com/media/abc",
            "MediaContentType0": "image/jpeg",
        }
    )
    assert i.kind == "media"
    assert i.media_url.endswith("/abc")
    assert i.text == "leaking"


def test_flow_submission_is_not_a_button_payload():
    i = parse_interaction(
        {
            "Body": "Sent",
            "InteractiveData": '{"selected_trades":["plumbing"],"selected_zones":["katy"]}',
            "NumMedia": "0",
        }
    )
    assert i.kind == "flow"
    assert i.payload == ""
    assert i.text == "Sent"


def test_empty_unknown():
    i = parse_interaction({"NumMedia": 0, "Body": ""})
    assert i.kind == "unknown"
