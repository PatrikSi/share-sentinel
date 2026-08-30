import json
import uuid
from datetime import UTC, datetime

import pytest
from app.models import ApiToken, Project, User
from app.services.audit import MAX_AUDIT_METADATA_BYTES, sanitize_audit_metadata, write_audit_event


def test_sanitize_audit_metadata_redacts_nested_secret_fields() -> None:
    payload = sanitize_audit_metadata(
        {
            "request_id": "request-1",
            "password": "never-store-me",
            "seed_admin_password": "suffix-secret",
            "database_url": "postgresql://user:password@example.invalid/db",
            "nested": {
                "client-secret": "also-secret",
                "graphClientSecret": "camel-secret",
                "authToken": "opaque-secret",
                "signing_client_assertion": "signed-secret",
                "api_token_hash": "hashed-but-sensitive",
                "secretKey": "wrapped-secret",
                "passwordValue": "wrapped-password",
                "clientSecretValue": "wrapped-client-secret",
                "apiKeyValue": "wrapped-api-key",
                "privateKeyData": "wrapped-private-key",
                "bearerTokenValue": "wrapped-token",
                "token_id": "safe-reference",
                "when": datetime(2026, 8, 30, tzinfo=UTC),
            },
        }
    )

    assert payload["password"] == "[redacted]"
    assert payload["seed_admin_password"] == "[redacted]"
    assert payload["database_url"] == "[redacted]"
    assert payload["nested"]["client-secret"] == "[redacted]"
    assert payload["nested"]["graphClientSecret"] == "[redacted]"
    assert payload["nested"]["authToken"] == "[redacted]"
    assert payload["nested"]["signing_client_assertion"] == "[redacted]"
    assert payload["nested"]["api_token_hash"] == "[redacted]"
    for key in (
        "secretKey",
        "passwordValue",
        "clientSecretValue",
        "apiKeyValue",
        "privateKeyData",
        "bearerTokenValue",
    ):
        assert payload["nested"][key] == "[redacted]"
    assert payload["nested"]["token_id"] == "safe-reference"
    assert payload["nested"]["when"] == "2026-08-30T00:00:00+00:00"


def test_sanitize_audit_metadata_bounds_oversized_payload() -> None:
    payload = sanitize_audit_metadata(
        {"request_id": "request-2", **{f"field_{index}": "x" * 4096 for index in range(100)}}
    )

    assert payload["request_id"] == "request-2"
    assert payload["_metadata_truncated"] is True
    assert payload["_encoded_bytes_before_truncation"] > MAX_AUDIT_METADATA_BYTES
    assert len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) <= MAX_AUDIT_METADATA_BYTES


def test_sanitize_audit_metadata_bounds_hostile_long_keys_and_preserved_fields() -> None:
    metadata = {
        "request_id": "\x00" * 4096,
        **{f"{'k' * 4088}{index:08d}": "value" for index in range(100)},
    }

    payload = sanitize_audit_metadata(metadata)

    assert payload["_metadata_truncated"] is True
    assert "_top_level_keys" not in payload
    assert len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) <= MAX_AUDIT_METADATA_BYTES


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


def test_write_audit_event_snapshots_all_actor_and_project_attribution() -> None:
    actor_user_id = uuid.uuid4()
    actor_token_id = uuid.uuid4()
    project_id = uuid.uuid4()
    added = []

    class _Db:
        entities = {
            (User, actor_user_id): type("UserRow", (), {"email": "actor@example.com"})(),
            (ApiToken, actor_token_id): type("TokenRow", (), {"name": "nightly-collector"})(),
            (Project, project_id): type("ProjectRow", (), {"name": "Finance"})(),
        }

        def get(self, model, object_id):
            return self.entities.get((model, object_id))

        def add(self, value):
            added.append(value)

    write_audit_event(
        _Db(),
        action="RUN_CREATED",
        object_type="scan_run",
        object_id=str(uuid.uuid4()),
        actor_user_id=actor_user_id,
        actor_token_id=actor_token_id,
        project_id=project_id,
    )

    event = added[0]
    assert event.actor_user_ref == actor_user_id
    assert event.actor_email_snapshot == "actor@example.com"
    assert event.actor_token_ref == actor_token_id
    assert event.actor_token_name_snapshot == "nightly-collector"
    assert event.project_ref == project_id
    assert event.project_name_snapshot == "Finance"


def test_write_audit_event_rejects_conflicting_live_and_snapshot_references() -> None:
    class _Db:
        def add(self, _value):
            raise AssertionError("invalid event must not be added")

    with pytest.raises(ValueError, match="project_ref must match project_id"):
        write_audit_event(
            _Db(),
            action="RUN_CREATED",
            object_type="scan_run",
            object_id="1",
            project_id=uuid.uuid4(),
            project_ref=uuid.uuid4(),
        )


def test_write_audit_event_rejects_unbounded_programmer_labels() -> None:
    class _Db:
        def add(self, _value):
            raise AssertionError("invalid event must not be added")

    with pytest.raises(ValueError, match="action must be a bounded audit identifier"):
        write_audit_event(_Db(), action="invalid action", object_type="finding", object_id="1")
