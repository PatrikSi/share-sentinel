import importlib.util
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def migration_module():
    migration_path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "0009_api_scale_indexes.py"
    )
    spec = importlib.util.spec_from_file_location("api_scale_migration", migration_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("validity", "expected"),
    [
        (True, []),
        (None, ["CREATE INDEX CONCURRENTLY target"]),
        (
            False,
            [
                "DROP INDEX CONCURRENTLY IF EXISTS target_index",
                "CREATE INDEX CONCURRENTLY target",
            ],
        ),
    ],
)
def test_ensure_valid_index_recovers_interrupted_concurrent_build(
    monkeypatch,
    migration_module,
    validity: bool | None,
    expected: list[str],
) -> None:
    executed: list[str] = []
    monkeypatch.setattr(migration_module, "_index_validity", lambda _name: validity)
    monkeypatch.setattr(migration_module.op, "execute", executed.append)

    migration_module._ensure_valid_index(
        "target_index",
        "CREATE INDEX CONCURRENTLY target",
    )

    assert executed == expected


def test_global_keyset_indexes_match_query_order_and_model_metadata(migration_module) -> None:
    from app.models import ApiToken, AuditEvent, User

    statements = dict(migration_module.INDEXES)
    expected = {
        "ix_audit_events_ts_id": (
            statements["ix_audit_events_ts_id"],
            "ON audit_events (ts DESC, id DESC)",
            AuditEvent.__table__,
        ),
        "ix_users_created_id": (
            statements["ix_users_created_id"],
            "ON users (created_at DESC, id DESC)",
            User.__table__,
        ),
        "ix_api_tokens_created_id": (
            statements["ix_api_tokens_created_id"],
            "ON api_tokens (created_at DESC, id DESC)",
            ApiToken.__table__,
        ),
    }

    for name, (statement, expected_order, table) in expected.items():
        assert expected_order in statement
        assert name in {index.name for index in table.indexes}
