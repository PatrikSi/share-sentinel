import json
import math
import re
import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models import ApiToken, AuditEvent, Project, User

MAX_AUDIT_METADATA_BYTES = 64 * 1024
MAX_AUDIT_STRING_CHARS = 4096
MAX_AUDIT_COLLECTION_ITEMS = 100
MAX_AUDIT_DEPTH = 6
MAX_AUDIT_FALLBACK_STRING_CHARS = 512
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
SENSITIVE_METADATA_WRAPPER_SUFFIXES = (
    "blob",
    "bytes",
    "content",
    "contents",
    "data",
    "key",
    "material",
    "text",
    "value",
)
SENSITIVE_METADATA_WRAPPED_FINGERPRINT_SUFFIXES = tuple(
    f"{stem}{wrapper}"
    for stem in SENSITIVE_METADATA_FINGERPRINT_SUFFIXES
    for wrapper in SENSITIVE_METADATA_WRAPPER_SUFFIXES
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
        or fingerprint.endswith(SENSITIVE_METADATA_WRAPPED_FINGERPRINT_SUFFIXES)
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

    preserved: dict[str, Any] = {}
    for key in ("request_id", "ip", "user_agent", "reason", "status"):
        if key not in sanitized:
            continue
        value = sanitized.get(key)
        if value is None or isinstance(value, (bool, int, float)):
            preserved[key] = value
        elif isinstance(value, str):
            preserved[key] = value[:MAX_AUDIT_FALLBACK_STRING_CHARS]
        else:
            preserved[key] = "[omitted: oversized audit metadata]"
    fallback = {
        **preserved,
        "_metadata_truncated": True,
        "_encoded_bytes_before_truncation": len(encoded),
        "_top_level_key_count": len(sanitized),
    }
    fallback_encoded = json.dumps(fallback, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(fallback_encoded) <= MAX_AUDIT_METADATA_BYTES:
        return fallback
    return {
        "_metadata_truncated": True,
        "_encoded_bytes_before_truncation": len(encoded),
        "_top_level_key_count": len(sanitized),
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


def _snapshot_label(
    db: Session,
    model: type[ApiToken] | type[Project] | type[User],
    object_id: uuid.UUID | None,
    attribute: str,
    supplied: str | None,
    *,
    max_length: int,
) -> str | None:
    value = supplied
    if value is None and object_id is not None:
        getter = getattr(db, "get", None)
        if callable(getter):
            entity = getter(model, object_id)
            value = getattr(entity, attribute, None) if entity is not None else None
    if value is None:
        return None
    normalized = str(value)
    if not normalized or len(normalized) > max_length:
        raise ValueError(f"{attribute} snapshot must be between 1 and {max_length} characters")
    return normalized


def write_audit_event(
    db: Session,
    action: str,
    object_type: str,
    object_id: str,
    actor_user_id: uuid.UUID | None = None,
    actor_token_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    actor_user_ref: uuid.UUID | None = None,
    actor_email_snapshot: str | None = None,
    actor_token_ref: uuid.UUID | None = None,
    actor_token_name_snapshot: str | None = None,
    project_ref: uuid.UUID | None = None,
    project_name_snapshot: str | None = None,
    metadata: dict | None = None,
) -> None:
    if actor_user_id is not None and actor_user_ref not in (None, actor_user_id):
        raise ValueError("actor_user_ref must match actor_user_id when both are provided")
    if actor_token_id is not None and actor_token_ref not in (None, actor_token_id):
        raise ValueError("actor_token_ref must match actor_token_id when both are provided")
    if project_id is not None and project_ref not in (None, project_id):
        raise ValueError("project_ref must match project_id when both are provided")

    effective_actor_user_ref = actor_user_id or actor_user_ref
    effective_actor_token_ref = actor_token_id or actor_token_ref
    effective_project_ref = project_id or project_ref
    event = AuditEvent(
        action=_validate_audit_label(action, name="action", max_length=120),
        object_type=_validate_audit_label(object_type, name="object_type", max_length=80),
        object_id=_validate_object_id(object_id),
        actor_user_id=actor_user_id,
        actor_user_ref=effective_actor_user_ref,
        actor_email_snapshot=_snapshot_label(
            db,
            User,
            effective_actor_user_ref,
            "email",
            actor_email_snapshot,
            max_length=320,
        ),
        actor_token_id=actor_token_id,
        actor_token_ref=effective_actor_token_ref,
        actor_token_name_snapshot=_snapshot_label(
            db,
            ApiToken,
            effective_actor_token_ref,
            "name",
            actor_token_name_snapshot,
            max_length=120,
        ),
        project_id=project_id,
        project_ref=effective_project_ref,
        project_name_snapshot=_snapshot_label(
            db,
            Project,
            effective_project_ref,
            "name",
            project_name_snapshot,
            max_length=255,
        ),
        metadata_json=sanitize_audit_metadata(metadata),
    )
    db.add(event)
