import pytest

from app.sources.pumpportal import (
    PumpPortalMessageError,
    parse_message,
)


def test_parse_valid_message():
    result = parse_message('{"name": "Example", "symbol": "EX"}')
    assert result["name"] == "Example"
    assert result["symbol"] == "EX"


def test_reject_invalid_json():
    with pytest.raises(PumpPortalMessageError):
        parse_message("not json")


def test_reject_json_array():
    with pytest.raises(PumpPortalMessageError):
        parse_message('[{"name": "Example"}]')


def test_reject_json_string():
    with pytest.raises(PumpPortalMessageError):
        parse_message('"hello"')


def test_reject_empty_message():
    with pytest.raises(PumpPortalMessageError):
        parse_message("")


def test_reject_whitespace_message():
    with pytest.raises(PumpPortalMessageError):
        parse_message("   ")


def test_reject_oversized_message():
    from app.sources.pumpportal import MAX_MESSAGE_SIZE

    raw = " " * (MAX_MESSAGE_SIZE + 1)

    with pytest.raises(PumpPortalMessageError):
        parse_message(raw)


def test_reject_non_string_message():
    with pytest.raises(PumpPortalMessageError):
        parse_message(None)
