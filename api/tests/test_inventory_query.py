import pytest

from fastapi import HTTPException
from sqlalchemy import select

from app.models import Endpoint, Item, Resource
from app.routers import inventory as inventory_router
from app.services.inventory_query import InventoryQueryClause, parse_inventory_query


def test_parse_inventory_query_supports_verbose_compact_and_precedence() -> None:
    groups = parse_inventory_query('search contains "finance review" OR endpoint^fs- AND !ext=.tmp')

    assert len(groups) == 2
    assert groups[0] == [InventoryQueryClause(field="search", operator="contains", value="finance review", negated=False)]
    assert groups[1] == [
        InventoryQueryClause(field="endpoint", operator="startswith", value="fs-", negated=False),
        InventoryQueryClause(field="ext", operator="equals", value=".tmp", negated=True),
    ]


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
