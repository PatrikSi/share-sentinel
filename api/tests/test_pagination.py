import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import column

from app.pagination import (
    KeysetColumn,
    build_keyset_filter,
    encode_keyset_cursor,
    paginate_rows,
    parse_datetime_cursor_value,
    parse_int_cursor_value,
    parse_keyset_cursor,
    parse_uuid_cursor_value,
)


def test_keyset_cursor_round_trip_for_datetime_and_uuid() -> None:
    specs = (
        KeysetColumn("created_at", "created_at", direction="desc", parser=parse_datetime_cursor_value),
        KeysetColumn("id", "id", direction="desc", parser=parse_uuid_cursor_value),
    )
    row = SimpleNamespace(created_at=datetime(2026, 3, 11, 5, 45, tzinfo=UTC), id=uuid.uuid4())

    cursor = encode_keyset_cursor(row, specs)
    parsed = parse_keyset_cursor(cursor, specs)

    assert parsed == {"created_at": row.created_at, "id": row.id}


def test_paginate_rows_returns_cursor_only_when_more_data_exists() -> None:
    specs = (KeysetColumn("id", "id", parser=parse_int_cursor_value),)
    rows = [SimpleNamespace(id=3), SimpleNamespace(id=2), SimpleNamespace(id=1)]

    page_rows, next_cursor = paginate_rows(rows, specs, limit=2)

    assert [row.id for row in page_rows] == [3, 2]
    assert parse_keyset_cursor(next_cursor, specs) == {"id": 2}

    final_rows, final_cursor = paginate_rows(page_rows, specs, limit=2)
    assert [row.id for row in final_rows] == [3, 2]
    assert final_cursor is None


def test_parse_keyset_cursor_rejects_invalid_tokens() -> None:
    specs = (KeysetColumn("id", "id", parser=parse_int_cursor_value),)

    with pytest.raises(Exception) as exc_info:
        parse_keyset_cursor("not-base64", specs)

    assert getattr(exc_info.value, "status_code", None) == 400


def test_build_keyset_filter_creates_lexicographic_predicate() -> None:
    specs = (
        KeysetColumn("created_at", column("created_at"), direction="desc", parser=parse_datetime_cursor_value),
        KeysetColumn("id", column("id"), direction="desc", parser=parse_int_cursor_value),
    )

    predicate = build_keyset_filter(
        specs,
        {
            "created_at": datetime(2026, 3, 11, 5, 45, tzinfo=UTC),
            "id": 42,
        },
    )

    rendered = str(predicate)
    assert "created_at <" in rendered or "created_at<" in rendered
    assert "id <" in rendered or "id<" in rendered
