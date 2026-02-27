from app.pagination import next_cursor, parse_cursor


def test_parse_cursor_defaults_to_zero() -> None:
    assert parse_cursor(None) == 0
    assert parse_cursor("invalid") == 0
    assert parse_cursor("-100") == 0


def test_next_cursor_only_when_page_full() -> None:
    assert next_cursor(0, 50, 49) is None
    assert next_cursor(0, 50, 50) == "50"
