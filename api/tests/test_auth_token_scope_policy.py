from types import SimpleNamespace

import pytest
from app.enums import ProjectRole
from app.routers import auth as auth_router
from fastapi import HTTPException


def test_non_sysadmin_scope_policy_accepts_default_scopes() -> None:
    owner = SimpleNamespace(is_sysadmin=False)
    scopes = ["read:projects", "read:runs", "read:inventory"]
    auth_router._enforce_scope_policy_for_owner(owner, ProjectRole.VIEWER, scopes)


def test_non_sysadmin_scope_policy_rejects_disallowed_scopes() -> None:
    owner = SimpleNamespace(is_sysadmin=False)
    with pytest.raises(HTTPException) as exc:
        auth_router._enforce_scope_policy_for_owner(owner, ProjectRole.VIEWER, ["write:runs"])
    assert exc.value.status_code == 400
    assert "non-sysadmin token scopes must match role defaults" in str(exc.value.detail)


def test_sysadmin_scope_policy_allows_broad_scopes() -> None:
    owner = SimpleNamespace(is_sysadmin=True)
    auth_router._enforce_scope_policy_for_owner(owner, ProjectRole.VIEWER, ["admin:*", "*:*"])
