import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from app.models import ComparisonResourceChange, Item, PermissionPrincipal, Resource, RunComparison


def _load_migration(filename: str, module_name: str):
    migration_path = Path(__file__).parents[1] / "alembic" / "versions" / filename
    spec = importlib.util.spec_from_file_location(module_name, migration_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def schema_migration():
    return _load_migration(
        "0012_permission_evidence_comparisons.py",
        "permission_evidence_schema_migration",
    )


@pytest.fixture(scope="module")
def index_migration():
    return _load_migration(
        "0013_comparison_online_indexes.py",
        "permission_evidence_index_migration",
    )


def test_permission_evidence_migration_chain_is_split_at_online_indexes(
    schema_migration,
    index_migration,
) -> None:
    assert schema_migration.down_revision == "0011_sharepoint_indexes"
    assert index_migration.down_revision == schema_migration.revision

    schema_source = Path(schema_migration.__file__).read_text(encoding="utf-8")
    index_source = Path(index_migration.__file__).read_text(encoding="utf-8")
    assert "CREATE INDEX CONCURRENTLY" not in schema_source
    assert "CREATE INDEX CONCURRENTLY" in index_source
    assert "indisvalid" in index_source


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
def test_comparison_index_migration_recovers_interrupted_builds(
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


def test_comparison_indexes_and_unbounded_interpretation_match_models(index_migration) -> None:
    statements = dict(index_migration.INDEXES)
    assert "ix_resources_run_identity_id" in {index.name for index in Resource.__table__.indexes}
    assert "ix_items_run_identity_id" not in {index.name for index in Item.__table__.indexes}
    assert "ix_comparison_changes_impact_id" in {index.name for index in ComparisonResourceChange.__table__.indexes}
    assert "impact_rank DESC, id DESC" in statements["ix_comparison_changes_impact_id"]
    assert "provider, impact_rank DESC, id DESC" in statements["ix_comparison_changes_provider_impact_id"]
    assert "search_text gin_trgm_ops" in statements["ix_comparison_changes_search_trgm"]
    assert "change_categories" in statements["ix_comparison_changes_categories_gin"]
    assert isinstance(ComparisonResourceChange.__table__.c.access_interpretation.type, sa.Text)
    assert isinstance(ComparisonResourceChange.__table__.c.search_text.type, sa.Text)
    assert isinstance(RunComparison.__table__.c.next_retry_at.type, sa.DateTime)


def test_permission_principal_identity_is_database_enforced() -> None:
    assert PermissionPrincipal.__table__.c.authority.nullable is False
    assert PermissionPrincipal.__table__.c.native_id.nullable is False
