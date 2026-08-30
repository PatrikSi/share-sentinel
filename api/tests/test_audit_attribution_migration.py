import importlib.util
import os
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from app.models import AuditEvent


def _load_migration(filename: str, module_name: str):
    path = Path(__file__).parents[1] / "alembic" / "versions" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_audit_attribution_migrations_match_models_and_are_online_safe() -> None:
    schema = _load_migration("0016_durable_audit_attribution.py", "audit_attribution_0016")
    indexes = _load_migration("0017_audit_attribution_online_index.py", "audit_attribution_0017")
    backfill = _load_migration("0018_backfill_audit_attribution.py", "audit_attribution_0018")

    assert schema.down_revision == "0015_monitoring_indexes"
    assert indexes.down_revision == schema.revision
    assert backfill.down_revision == indexes.revision
    assert {
        "actor_user_ref",
        "actor_email_snapshot",
        "actor_token_ref",
        "actor_token_name_snapshot",
        "project_ref",
        "project_name_snapshot",
    }.issubset(AuditEvent.__table__.columns.keys())
    assert {
        "ix_audit_events_project_ref_ts_id",
        "ix_audit_events_actor_user_id",
        "ix_audit_events_actor_token_id",
    }.issubset({index.name for index in AuditEvent.__table__.indexes})

    trigger_sql = "\n".join(
        (
            *schema.TRIGGER_FUNCTIONS,
            *schema.TRIGGERS,
            *backfill.PARENT_TRIGGER_FUNCTIONS,
            *backfill.PARENT_TRIGGERS,
        )
    ).lower()
    assert "before insert or update on audit_events" in trigger_sql
    assert "before delete on projects" in trigger_sql
    assert "before delete on users" in trigger_sql
    assert "before delete on api_tokens" in trigger_sql
    assert "token_hash" not in trigger_sql

    statements = dict(indexes.INDEXES)
    assert "CREATE INDEX CONCURRENTLY" in statements["ix_audit_events_project_ref_ts_id"]
    assert "(project_ref, ts, id)" in statements["ix_audit_events_project_ref_ts_id"]
    assert "WHERE actor_user_id IS NOT NULL" in statements["ix_audit_events_actor_user_id"]
    assert "WHERE actor_token_id IS NOT NULL" in statements["ix_audit_events_actor_token_id"]
    assert all("LIMIT :batch_size" in statement for statement in backfill.BACKFILL_STATEMENTS)
    assert backfill.BATCH_SIZE <= 5_000
    assert all("CREATE OR REPLACE FUNCTION" in statement for statement in backfill.PARENT_TRIGGER_FUNCTIONS)
    assert len(backfill.PARENT_TRIGGER_DROPS) == len(backfill.PARENT_TRIGGERS)


@pytest.mark.parametrize(
    ("validity", "expected_prefixes"),
    [
        (True, []),
        (None, ["CREATE INDEX CONCURRENTLY"]),
        (False, ["DROP INDEX CONCURRENTLY", "CREATE INDEX CONCURRENTLY"]),
    ],
)
def test_audit_attribution_index_migration_recovers_interrupted_builds(
    monkeypatch,
    validity: bool | None,
    expected_prefixes: list[str],
) -> None:
    indexes = _load_migration("0017_audit_attribution_online_index.py", "audit_index_recovery")
    executed: list[str] = []
    monkeypatch.setattr(indexes, "_index_validity", lambda _name: validity)
    monkeypatch.setattr(indexes.op, "execute", executed.append)

    indexes._ensure_valid_index("target_index", "CREATE INDEX CONCURRENTLY target_index")

    assert [statement.split(" ", 1)[0] + " " + statement.split(" ", 2)[1] for statement in executed] == [
        prefix.rsplit(" ", 1)[0] for prefix in expected_prefixes
    ]


def test_audit_backfill_advances_a_bounded_cursor_until_complete() -> None:
    backfill = _load_migration("0018_backfill_audit_attribution.py", "audit_backfill_cursor")
    calls: list[dict] = []
    results = iter([(2, 8), (1, 14), (0, None)])

    class _Result:
        def __init__(self, row):
            self.row = row

        def one(self):
            return self.row

    class _Connection:
        def execute(self, _statement, parameters):
            calls.append(parameters)
            return _Result(next(results))

    backfill._backfill_attribution(_Connection(), backfill.BACKFILL_STATEMENTS[0], batch_size=25)

    assert calls == [
        {"last_id": 0, "batch_size": 25},
        {"last_id": 8, "batch_size": 25},
        {"last_id": 14, "batch_size": 25},
    ]


@pytest.mark.skipif(
    not os.getenv("AUDIT_ATTRIBUTION_TEST_DATABASE_URL"),
    reason="set AUDIT_ATTRIBUTION_TEST_DATABASE_URL to exercise PostgreSQL triggers",
)
def test_postgresql_audit_snapshots_survive_renames_and_parent_deletion() -> None:
    migration = _load_migration("0016_durable_audit_attribution.py", "audit_attribution_pg_0016")
    backfill = _load_migration("0018_backfill_audit_attribution.py", "audit_attribution_pg_0018")
    database_url = os.environ["AUDIT_ATTRIBUTION_TEST_DATABASE_URL"]
    if database_url.startswith("postgresql://"):
        database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    engine = sa.create_engine(database_url)
    schema_name = f"audit_attr_test_{uuid.uuid4().hex}"
    project_id = uuid.uuid4()
    user_id = uuid.uuid4()
    token_id = uuid.uuid4()

    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema_name}"')
            connection.exec_driver_sql(f'SET LOCAL search_path TO "{schema_name}"')
            connection.exec_driver_sql(
                "CREATE TABLE projects (id uuid PRIMARY KEY, name varchar(255) NOT NULL)"
            )
            connection.exec_driver_sql(
                "CREATE TABLE users (id uuid PRIMARY KEY, email varchar(320) NOT NULL)"
            )
            connection.exec_driver_sql(
                """
                CREATE TABLE api_tokens (
                    id uuid PRIMARY KEY,
                    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    name varchar(120) NOT NULL
                )
                """
            )
            connection.exec_driver_sql(
                """
                CREATE TABLE audit_events (
                    id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    actor_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
                    actor_user_ref uuid,
                    actor_email_snapshot varchar(320),
                    actor_token_id uuid REFERENCES api_tokens(id) ON DELETE SET NULL,
                    actor_token_ref uuid,
                    actor_token_name_snapshot varchar(120),
                    project_id uuid REFERENCES projects(id) ON DELETE SET NULL,
                    project_ref uuid,
                    project_name_snapshot varchar(255)
                )
                """
            )
            params = {"project_id": project_id, "user_id": user_id, "token_id": token_id}
            connection.execute(
                sa.text("INSERT INTO projects (id, name) VALUES (:project_id, 'Finance')"),
                params,
            )
            connection.execute(
                sa.text("INSERT INTO users (id, email) VALUES (:user_id, 'old@example.com')"),
                params,
            )
            connection.execute(
                sa.text(
                    """
                    INSERT INTO api_tokens (id, user_id, project_id, name)
                    VALUES (:token_id, :user_id, :project_id, 'nightly-old')
                    """
                ),
                params,
            )
            # Simulate a legacy row written before the attribution trigger existed.
            connection.execute(
                sa.text(
                    """
                    INSERT INTO audit_events (actor_user_id, actor_token_id, project_id)
                    VALUES (:user_id, :token_id, :project_id)
                    """
                ),
                params,
            )
            for statement in migration.TRIGGER_FUNCTIONS:
                connection.exec_driver_sql(statement)
            for statement in migration.TRIGGERS:
                connection.exec_driver_sql(statement)
            for statement in backfill.PARENT_TRIGGER_DROPS:
                connection.exec_driver_sql(statement)
            for statement in backfill.PARENT_TRIGGER_FUNCTIONS:
                connection.exec_driver_sql(statement)
            for statement in backfill.PARENT_TRIGGERS:
                connection.exec_driver_sql(statement)
            # Simulate an interrupted 0018 rerun after its DDL committed but
            # before Alembic recorded the revision.
            for statement in backfill.PARENT_TRIGGER_DROPS:
                connection.exec_driver_sql(statement)
            for statement in backfill.PARENT_TRIGGER_FUNCTIONS:
                connection.exec_driver_sql(statement)
            for statement in backfill.PARENT_TRIGGERS:
                connection.exec_driver_sql(statement)
            for statement in backfill.BACKFILL_STATEMENTS:
                backfill._backfill_attribution(connection, statement, batch_size=1)

            # New events are snapshotted immediately while the earlier row
            # exercises the bounded legacy backfill.
            connection.execute(
                sa.text(
                    """
                    INSERT INTO audit_events (actor_user_id, actor_token_id, project_id)
                    VALUES (:user_id, :token_id, :project_id)
                    """
                ),
                params,
            )

            connection.execute(sa.text("UPDATE projects SET name = 'Finance renamed'"))
            connection.execute(sa.text("UPDATE users SET email = 'new@example.com'"))
            connection.execute(sa.text("UPDATE api_tokens SET name = 'nightly-new'"))
            connection.execute(
                sa.text("UPDATE audit_events SET project_name_snapshot = 'tampered'")
            )
            connection.execute(sa.text("DELETE FROM projects WHERE id = :project_id"), params)
            connection.execute(sa.text("DELETE FROM users WHERE id = :user_id"), params)

            rows = connection.execute(
                sa.text(
                    """
                    SELECT actor_user_id, actor_user_ref, actor_email_snapshot,
                           actor_token_id, actor_token_ref, actor_token_name_snapshot,
                           project_id, project_ref, project_name_snapshot
                    FROM audit_events
                    ORDER BY id
                    """
                )
            ).all()
            assert len(rows) == 2
            for row in rows:
                assert row.actor_user_id is None
                assert row.actor_user_ref == user_id
                assert row.actor_email_snapshot == "old@example.com"
                assert row.actor_token_id is None
                assert row.actor_token_ref == token_id
                assert row.actor_token_name_snapshot == "nightly-old"
                assert row.project_id is None
                assert row.project_ref == project_id
                assert row.project_name_snapshot == "Finance"
            assert connection.execute(
                sa.text("SELECT count(*) FROM audit_events WHERE project_ref = :project_id"),
                params,
            ).scalar_one() == 2
    finally:
        try:
            with engine.begin() as cleanup_connection:
                cleanup_connection.exec_driver_sql(
                    f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'
                )
        except sa.exc.SQLAlchemyError:
            # Cleanup is best effort and must not hide the primary regression result.
            pass
        engine.dispose()


@pytest.mark.skipif(
    not os.getenv("AUDIT_ATTRIBUTION_TEST_DATABASE_URL"),
    reason="set AUDIT_ATTRIBUTION_TEST_DATABASE_URL to exercise PostgreSQL batches",
)
def test_postgresql_audit_backfill_batches_commit_independently() -> None:
    backfill = _load_migration("0018_backfill_audit_attribution.py", "audit_backfill_pg_commit")
    database_url = os.environ["AUDIT_ATTRIBUTION_TEST_DATABASE_URL"]
    if database_url.startswith("postgresql://"):
        database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    engine = sa.create_engine(database_url)
    schema_name = f"audit_attr_batch_test_{uuid.uuid4().hex}"
    project_a = uuid.uuid4()
    project_b = uuid.uuid4()

    try:
        with engine.begin() as setup:
            setup.exec_driver_sql(f'CREATE SCHEMA "{schema_name}"')
            setup.exec_driver_sql(f'SET LOCAL search_path TO "{schema_name}"')
            setup.exec_driver_sql(
                "CREATE TABLE projects (id uuid PRIMARY KEY, name varchar(255) NOT NULL)"
            )
            setup.exec_driver_sql(
                """
                CREATE TABLE audit_events (
                    id bigint PRIMARY KEY,
                    project_id uuid REFERENCES projects(id),
                    project_ref uuid,
                    project_name_snapshot varchar(255),
                    CONSTRAINT fail_second_batch CHECK (id <> 2 OR project_ref IS NULL)
                )
                """
            )
            setup.execute(
                sa.text(
                    """
                    INSERT INTO projects (id, name)
                    VALUES (:project_a, 'A'), (:project_b, 'B')
                    """
                ),
                {"project_a": project_a, "project_b": project_b},
            )
            setup.execute(
                sa.text(
                    """
                    INSERT INTO audit_events (id, project_id)
                    VALUES (1, :project_a), (2, :project_b)
                    """
                ),
                {"project_a": project_a, "project_b": project_b},
            )

        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.exec_driver_sql(f'SET search_path TO "{schema_name}"')
            with pytest.raises(sa.exc.IntegrityError):
                backfill._backfill_attribution(
                    connection,
                    backfill.BACKFILL_STATEMENTS[0],
                    batch_size=1,
                )

        with engine.connect() as verification:
            verification.exec_driver_sql(f'SET search_path TO "{schema_name}"')
            rows = verification.execute(
                sa.text("SELECT id, project_ref FROM audit_events ORDER BY id")
            ).all()
            assert rows == [(1, project_a), (2, None)]
    finally:
        try:
            with engine.begin() as cleanup_connection:
                cleanup_connection.exec_driver_sql(
                    f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'
                )
        except sa.exc.SQLAlchemyError:
            pass
        engine.dispose()
