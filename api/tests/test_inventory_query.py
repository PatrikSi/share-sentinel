import pytest
from app.models import Endpoint, Item, Resource
from app.routers import inventory as inventory_router
from app.services.inventory_query import (
    MAX_INVENTORY_QUERY_CHARS,
    MAX_INVENTORY_QUERY_CLAUSES,
    MAX_INVENTORY_QUERY_VALUE_CHARS,
    InventoryQueryClause,
    parse_inventory_query,
)
from fastapi import HTTPException
from sqlalchemy import select


def test_parse_inventory_query_supports_verbose_compact_and_precedence() -> None:
    groups = parse_inventory_query('search contains "finance review" OR endpoint^fs- AND !ext=.tmp')

    assert len(groups) == 2
    assert groups[0] == [InventoryQueryClause(field="search", operator="contains", value="finance review", negated=False)]
    assert groups[1] == [
        InventoryQueryClause(field="endpoint", operator="startswith", value="fs-", negated=False),
        InventoryQueryClause(field="ext", operator="equals", value=".tmp", negated=True),
    ]


def test_parse_inventory_query_supports_doubled_quotes_without_changing_backslashes() -> None:
    groups = parse_inventory_query(
        'search~"Bob\'s ""quarterly"" report" AND path^"\\\\HR\\Team"'
    )

    assert groups == [[
        InventoryQueryClause(
            field="search",
            operator="contains",
            value='Bob\'s "quarterly" report',
        ),
        InventoryQueryClause(
            field="path",
            operator="startswith",
            value="\\\\HR\\Team",
        ),
    ]]


def test_parse_inventory_query_normalizes_aliases_and_not_keyword() -> None:
    groups = parse_inventory_query("NOT hostname equals fs-01 !path startswith \\\\HR\\")

    assert groups == [[
        InventoryQueryClause(field="endpoint", operator="equals", value="fs-01", negated=True),
        InventoryQueryClause(field="path", operator="startswith", value="\\\\HR\\", negated=True),
    ]]


@pytest.mark.parametrize(
    ("raw", "detail"),
    [
        ("unknown contains test", "unsupported inventory query field"),
        ("endpoint between fs-01", "unsupported operator"),
        ('share contains "unterminated', "unterminated quoted value"),
        ("endpoint contains", "expected value"),
        ("endpoint contains fs-01 OR", "ended after a boolean operator"),
    ],
)
def test_parse_inventory_query_rejects_invalid_input(raw: str, detail: str) -> None:
    with pytest.raises(HTTPException) as exc_info:
        parse_inventory_query(raw)

    assert detail in exc_info.value.detail


def test_parse_inventory_query_rejects_unbounded_input() -> None:
    with pytest.raises(HTTPException, match="too long") as length_error:
        parse_inventory_query("x" * (MAX_INVENTORY_QUERY_CHARS + 1))
    assert length_error.value.status_code == 400

    with pytest.raises(HTTPException, match="too complex") as clause_error:
        parse_inventory_query(" AND ".join("search=value" for _ in range(MAX_INVENTORY_QUERY_CLAUSES + 1)))
    assert clause_error.value.status_code == 400

    with pytest.raises(HTTPException, match="value is too long") as value_error:
        parse_inventory_query(f"search:{'x' * (MAX_INVENTORY_QUERY_VALUE_CHARS + 1)}")
    assert value_error.value.status_code == 400


def test_resource_and_endpoint_query_builders_use_exists_for_child_item_filters() -> None:
    clause = InventoryQueryClause(field="ext", operator="equals", value=".xlsx", negated=True)

    resource_sql = str(
        inventory_router._resource_inventory_clause_expression(clause).compile(compile_kwargs={"literal_binds": True})
    )
    endpoint_sql = str(
        inventory_router._endpoint_inventory_clause_expression(clause).compile(compile_kwargs={"literal_binds": True})
    )

    assert "NOT (EXISTS" in resource_sql.upper()
    assert "NOT (EXISTS" in endpoint_sql.upper()


def test_string_match_expression_coalesces_nullable_values_for_negation_safe_queries() -> None:
    sql = str(inventory_router._item_inventory_clause_expression(InventoryQueryClause(field="ext", operator="equals", value=".md", negated=True)))

    assert "coalesce" in sql.lower()


def test_endpoint_query_groups_compile_when_or_clauses_mix_share_and_item_predicates() -> None:
    stmt = (
        select(Endpoint.id)
        .select_from(Endpoint)
        .outerjoin(Resource, (Resource.endpoint_id == Endpoint.id) & (Resource.run_id == Endpoint.run_id))
        .outerjoin(Item, (Item.resource_id == Resource.id) & (Item.run_id == Resource.run_id))
    )
    groups = parse_inventory_query("share contains Engineering OR ext = .vsdx")

    compiled = str(
        inventory_router._apply_inventory_query_groups(stmt, groups, inventory_router._endpoint_inventory_clause_expression).compile()
    )

    assert "exists" in compiled.lower()


def test_parse_inventory_query_supports_provider_resource_type_exposure_and_source() -> None:
    groups = parse_inventory_query(
        "provider=sharepoint resource_type=sharepoint_library "
        "exposure=ANONYMOUS source=sharepoint"
    )

    assert groups == [[
        InventoryQueryClause(field="provider", operator="equals", value="sharepoint"),
        InventoryQueryClause(
            field="resource_type",
            operator="equals",
            value="sharepoint_library",
        ),
        InventoryQueryClause(field="exposure", operator="equals", value="ANONYMOUS"),
        InventoryQueryClause(field="source", operator="equals", value="sharepoint"),
    ]]


def test_parse_inventory_query_supports_item_type_aliases() -> None:
    assert parse_inventory_query("item_type=directory OR kind=file") == [
        [InventoryQueryClause(field="item_type", operator="equals", value="directory")],
        [InventoryQueryClause(field="item_type", operator="equals", value="file")],
    ]


def test_item_type_query_compiles_for_each_inventory_level() -> None:
    clause = InventoryQueryClause(field="item_type", operator="equals", value="folder")

    item_sql = str(
        inventory_router._item_inventory_clause_expression(clause).compile(
            compile_kwargs={"literal_binds": True}
        )
    ).lower()
    resource_sql = str(
        inventory_router._resource_inventory_clause_expression(clause).compile(
            compile_kwargs={"literal_binds": True}
        )
    ).lower()
    endpoint_sql = str(
        inventory_router._endpoint_inventory_clause_expression(clause).compile(
            compile_kwargs={"literal_binds": True}
        )
    ).lower()

    assert "items.is_dir is true" in item_sql
    assert "directory" in item_sql
    assert "exists" in resource_sql
    assert "items.is_dir is true" in resource_sql
    assert "exists" in endpoint_sql
    assert "items.is_dir is true" in endpoint_sql


def test_file_archive_status_query_parses_aliases_and_compiles_for_each_inventory_level() -> None:
    assert parse_inventory_query("file_archive_state=archived") == [[
        InventoryQueryClause(
            field="file_archive_status",
            operator="equals",
            value="archived",
        ),
    ]]
    clause = InventoryQueryClause(
        field="file_archive_status",
        operator="equals",
        value="fully_archived",
    )

    item_sql = str(
        inventory_router._item_inventory_clause_expression(clause).compile(
            compile_kwargs={"literal_binds": True}
        )
    ).lower()
    resource_sql = str(
        inventory_router._resource_inventory_clause_expression(clause).compile(
            compile_kwargs={"literal_binds": True}
        )
    ).lower()
    endpoint_sql = str(
        inventory_router._endpoint_inventory_clause_expression(clause).compile(
            compile_kwargs={"literal_binds": True}
        )
    ).lower()

    assert "provider_metadata" in item_sql
    assert "file_archive_status" in item_sql
    assert "exists" in resource_sql
    assert "file_archive_status" in resource_sql
    assert "exists" in endpoint_sql
    assert "file_archive_status" in endpoint_sql


def test_generic_archive_status_is_not_accepted_as_a_file_status_alias() -> None:
    with pytest.raises(HTTPException, match="unsupported inventory query field"):
        parse_inventory_query("archive_status=fully_archived")


def test_parse_inventory_query_normalizes_visibility_to_exposure() -> None:
    assert parse_inventory_query("visibility=EXTERNAL") == [[
        InventoryQueryClause(field="exposure", operator="equals", value="EXTERNAL"),
    ]]


def test_sharepoint_inventory_query_fields_compile_against_provider_columns() -> None:
    clauses = [
        InventoryQueryClause(field="provider", operator="equals", value="sharepoint"),
        InventoryQueryClause(
            field="resource_type",
            operator="equals",
            value="sharepoint_library",
        ),
        InventoryQueryClause(field="exposure", operator="equals", value="ANONYMOUS"),
        InventoryQueryClause(field="source", operator="equals", value="sharepoint"),
    ]

    item_sql = " ".join(
        str(inventory_router._item_inventory_clause_expression(clause)) for clause in clauses
    ).lower()
    resource_sql = " ".join(
        str(inventory_router._resource_inventory_clause_expression(clause)) for clause in clauses
    ).lower()
    endpoint_sql = " ".join(
        str(inventory_router._endpoint_inventory_clause_expression(clause)) for clause in clauses
    ).lower()

    assert "items.provider" in item_sql
    assert "resources.resource_type" in item_sql
    assert "items.exposure" in item_sql
    assert "collection_context" in item_sql
    assert "resources.provider" in resource_sql
    assert "resources.resource_type" in resource_sql
    assert "resources.exposure" in resource_sql
    assert "endpoints.provider" in endpoint_sql
    assert "exists" in endpoint_sql


def test_endpoint_provider_query_matches_direct_or_child_resource_provider() -> None:
    sql = str(
        inventory_router._endpoint_inventory_clause_expression(
            InventoryQueryClause(field="provider", operator="equals", value="smb")
        ).compile(compile_kwargs={"literal_binds": True})
    ).lower()

    assert "endpoints.provider" in sql
    assert "resources.provider" in sql
    assert "exists" in sql
    assert " or " in sql


def test_endpoint_search_query_includes_sharepoint_site_metadata() -> None:
    sql = str(
        inventory_router._endpoint_inventory_clause_expression(
            InventoryQueryClause(field="search", operator="contains", value="Finance")
        ).compile(compile_kwargs={"literal_binds": True})
    ).lower()

    assert "provider_metadata" in sql
    assert "display_name" in sql
    assert "site_name" in sql
    assert "web_url" in sql
