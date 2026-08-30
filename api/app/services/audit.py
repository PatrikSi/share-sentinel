import json
import math
import re
import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditEvent

MAX_AUDIT_METADATA_BYTES = 64 * 1024
MAX_AUDIT_STRING_CHARS = 4096
MAX_AUDIT_COLLECTION_ITEMS = 100
MAX_AUDIT_DEPTH = 6
AUDIT_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]*$")
SENSITIVE_METADATA_KEYS = frozenset(
    {
        "access_key",
        "access_token",
        "api_key",
        "authorization",
        "client_assertion",
        "client_secret",
        "connection_string",
        "cookie",
        "credential",
        "credential_hash",
        "credentials",
        "database_url",
        "hashes",
        "jwt",
        "lm_hash",
        "nt_hash",
        "passphrase",
        "password",
        "password_hash",
        "pem",
        "private_key",
        "refresh_token",
        "secret",
        "set_cookie",
        "token_hash",
    }
)
SENSITIVE_METADATA_SUFFIXES = (
    "_access_key",
    "_access_token",
    "_api_key",
    "_authorization",
    "_client_assertion",
    "_connection_string",
    "_cookie",
    "_credential",
    "_credential_hash",
    "_database_url",
    "_hashes",
    "_jwt",
    "_passphrase",
    "_password",
    "_password_hash",
    "_pem",
    "_private_key",
    "_refresh_token",
    "_secret",
    "_token_hash",
)
SENSITIVE_METADATA_FINGERPRINT_SUFFIXES = (
    "accesskey",
    "accesstoken",
    "apikey",
    "assertion",
    "authorization",
    "bearertoken",
    "clientsecret",
    "connectionstring",
    "cookie",
    "credential",
    "databaseurl",
    "hashes",
    "jwt",
    "lmhash",
    "nthash",
    "passphrase",
    "password",
    "pem",
    "privatekey",
    "refreshtoken",
    "secret",
    "token",
    "tokenhash",
)


def _normalized_key(value: object) -> str:
    return str(value).strip().casefold().replace("-", "_")


def _is_sensitive_key(value: object) -> bool:
    normalized = _normalized_key(value)
    fingerprint = "".join(character for character in normalized if character.isalnum())
    return (
        normalized in SENSITIVE_METADATA_KEYS
        or normalized.endswith(SENSITIVE_METADATA_SUFFIXES)
        or fingerprint.endswith(SENSITIVE_METADATA_FINGERPRINT_SUFFIXES)
    )


def _bounded_string(value: object) -> str:
    normalized = str(value)
    if len(normalized) <= MAX_AUDIT_STRING_CHARS:
        return normalized
    omitted = len(normalized) - MAX_AUDIT_STRING_CHARS
    return f"{normalized[:MAX_AUDIT_STRING_CHARS]}…[truncated {omitted} chars]"


def _sanitize_value(value: object, *, depth: int = 0) -> Any:
    if depth >= MAX_AUDIT_DEPTH:
        return "[truncated: maximum audit metadata depth exceeded]"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return _bounded_string(value)
    if isinstance(value, (uuid.UUID, date, datetime)):
        return value.isoformat() if hasattr(value, "isoformat") else str(value)
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        pairs = list(value.items())
        for raw_key, item in pairs[:MAX_AUDIT_COLLECTION_ITEMS]:
            key = _bounded_string(raw_key)
            if _is_sensitive_key(key):
                sanitized[key] = "[redacted]"
            else:
                sanitized[key] = _sanitize_value(item, depth=depth + 1)
        if len(pairs) > MAX_AUDIT_COLLECTION_ITEMS:
            sanitized["_truncated_field_count"] = len(pairs) - MAX_AUDIT_COLLECTION_ITEMS
        return sanitized
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        sanitized_items = [
            _sanitize_value(item, depth=depth + 1) for item in items[:MAX_AUDIT_COLLECTION_ITEMS]
        ]
        if len(items) > MAX_AUDIT_COLLECTION_ITEMS:
            sanitized_items.append(
                {"_truncated_item_count": len(items) - MAX_AUDIT_COLLECTION_ITEMS}
            )
        return sanitized_items
    return _bounded_string(value)


def sanitize_audit_metadata(metadata: dict | None) -> dict[str, Any]:
    sanitized = _sanitize_value(metadata or {})
    if not isinstance(sanitized, dict):
        sanitized = {"value": sanitized}
    encoded = json.dumps(sanitized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) <= MAX_AUDIT_METADATA_BYTES:
        return sanitized

    preserved = {
        key: value
        for key, value in sanitized.items()
        if key in {"request_id", "ip", "user_agent", "reason", "status"}
    }
    return {
        **preserved,
        "_metadata_truncated": True,
        "_encoded_bytes_before_truncation": len(encoded),
        "_top_level_keys": list(sanitized)[:MAX_AUDIT_COLLECTION_ITEMS],
    }


def _validate_audit_label(value: str, *, name: str, max_length: int) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > max_length or not AUDIT_NAME_PATTERN.fullmatch(normalized):
        raise ValueError(f"{name} must be a bounded audit identifier")
    return normalized


def _validate_object_id(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > 255:
        raise ValueError("object_id must be between 1 and 255 characters")
    return normalized


def write_audit_event(
    db: Session,
    action: str,
    object_type: str,
    object_id: str,
    actor_user_id: uuid.UUID | None = None,
    actor_token_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    metadata: dict | None = None,
) -> None:
    event = AuditEvent(
        action=_validate_audit_label(action, name="action", max_length=120),
        object_type=_validate_audit_label(object_type, name="object_type", max_length=80),
        object_id=_validate_object_id(object_id),
        actor_user_id=actor_user_id,
        actor_token_id=actor_token_id,
        project_id=project_id,
        metadata_json=sanitize_audit_metadata(metadata),
    )
    db.add(event)
