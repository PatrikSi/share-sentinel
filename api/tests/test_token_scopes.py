from app.enums import ProjectRole
from app.token_scopes import (
    SCOPE_READ_INVENTORY,
    SCOPE_READ_RUNS,
    SCOPE_WRITE_RUNS,
    default_scopes_for_project_role,
    has_required_scope,
    is_scope_allowed,
    normalize_token_scopes,
)


def test_normalize_token_scopes_lowercases_and_deduplicates() -> None:
    scopes = normalize_token_scopes(["Read:Runs", "read:runs", " write:runs ", ""])
    assert scopes == ["read:runs", "write:runs"]


def test_scope_matching_supports_write_implies_read_and_wildcards() -> None:
    assert has_required_scope({"write:runs"}, SCOPE_READ_RUNS)
    assert has_required_scope({"read:*"}, SCOPE_READ_INVENTORY)
    assert not has_required_scope({"read:runs"}, SCOPE_WRITE_RUNS)


def test_default_scopes_follow_project_role() -> None:
    viewer_scopes = default_scopes_for_project_role(ProjectRole.VIEWER)
    operator_scopes = default_scopes_for_project_role(ProjectRole.OPERATOR)
    admin_scopes = default_scopes_for_project_role(ProjectRole.ADMIN)

    assert "read:projects" in viewer_scopes
    assert "write:runs" not in viewer_scopes
    assert "write:runs" in operator_scopes
    assert "write:members" in admin_scopes
    assert "read:audit" in admin_scopes


def test_is_scope_allowed() -> None:
    assert is_scope_allowed("read:runs")
    assert not is_scope_allowed("drop:database")
