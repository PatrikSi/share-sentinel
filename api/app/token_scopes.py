from collections.abc import Iterable

from app.enums import ProjectRole

SCOPE_READ_PROJECTS = "read:projects"
SCOPE_WRITE_PROJECTS = "write:projects"
SCOPE_READ_RUNS = "read:runs"
SCOPE_WRITE_RUNS = "write:runs"
SCOPE_READ_INVENTORY = "read:inventory"
SCOPE_READ_AUDIT = "read:audit"
SCOPE_READ_MEMBERS = "read:members"
SCOPE_WRITE_MEMBERS = "write:members"
SCOPE_READ_TOKENS = "read:tokens"
SCOPE_WRITE_TOKENS = "write:tokens"
SCOPE_READ_USERS = "read:users"
SCOPE_WRITE_USERS = "write:users"

SCOPE_READ_ALL = "read:*"
SCOPE_WRITE_ALL = "write:*"
SCOPE_ADMIN_ALL = "admin:*"
SCOPE_ANY_ALL = "*:*"

ALLOWED_API_TOKEN_SCOPES = {
    SCOPE_READ_PROJECTS,
    SCOPE_WRITE_PROJECTS,
    SCOPE_READ_RUNS,
    SCOPE_WRITE_RUNS,
    SCOPE_READ_INVENTORY,
    SCOPE_READ_AUDIT,
    SCOPE_READ_MEMBERS,
    SCOPE_WRITE_MEMBERS,
    SCOPE_READ_TOKENS,
    SCOPE_WRITE_TOKENS,
    SCOPE_READ_USERS,
    SCOPE_WRITE_USERS,
    SCOPE_READ_ALL,
    SCOPE_WRITE_ALL,
    SCOPE_ADMIN_ALL,
    SCOPE_ANY_ALL,
}

_PROJECT_VIEWER_DEFAULT_SCOPES = (
    SCOPE_READ_PROJECTS,
    SCOPE_READ_RUNS,
    SCOPE_READ_INVENTORY,
)

_PROJECT_OPERATOR_DEFAULT_SCOPES = (
    *_PROJECT_VIEWER_DEFAULT_SCOPES,
    SCOPE_WRITE_RUNS,
)

_PROJECT_ADMIN_DEFAULT_SCOPES = (
    *_PROJECT_OPERATOR_DEFAULT_SCOPES,
    SCOPE_READ_AUDIT,
    SCOPE_READ_MEMBERS,
    SCOPE_WRITE_MEMBERS,
    SCOPE_READ_TOKENS,
    SCOPE_WRITE_TOKENS,
)


def normalize_token_scopes(scopes: Iterable[str] | None) -> list[str]:
    if scopes is None:
        return []
    normalized = {scope.strip().lower() for scope in scopes if scope and scope.strip()}
    return sorted(normalized)


def is_scope_allowed(scope: str) -> bool:
    return scope in ALLOWED_API_TOKEN_SCOPES


def has_required_scope(granted_scopes: set[str], required_scope: str) -> bool:
    if not required_scope:
        return True
    if SCOPE_ADMIN_ALL in granted_scopes or SCOPE_ANY_ALL in granted_scopes:
        return True
    if required_scope in granted_scopes:
        return True

    action, separator, resource = required_scope.partition(":")
    if not separator:
        return False

    wildcard_scope = f"{action}:*"
    if wildcard_scope in granted_scopes:
        return True

    # Write permission implies read for the same resource.
    if action == "read" and f"write:{resource}" in granted_scopes:
        return True
    return False


def default_scopes_for_project_role(role: ProjectRole) -> list[str]:
    if role == ProjectRole.ADMIN:
        return list(_PROJECT_ADMIN_DEFAULT_SCOPES)
    if role == ProjectRole.OPERATOR:
        return list(_PROJECT_OPERATOR_DEFAULT_SCOPES)
    return list(_PROJECT_VIEWER_DEFAULT_SCOPES)
