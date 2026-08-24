import importlib.util
from pathlib import Path

import pytest
from app.enums import ResourceType
from app.models import Item, Resource, ScanRun


def _load_migration(filename: str, module_name: str):
    migration_path = Path(__file__).parents[1] / "alembic" / "versions" / filename
    spec = importlib.util.spec_from_file_location(module_name, migration_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def schema_migration():
    return _load_migration("0010_add_sharepoint_inventory.py", "sharepoint_schema_migration")


@pytest.fixture(scope="module")
def index_migration():
    return _load_migration(
        "0011_add_sharepoint_identity_indexes.py",
        "sharepoint_index_migration",
    )


def test_sharepoint_migration_chain_and_enum(schema_migration, index_migration) -> None:
    assert schema_migration.down_revision == "0009_api_scale_indexes"
    assert index_migration.down_revision == schema_migration.revision
    assert ResourceType.SHAREPOINT_LIBRARY.value == "sharepoint_library"
    assert "collection_context" in ScanRun.__table__.columns


def test_provider_identity_indexes_match_model_and_use_partial_uniqueness(index_migration) -> None:
    statements = dict(index_migration.INDEXES)
    resource_indexes = {index.name for index in Resource.__table__.indexes}
    item_indexes = {index.name for index in Item.__table__.indexes}

    assert "uq_resources_run_endpoint_provider_id" in resource_indexes
    assert "uq_items_run_resource_provider_id" in item_indexes
    assert "WHERE provider_resource_id IS NOT NULL" in statements["uq_resources_run_endpoint_provider_id"]
    assert "WHERE provider_item_id IS NOT NULL" in statements["uq_items_run_resource_provider_id"]
    assert "CONCURRENTLY" in statements["ix_items_run_provider_exposure_id"]


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
def test_sharepoint_index_migration_recovers_interrupted_builds(
    monkeypatch,
    index_migration,
    validity: bool | None,
    expected: list[str],
) -> None:
    executed: list[str] = []
    monkeypatch.setattr(index_migration, "_index_validity", lambda _name: validity)
    monkeypatch.setattr(index_migration.op, "execute", executed.append)

    index_migration._ensure_valid_index("target_index", "CREATE INDEX CONCURRENTLY target")

    assert executed == expected


def test_sharepoint_schema_migration_is_split_from_concurrent_indexes(
    schema_migration,
    index_migration,
) -> None:
    schema_source = Path(schema_migration.__file__).read_text(encoding="utf-8")
    index_source = Path(index_migration.__file__).read_text(encoding="utf-8")

    assert "ADD VALUE IF NOT EXISTS 'sharepoint_library'" in schema_source
    assert "CREATE INDEX CONCURRENTLY" not in schema_source
    assert "CREATE INDEX CONCURRENTLY" in index_source
    assert "indisvalid" in index_source


def test_sharepoint_index_downgrade_preflights_duplicates_before_schema_mutation(
    monkeypatch,
    index_migration,
) -> None:
    queries: list[str] = []
    mutations: list[str] = []

    class _Result:
        def __init__(self, duplicate: bool) -> None:
            self.duplicate = duplicate

        def first(self):
            return (1,) if self.duplicate else None

    class _Connection:
        def execute(self, statement):
            sql = str(statement)
            queries.append(sql)
            return _Result("FROM resources" in sql)

    monkeypatch.setattr(index_migration.op, "get_bind", lambda: _Connection())
    monkeypatch.setattr(index_migration.op, "execute", mutations.append)
    monkeypatch.setattr(
        index_migration.op,
        "create_unique_constraint",
        lambda *_args, **_kwargs: mutations.append("create constraint"),
    )
    monkeypatch.setattr(
        index_migration.op,
        "get_context",
        lambda: (_ for _ in ()).throw(AssertionError("autocommit block must not start")),
    )

    with pytest.raises(RuntimeError, match="duplicate legacy keys in resources"):
        index_migration.downgrade()

    assert len(queries) == 2
    assert mutations == []
