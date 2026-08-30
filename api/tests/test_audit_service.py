import uuid
from datetime import UTC, datetime

import pytest
from app.services.audit import MAX_AUDIT_METADATA_BYTES, sanitize_audit_metadata, write_audit_event


def test_sanitize_audit_metadata_redacts_nested_secret_fields() -> None:
    payload = sanitize_audit_metadata(
        {
            "request_id": "request-1",
            "password": "never-store-me",
            "seed_admin_password": "suffix-secret",
            "nested": {
                "client-secret": "also-secret",
                "token_id": "safe-reference",
                "when": datetime(2026, 8, 30, tzinfo=UTC),
            },
        }
    )

    assert payload["password"] == "[redacted]"
    assert payload["seed_admin_password"] == "[redacted]"
    assert payload["nested"]["client-secret"] == "[redacted]"
    assert payload["nested"]["token_id"] == "safe-reference"
    assert payload["nested"]["when"] == "2026-08-30T00:00:00+00:00"


def test_sanitize_audit_metadata_bounds_oversized_payload() -> None:
    payload = sanitize_audit_metadata(
        {"request_id": "request-2", **{f"field_{index}": "x" * 4096 for index in range(100)}}
    )

    assert payload["request_id"] == "request-2"
    assert payload["_metadata_truncated"] is True
    assert payload["_encoded_bytes_before_truncation"] > MAX_AUDIT_METADATA_BYTES


def test_write_audit_event_validates_labels_and_sanitizes_metadata() -> None:
    added = []

    class _Db:
        def add(self, value):
            added.append(value)

    write_audit_event(
        _Db(),
        action="FINDING_UPDATED",
        object_type="finding",
        object_id=str(uuid.uuid4()),
        metadata={"authorization": "Bearer secret", "result": "ok"},
    )

    assert len(added) == 1
    assert added[0].metadata_json == {"authorization": "[redacted]", "result": "ok"}


def test_write_audit_event_rejects_unbounded_programmer_labels() -> None:
    class _Db:
        def add(self, _value):
            raise AssertionError("invalid event must not be added")

    with pytest.raises(ValueError, match="action must be a bounded audit identifier"):
        write_audit_event(_Db(), action="invalid action", object_type="finding", object_id="1")
