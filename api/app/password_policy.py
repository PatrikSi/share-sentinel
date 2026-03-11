import re
from typing import Any

_SPECIAL_CHAR_PATTERN = re.compile(r"[^A-Za-z0-9]")


def password_policy_kwargs(settings: Any) -> dict[str, int | bool]:
    return {
        "min_length": int(getattr(settings, "password_min_length", 12)),
        "require_lowercase": bool(getattr(settings, "password_require_lowercase", True)),
        "require_uppercase": bool(getattr(settings, "password_require_uppercase", True)),
        "require_number": bool(getattr(settings, "password_require_number", True)),
        "require_special": bool(getattr(settings, "password_require_special", False)),
    }


def validate_password_strength(
    password: str,
    min_length: int,
    require_lowercase: bool = True,
    require_uppercase: bool = True,
    require_number: bool = True,
    require_special: bool = False,
) -> None:
    if len(password) < min_length:
        raise ValueError(f"password must be at least {min_length} characters")
    if require_lowercase and not re.search(r"[a-z]", password):
        raise ValueError("password must include a lowercase letter")
    if require_uppercase and not re.search(r"[A-Z]", password):
        raise ValueError("password must include an uppercase letter")
    if require_number and not re.search(r"\d", password):
        raise ValueError("password must include a number")
    if require_special and not _SPECIAL_CHAR_PATTERN.search(password):
        raise ValueError("password must include a special character")
