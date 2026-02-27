
def parse_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        offset = int(cursor)
    except ValueError:
        return 0
    return max(0, offset)


def next_cursor(offset: int, limit: int, current_count: int) -> str | None:
    if current_count < limit:
        return None
    return str(offset + limit)
