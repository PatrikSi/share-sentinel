import json
from datetime import date, datetime
from enum import Enum


def spreadsheet_safe_csv_value(value: object) -> str:
    """Serialize one CSV cell and neutralize spreadsheet formula execution."""

    if isinstance(value, Enum):
        value = value.value
    if value is None:
        text = ""
    elif isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, (datetime, date)):
        text = value.isoformat()
    elif isinstance(value, (dict, list, tuple)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    else:
        text = str(value)

    formula_candidate = text.lstrip(" \t\r\n")
    leading_characters = text[: len(text) - len(formula_candidate)]
    if any(character in "\t\r\n" for character in leading_characters) or formula_candidate.startswith(
        ("=", "+", "-", "@")
    ):
        return f"'{text}"
    return text
