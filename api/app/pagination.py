from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Literal, Sequence

from fastapi import HTTPException, status
from sqlalchemy import and_, or_

Direction = Literal["asc", "desc"]


@dataclass(frozen=True)
class KeysetColumn:
    key: str
    column: Any
    direction: Direction = "asc"
    parser: Callable[[Any], Any] | None = None
    serializer: Callable[[Any], Any] | None = None
    getter: Callable[[Any], Any] | None = None


def parse_datetime_cursor_value(raw: Any) -> datetime:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("expected ISO datetime string")
    return datetime.fromisoformat(raw)


def parse_int_cursor_value(raw: Any) -> int:
    return int(raw)


def parse_uuid_cursor_value(raw: Any) -> uuid.UUID:
    return uuid.UUID(str(raw))


def apply_keyset_pagination(stmt, specs: Sequence[KeysetColumn], cursor: str | None, limit: int):
    cursor_values = parse_keyset_cursor(cursor, specs)
    if cursor_values is not None:
        stmt = stmt.where(build_keyset_filter(specs, cursor_values))

    stmt = stmt.order_by(*_order_by_clauses(specs)).limit(limit + 1)
    return stmt


def paginate_rows(rows: list[Any], specs: Sequence[KeysetColumn], limit: int) -> tuple[list[Any], str | None]:
    if len(rows) <= limit:
        return rows, None

    page_rows = rows[:limit]
    return page_rows, encode_keyset_cursor(page_rows[-1], specs)


def parse_keyset_cursor(cursor: str | None, specs: Sequence[KeysetColumn]) -> dict[str, Any] | None:
    if not cursor:
        return None

    try:
        payload = _decode_cursor_payload(cursor)
    except (ValueError, json.JSONDecodeError) as exc:
        raise _invalid_cursor() from exc

    if not isinstance(payload, dict):
        raise _invalid_cursor()

    parsed: dict[str, Any] = {}
    expected_keys = {spec.key for spec in specs}
    if set(payload.keys()) != expected_keys:
        raise _invalid_cursor()

    for spec in specs:
        if spec.key not in payload:
            raise _invalid_cursor()
        raw_value = payload[spec.key]
        try:
            parsed[spec.key] = spec.parser(raw_value) if spec.parser else raw_value
        except (TypeError, ValueError, AttributeError) as exc:
            raise _invalid_cursor() from exc
    return parsed


def encode_keyset_cursor(row: Any, specs: Sequence[KeysetColumn]) -> str:
    payload = {
        spec.key: _serialize_cursor_value(_row_cursor_value(row, spec), spec.serializer)
        for spec in specs
    }
    raw_json = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw_json).decode("ascii").rstrip("=")


def build_keyset_filter(specs: Sequence[KeysetColumn], values: dict[str, Any]):
    branches = []
    equality_prefix = []
    for spec in specs:
        if spec.key not in values:
            raise _invalid_cursor()
        comparator = spec.column > values[spec.key] if spec.direction == "asc" else spec.column < values[spec.key]
        if equality_prefix:
            branches.append(and_(*equality_prefix, comparator))
        else:
            branches.append(comparator)
        equality_prefix.append(spec.column == values[spec.key])
    return or_(*branches)


def _order_by_clauses(specs: Sequence[KeysetColumn]) -> list[Any]:
    clauses = []
    for spec in specs:
        clauses.append(spec.column.asc() if spec.direction == "asc" else spec.column.desc())
    return clauses


def _decode_cursor_payload(cursor: str) -> Any:
    padded = cursor + "=" * (-len(cursor) % 4)
    raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    return json.loads(raw.decode("utf-8"))


def _serialize_cursor_value(value: Any, serializer: Callable[[Any], Any] | None) -> Any:
    if serializer is not None:
        return serializer(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def _row_cursor_value(row: Any, spec: KeysetColumn) -> Any:
    if spec.getter is not None:
        return spec.getter(row)
    return getattr(row, spec.key)


def _invalid_cursor() -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid cursor")
