from __future__ import annotations

import hashlib
import json
import threading
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable, Iterable, Protocol
from urllib.parse import quote

from .graph import GraphAPIError, GraphProtocolError

PERMISSION_SEMANTICS = "sharepoint_graph_permission_v1"
PERMISSION_SURFACE = "sharepoint_graph_permissions"
PERMISSION_METHOD = "graph_driveitem_permissions"
PRINCIPAL_RESOLUTION = "provider_identifiers_no_group_expansion"
PERMISSION_SELECT = (
    "id,roles,expirationDateTime,hasPassword,grantedToV2,grantedToIdentitiesV2,inheritedFrom,invitation,link"
)
PROVIDER_ID_MAX_CHARACTERS = 512
DISPLAY_ALIAS_MAX_CHARACTERS = 512
MAX_ROLES_PER_PERMISSION = 16
MAX_IDENTITIES_PER_PERMISSION = 4096

_KNOWN_ROLES = frozenset({"read", "write", "owner"})
_KNOWN_LINK_SCOPES = {
    "anonymous": "anonymous",
    "organization": "organization",
    "users": "users",
    "existingaccess": "existing_access",
}
_KNOWN_LINK_TYPES = frozenset({"view", "edit", "embed"})
_KNOWN_PERMISSION_FIELDS = frozenset(
    {
        "id",
        "roles",
        "expirationDateTime",
        "hasPassword",
        "grantedTo",
        "grantedToIdentities",
        "grantedToV2",
        "grantedToIdentitiesV2",
        "inheritedFrom",
        "invitation",
        "link",
        "shareId",
    }
)
_KNOWN_LINK_FIELDS = frozenset(
    {
        "application",
        "preventsDownload",
        "scope",
        "type",
        "webHtml",
        "webUrl",
    }
)
_IDENTITY_FACETS = (
    ("user", "user", "entra_object_id", "entra_id"),
    ("group", "group", "entra_object_id", "entra_id"),
    ("application", "application", "entra_object_id", "entra_id"),
    ("device", "device", "entra_object_id", "entra_id"),
    ("siteUser", "site_user", "sharepoint_principal_id", "sharepoint"),
    ("siteGroup", "site_group", "sharepoint_principal_id", "sharepoint"),
)
_BASE_LIMITATIONS = (
    "caller_dependent_permission_visibility",
    "group_membership_not_expanded",
    "inheritance_not_returned_for_sharepoint_document_libraries",
    "negative_exposure_conclusion_not_supported",
)


class PermissionGraphClient(Protocol):
    def get(self, url: str, *, attempt_budget=None) -> dict[str, object]: ...

    def iter_pages(self, url: str, *, attempt_budget=None) -> Iterable[dict[str, object]]: ...


@dataclass(frozen=True)
class PermissionSubject:
    endpoint_key: str
    resource_name: str
    site_id: str
    drive_id: str
    item_id: str | None
    subject_kind: str
    subject_path: str | None = None


@dataclass(frozen=True)
class PermissionAssessmentResult:
    assessment_record: dict[str, object] | None
    entry_records: tuple[dict[str, object], ...]
    permission_summary: dict[str, object]
    exposure: str
    exposure_evidence: dict[str, object]
    assessment_state: str
    complete: bool


def _canonical_hash(value: object) -> str:
    serialized = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _safe_text(value: object, maximum: int, *, strip: bool = True) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = unicodedata.normalize("NFC", value)
    if strip:
        normalized = normalized.strip()
    if not normalized or len(normalized) > maximum or any(not character.isprintable() for character in normalized):
        return None
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return normalized


def _safe_timestamp(value: object) -> str | None:
    normalized = _safe_text(value, 128)
    if normalized is None:
        return None
    try:
        datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except (OverflowError, ValueError):
        return None
    return normalized


def _expiration_state(value: str | None) -> str:
    if value is None:
        return "not_set"
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.year == 1:
        return "not_set"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return "expired" if parsed.astimezone(UTC) <= datetime.now(tz=UTC) else "active"


def _assessment_key(tenant_id: str, subject: PermissionSubject) -> str:
    fingerprint = _canonical_hash(
        {
            "tenant_id": tenant_id,
            "site_id": subject.site_id,
            "drive_id": subject.drive_id,
            "item_id": subject.item_id if subject.subject_kind == "item" else "<root>",
            "semantics": PERMISSION_SEMANTICS,
        }
    )
    return f"spa:v1:{fingerprint}"


def _subject_key(tenant_id: str, subject: PermissionSubject) -> str:
    """Identify the Graph object without mutable display names or paths."""

    return _canonical_hash(
        {
            "tenant_id": tenant_id,
            "site_id": subject.site_id,
            "drive_id": subject.drive_id,
            "item_id": subject.item_id if subject.subject_kind == "item" else "<root>",
            "subject_kind": subject.subject_kind,
        }
    )


def _principal_from_identity_set(
    raw: object,
    *,
    tenant_id: str,
    site_id: str,
) -> tuple[dict[str, object] | None, bool]:
    if not isinstance(raw, dict):
        return None, False

    directory_id: str | None = None
    sharepoint_id: str | None = None
    primary_kind: str | None = None
    primary_namespace: str | None = None
    primary_authority: str | None = None
    aliases: list[str] = []
    display_name: str | None = None
    login_name: str | None = None
    valid = True

    for facet_name, kind, namespace, authority in _IDENTITY_FACETS:
        facet = raw.get(facet_name)
        if facet is None:
            continue
        if not isinstance(facet, dict):
            valid = False
            continue
        native_id = _safe_text(facet.get("id"), PROVIDER_ID_MAX_CHARACTERS)
        if native_id is None:
            valid = False
        elif authority == "entra_id" and directory_id is None:
            directory_id = native_id
        elif authority == "sharepoint" and sharepoint_id is None:
            sharepoint_id = native_id
        if primary_kind is None and native_id is not None:
            primary_kind = kind
            primary_namespace = namespace
            primary_authority = authority
        for alias_field in ("displayName", "loginName"):
            alias = _safe_text(facet.get(alias_field), DISPLAY_ALIAS_MAX_CHARACTERS)
            if alias and alias not in aliases:
                aliases.append(alias)
            if alias_field == "displayName" and display_name is None:
                display_name = alias
            elif alias_field == "loginName" and login_name is None:
                login_name = alias

    native_id = directory_id or sharepoint_id
    if native_id is None or primary_kind is None or primary_namespace is None or primary_authority is None:
        return None, False
    principal_fingerprint = _canonical_hash(
        {
            "tenant_id": tenant_id,
            "site_id": site_id,
            "kind": primary_kind,
            "directory_object_id": directory_id,
            "sharepoint_principal_id": sharepoint_id,
        }
    )
    # Entra object IDs are tenant-scoped, while SharePoint site-user and
    # site-group IDs are only meaningful within one site collection. Carry a
    # bounded, opaque authority identifier so downstream consumers can own
    # principal keys without accidentally collapsing site-local identifiers.
    authority_scope = {
        "tenant_id": tenant_id,
        "site_id": site_id if primary_authority == "sharepoint" else None,
    }
    canonical_authority = f"{primary_authority}:v1:{_canonical_hash(authority_scope)}"
    principal_key = f"spp:v1:{principal_fingerprint}"
    return (
        {
            "provider": "sharepoint",
            "identifier_namespace": primary_namespace,
            "principal_key": principal_key,
            "kind": primary_kind,
            "native_id": native_id,
            "authority": canonical_authority,
            "display_name": display_name,
            "login_name": login_name,
            "aliases": aliases[:4],
            "resolution": PRINCIPAL_RESOLUTION,
            "resolution_source": "microsoft_graph_permission_identity_set",
        },
        valid,
    )


def _invitation_principal(
    invitation: dict[str, object],
    *,
    tenant_id: str,
    site_id: str,
) -> tuple[dict[str, object] | None, bool]:
    raw_email = invitation.get("email")
    if raw_email is None:
        return None, True
    email = _safe_text(raw_email, 320)
    if email is None:
        return None, False
    principal_key = f"spp:v1:{_canonical_hash({'tenant_id': tenant_id, 'site_id': site_id, 'email': email.casefold()})}"
    authority = f"sharepoint_invitation:v1:{_canonical_hash({'tenant_id': tenant_id, 'site_id': site_id})}"
    return (
        {
            "provider": "sharepoint",
            "identifier_namespace": "invitation_email",
            "principal_key": principal_key,
            "kind": "invitation_email",
            "native_id": email,
            "authority": authority,
            "email": email,
            "aliases": [email],
            "resolution": "invitation_not_directory_resolved",
            "resolution_source": "microsoft_graph_permission_invitation",
        },
        True,
    )


def _normalize_roles(raw: object) -> tuple[list[str], bool]:
    if not isinstance(raw, list) or len(raw) > MAX_ROLES_PER_PERMISSION:
        return [], False
    roles: list[str] = []
    valid = True
    for value in raw:
        role = _safe_text(value, 80)
        if role is None:
            valid = False
            continue
        normalized = role.casefold()
        if normalized not in roles:
            roles.append(normalized)
        if normalized not in _KNOWN_ROLES:
            valid = False
    roles.sort()
    return roles, valid


def _normalize_inherited_from(raw: object) -> tuple[str, dict[str, str] | None, bool]:
    if raw is None:
        # The generic driveItem contract defines inheritedFrom, but Microsoft
        # documents that SharePoint document libraries do not return it.
        # Absence therefore cannot distinguish a direct from inherited entry.
        return "unknown", None, True
    if not isinstance(raw, dict):
        return "unknown", None, False
    item_id = _safe_text(raw.get("id"), PROVIDER_ID_MAX_CHARACTERS)
    drive_id = _safe_text(raw.get("driveId"), PROVIDER_ID_MAX_CHARACTERS)
    if item_id is None:
        return "unknown", None, False
    source = {"provider_item_id": item_id}
    if drive_id is not None:
        source["provider_drive_id"] = drive_id
    return "inherited", source, True


def _normalize_permission(
    raw: object,
    *,
    tenant_id: str,
    subject: PermissionSubject,
) -> tuple[list[dict[str, object]], bool]:
    if not isinstance(raw, dict):
        return [], False
    permission_id = _safe_text(raw.get("id"), PROVIDER_ID_MAX_CHARACTERS)
    if permission_id is None:
        return [], False

    semantic_complete = not any(key not in _KNOWN_PERMISSION_FIELDS and not key.startswith("@odata.") for key in raw)
    roles, roles_complete = _normalize_roles(raw.get("roles"))
    semantic_complete = semantic_complete and roles_complete

    raw_link = raw.get("link")
    link: dict[str, object] | None
    if raw_link is None:
        link = None
    elif isinstance(raw_link, dict):
        link = raw_link
    else:
        link = None
        semantic_complete = False

    raw_invitation = raw.get("invitation")
    invitation: dict[str, object] | None
    if raw_invitation is None:
        invitation = None
    elif isinstance(raw_invitation, dict):
        invitation = raw_invitation
    else:
        invitation = None
        semantic_complete = False

    if link is not None and any(key not in _KNOWN_LINK_FIELDS for key in link):
        semantic_complete = False
    raw_scope = _safe_text(link.get("scope"), 128) if link is not None else None
    link_scope = _KNOWN_LINK_SCOPES.get(raw_scope.casefold(), "unknown") if raw_scope else None
    if link is not None and link_scope is None:
        link_scope = "unknown"
    if link is not None and link_scope == "unknown":
        semantic_complete = False
    raw_link_type = _safe_text(link.get("type"), 128) if link is not None else None
    link_type = raw_link_type.casefold() if raw_link_type else None
    if link is not None and link_type not in _KNOWN_LINK_TYPES:
        link_type = "unknown"
        semantic_complete = False
    prevents_download = link.get("preventsDownload") if link is not None else None
    if prevents_download is not None and not isinstance(prevents_download, bool):
        prevents_download = None
        semantic_complete = False

    has_password = raw.get("hasPassword")
    if has_password is not None and not isinstance(has_password, bool):
        has_password = None
        semantic_complete = False

    sign_in_required = invitation.get("signInRequired") if invitation is not None else None
    if sign_in_required is not None and not isinstance(sign_in_required, bool):
        sign_in_required = None
        semantic_complete = False

    expiration_at = _safe_timestamp(raw.get("expirationDateTime"))
    expiration_invalid = raw.get("expirationDateTime") is not None and expiration_at is None
    if expiration_invalid:
        semantic_complete = False
    expiration_state = "unknown" if expiration_invalid else _expiration_state(expiration_at)

    inherited_state, inherited_from, inheritance_complete = _normalize_inherited_from(raw.get("inheritedFrom"))
    semantic_complete = semantic_complete and inheritance_complete

    identity_sets: list[object] = []
    raw_single = raw.get("grantedToV2")
    if raw_single is not None:
        identity_sets.append(raw_single)
    raw_multiple = raw.get("grantedToIdentitiesV2")
    if raw_multiple is not None:
        if not isinstance(raw_multiple, list) or len(raw_multiple) > MAX_IDENTITIES_PER_PERMISSION:
            semantic_complete = False
        else:
            identity_sets.extend(raw_multiple)
            if not raw_multiple and raw_single is None and link is None and invitation is None:
                semantic_complete = False

    principals: list[dict[str, object] | None] = []
    for identity_set in identity_sets:
        principal, principal_complete = _principal_from_identity_set(
            identity_set,
            tenant_id=tenant_id,
            site_id=subject.site_id,
        )
        semantic_complete = semantic_complete and principal_complete
        if principal is not None:
            principals.append(principal)
    if not principals and invitation is not None:
        principal, principal_complete = _invitation_principal(
            invitation,
            tenant_id=tenant_id,
            site_id=subject.site_id,
        )
        semantic_complete = semantic_complete and principal_complete
        if principal is not None:
            principals.append(principal)
    if not principals:
        principals.append(None)

    if all(facet is None for facet in (link, invitation, raw_single, raw_multiple)):
        entry_kind = "unknown"
        semantic_complete = False
    elif link is not None and invitation is not None:
        entry_kind = "mixed"
        semantic_complete = False
    elif link is not None:
        entry_kind = "link"
    elif invitation is not None:
        entry_kind = "invitation"
    else:
        entry_kind = "identity_grant"

    effect = "no_new_access" if link_scope == "existing_access" else "allow"
    if entry_kind in {"unknown", "mixed"}:
        effect = "unknown"
    elif expiration_state in {"expired", "unknown"}:
        effect = expiration_state
    provider_details: dict[str, object] = {
        "permission_kind": entry_kind,
        "roles": roles,
        "link_scope": link_scope,
        "link_type": link_type,
        "prevents_download": prevents_download,
        "has_password": has_password,
        "invitation_sign_in_required": sign_in_required,
        "inherited_from": inherited_from,
        "expiration_state": expiration_state,
    }
    entries: list[dict[str, object]] = []
    seen_principals: set[str] = set()
    for principal in principals:
        principal_key = str(principal.get("principal_key")) if principal is not None else None
        dedupe_key = principal_key or "<none>"
        if dedupe_key in seen_principals:
            semantic_complete = False
            continue
        seen_principals.add(dedupe_key)
        entry_key = f"spe:v1:{_canonical_hash({'subject_key': _subject_key(tenant_id, subject), 'permission_id': permission_id, 'principal_key': principal_key})}"
        stable_evidence = {
            "provider_entry_id": permission_id,
            "principal_key": principal_key,
            "entry_kind": entry_kind,
            "effect": effect,
            "normalized_rights": roles,
            "expiration_at": expiration_at,
            "provider_details": provider_details,
        }
        entry: dict[str, object] = {
            "entry_key": entry_key,
            "provider_entry_id": permission_id,
            "principal_key": principal_key,
            "entry_kind": entry_kind,
            "effect": effect,
            "normalized_rights": roles,
            "inherited_state": inherited_state,
            "expiration_at": expiration_at,
            "evidence_hash": _canonical_hash(stable_evidence),
            "provider_details": provider_details,
        }
        if principal is not None:
            entry["principal"] = principal
        entries.append(entry)
    return entries, semantic_complete


class DirectPermissionCollector:
    """Collect direct Graph permission evidence under shared, run-wide budgets."""

    def __init__(
        self,
        *,
        client: PermissionGraphClient,
        run_id: str,
        tenant_id: str,
        mode: str,
        max_objects: int,
        max_http_attempts: int,
        max_entries: int,
        concurrency: int,
        on_error: Callable[[str, BaseException, PermissionSubject], None] | None = None,
    ) -> None:
        self.client = client
        self.run_id = run_id
        self.tenant_id = tenant_id
        self.mode = mode
        self.max_objects = max(1, int(max_objects))
        self.max_http_attempts = max(1, int(max_http_attempts))
        self.max_entries = max(1, int(max_entries))
        self.concurrency = max(1, int(concurrency))
        self.on_error = on_error
        self._lock = threading.Lock()
        self._counters = {
            "candidate_objects": 0,
            "attempted_objects": 0,
            "completed_objects": 0,
            "failed_objects": 0,
            "skipped_objects": 0,
            "http_attempts": 0,
            "entries_observed": 0,
            "entries_emitted": 0,
            "entries_omitted": 0,
            "unknown_entries": 0,
            "anonymous_objects": 0,
            "broad_internal_objects": 0,
            "selection_incomplete_scopes": 0,
        }
        self._circuit_reason: str | None = None
        self._reported_error_codes: set[str] = set()
        self._selection_partial_reasons: set[str] = set()

    def reserve_attempt(self) -> bool:
        with self._lock:
            if self._counters["http_attempts"] >= self.max_http_attempts:
                self._circuit_reason = "budget_exhausted"
                return False
            self._counters["http_attempts"] += 1
            return True

    def _reserve_object(self) -> str | None:
        with self._lock:
            self._counters["candidate_objects"] += 1
            if self._circuit_reason:
                self._counters["skipped_objects"] += 1
                return self._circuit_reason
            if self._counters["attempted_objects"] >= self.max_objects:
                self._circuit_reason = "budget_exhausted"
                self._counters["skipped_objects"] += 1
                return "budget_exhausted"
            self._counters["attempted_objects"] += 1
            return None

    def _reserve_entry(self) -> bool:
        with self._lock:
            self._counters["entries_observed"] += 1
            if self._counters["entries_emitted"] >= self.max_entries:
                self._counters["entries_omitted"] += 1
                return False
            self._counters["entries_emitted"] += 1
            return True

    def _record_unknown(self, count: int = 1) -> None:
        with self._lock:
            self._counters["unknown_entries"] += max(0, int(count))

    def mark_selection_incomplete(self, reason: str) -> None:
        """Record that an upstream discovery/content failure hid candidate objects."""

        normalized = _safe_text(reason, 128) or "upstream_collection_failed"
        with self._lock:
            self._counters["selection_incomplete_scopes"] += 1
            self._selection_partial_reasons.add(normalized)

    def _finish(self, *, complete: bool, exposure: str) -> None:
        with self._lock:
            self._counters["completed_objects" if complete else "failed_objects"] += 1
            if exposure == "ANONYMOUS":
                self._counters["anonymous_objects"] += 1
            elif exposure == "BROAD_INTERNAL":
                self._counters["broad_internal_objects"] += 1

    def _trip_circuit(self, reason: str) -> None:
        if reason not in {"authentication_failed", "temporarily_unreachable", "budget_exhausted"}:
            return
        with self._lock:
            if self._circuit_reason is None or reason == "authentication_failed":
                self._circuit_reason = reason

    def _report_error_once(self, code: str, exc: BaseException, subject: PermissionSubject) -> None:
        if self.on_error is None:
            return
        with self._lock:
            if code in self._reported_error_codes:
                return
            self._reported_error_codes.add(code)
        self.on_error(code, exc, subject)

    @staticmethod
    def _error_state(exc: BaseException) -> tuple[str, str, str]:
        if isinstance(exc, GraphAPIError):
            if exc.code == "request_budget_exhausted":
                return "budget_exhausted", "partial", "PERMISSION_HTTP_BUDGET_EXHAUSTED"
            if exc.status_code == 401:
                return "authentication_failed", "partial", "PERMISSION_AUTHENTICATION_FAILED"
            if exc.status_code == 403:
                return "permission_denied", "partial", "PERMISSION_DENIED"
            if exc.status_code == 404:
                return "not_found_or_not_visible", "partial", "PERMISSION_NOT_FOUND_OR_NOT_VISIBLE"
            if exc.status_code == 429 or exc.code in {"throttled", "retry_after_exceeds_budget"}:
                return "temporarily_unreachable", "partial", "PERMISSION_THROTTLED"
            if exc.retryable or exc.status_code in {408, 500, 502, 503, 504}:
                return "temporarily_unreachable", "partial", "PERMISSION_TEMPORARILY_UNREACHABLE"
            if isinstance(exc, GraphProtocolError):
                return "protocol_error", "partial", "PERMISSION_PROTOCOL_ERROR"
        return "protocol_error", "partial", "PERMISSION_COLLECTION_FAILED"

    def _skipped_result(
        self,
        subject: PermissionSubject,
        *,
        reason: str,
        base_exposure: str,
        base_evidence: dict[str, object],
    ) -> PermissionAssessmentResult:
        state = "not_assessed"
        summary: dict[str, object] = {
            "assessment_key": None,
            "assessment_state": state,
            "failure_reason": reason,
            "selection_scope": self.mode,
            "selection_coverage": "budget_truncated" if reason == "budget_exhausted" else "not_attempted",
            "retrieval_coverage": "not_attempted",
            "provider_visibility": "caller_dependent_unverified",
            "semantic_coverage": "not_assessed",
            "principal_resolution": "not_assessed",
            "effective_access_status": "not_computed",
            "negative_conclusion_supported": False,
            "entries_observed": 0,
            "entries_emitted": 0,
            "entries_omitted": 0,
            "unknown_entries": 0,
            "entry_set_hash": None,
            "exposure": base_exposure,
            "positive_evidence": [],
            "limitations": list(_BASE_LIMITATIONS) + [f"permission_{reason}"],
        }
        evidence = dict(base_evidence)
        evidence["permission_summary"] = summary
        return PermissionAssessmentResult(None, (), summary, base_exposure, evidence, state, False)

    def assess_root(
        self,
        subject: PermissionSubject,
        *,
        base_exposure: str,
        base_evidence: dict[str, object],
    ) -> PermissionAssessmentResult:
        reserve_failure = self._reserve_object()
        if reserve_failure:
            return self._skipped_result(
                subject,
                reason=reserve_failure,
                base_exposure=base_exposure,
                base_evidence=base_evidence,
            )
        try:
            root = self.client.get(
                f"drives/{quote(subject.drive_id, safe='')}/root?$select=id",
                attempt_budget=self,
            )
            root_id = _safe_text(root.get("id"), PROVIDER_ID_MAX_CHARACTERS)
            if root_id is None:
                raise GraphProtocolError(status_code=None, code="permission_root_missing_id")
            resolved = PermissionSubject(
                endpoint_key=subject.endpoint_key,
                resource_name=subject.resource_name,
                site_id=subject.site_id,
                drive_id=subject.drive_id,
                item_id=root_id,
                subject_kind="resource",
                subject_path=None,
            )
            return self._assess_reserved(
                resolved,
                base_exposure=base_exposure,
                base_evidence=base_evidence,
                assessment_item_id=None,
            )
        except (GraphAPIError, GraphProtocolError) as exc:
            return self._failure_result(
                subject,
                exc,
                base_exposure=base_exposure,
                base_evidence=base_evidence,
            )

    def assess_item(
        self,
        subject: PermissionSubject,
        *,
        base_exposure: str,
        base_evidence: dict[str, object],
    ) -> PermissionAssessmentResult:
        reserve_failure = self._reserve_object()
        if reserve_failure:
            return self._skipped_result(
                subject,
                reason=reserve_failure,
                base_exposure=base_exposure,
                base_evidence=base_evidence,
            )
        return self._assess_reserved(
            subject,
            base_exposure=base_exposure,
            base_evidence=base_evidence,
            assessment_item_id=subject.item_id,
        )

    def _assess_reserved(
        self,
        subject: PermissionSubject,
        *,
        base_exposure: str,
        base_evidence: dict[str, object],
        assessment_item_id: str | None,
    ) -> PermissionAssessmentResult:
        if subject.item_id is None:
            return self._failure_result(
                subject,
                GraphProtocolError(status_code=None, code="permission_subject_missing_item_id"),
                base_exposure=base_exposure,
                base_evidence=base_evidence,
            )
        url = (
            f"drives/{quote(subject.drive_id, safe='')}/items/{quote(subject.item_id, safe='')}"
            f"/permissions?$select={PERMISSION_SELECT}"
        )
        emitted_entries: list[dict[str, object]] = []
        entry_hashes: list[str] = []
        entries_observed = 0
        entries_omitted = 0
        anonymous_exposure = False
        organization_exposure = False
        semantic_complete = True
        permissions_observed = 0
        pages_observed = 0
        unknown_permissions = 0
        collection_error: GraphAPIError | GraphProtocolError | None = None
        try:
            for page in self.client.iter_pages(url, attempt_budget=self):
                pages_observed += 1
                values = page.get("value")
                if not isinstance(values, list):
                    raise GraphProtocolError(status_code=None, code="permission_page_missing_values")
                for raw in values:
                    permissions_observed += 1
                    entries, permission_complete = _normalize_permission(
                        raw,
                        tenant_id=self.tenant_id,
                        subject=subject,
                    )
                    if not permission_complete:
                        semantic_complete = False
                        unknown_permissions += 1
                    for entry in entries:
                        ordinal = entries_observed
                        entries_observed += 1
                        details = entry.get("provider_details")
                        if isinstance(details, dict) and entry.get("effect") == "allow":
                            if details.get("link_scope") == "anonymous":
                                anonymous_exposure = True
                            elif details.get("link_scope") == "organization":
                                organization_exposure = True
                        if not self._reserve_entry():
                            entries_omitted += 1
                            entry_hashes.clear()
                            continue
                        if entries_omitted == 0:
                            entry_hashes.append(str(entry["evidence_hash"]))
                        emitted_entries.append(
                            {
                                "type": "permission_entry",
                                "run_id": self.run_id,
                                "assessment_key": _assessment_key(self.tenant_id, subject),
                                "provider": "sharepoint",
                                "permission_surface": PERMISSION_SURFACE,
                                "semantics": PERMISSION_SEMANTICS,
                                "ordinal": ordinal,
                                **entry,
                            }
                        )
        except (GraphAPIError, GraphProtocolError) as exc:
            collection_error = exc

        if anonymous_exposure:
            exposure, positive_evidence = "ANONYMOUS", ["anonymous_link"]
        elif organization_exposure:
            exposure, positive_evidence = "BROAD_INTERNAL", ["organization_link"]
        else:
            exposure, positive_evidence = None, []
        final_exposure = exposure or base_exposure
        if unknown_permissions:
            self._record_unknown(unknown_permissions)
        error_code: str | None = None
        if collection_error is not None:
            failure_reason, _retrieval_hint, error_code = self._error_state(collection_error)
            state = "partial" if pages_observed else "failed"
            retrieval_coverage = "partial" if pages_observed else "not_attempted"
            self._trip_circuit(failure_reason)
            self._report_error_once(error_code, collection_error, subject)
            complete = False
        else:
            complete = semantic_complete and entries_omitted == 0
            state = "complete" if complete else "partial"
            retrieval_coverage = "complete"
        if not permissions_observed and collection_error is not None:
            semantic_coverage = "not_assessed"
            principal_resolution = "not_assessed"
        else:
            semantic_coverage = "complete" if semantic_complete else "partial_unknown_semantics"
            if collection_error is not None and semantic_coverage == "complete":
                semantic_coverage = "complete_for_observed_subset"
            principal_resolution = PRINCIPAL_RESOLUTION
        limitations = list(_BASE_LIMITATIONS)
        if not semantic_complete:
            limitations.append("unknown_provider_semantics")
        if entries_omitted:
            limitations.append("entry_budget_truncated")
        if collection_error is not None:
            limitations.extend((f"permission_{failure_reason}", "incomplete_permission_pagination"))
        assessment_key = _assessment_key(self.tenant_id, subject)
        entry_set_hash = (
            _canonical_hash(sorted(entry_hashes)) if collection_error is None and entries_omitted == 0 else None
        )
        summary: dict[str, object] = {
            "assessment_key": assessment_key,
            "assessment_state": state,
            "selection_scope": self.mode,
            "selection_coverage": "exhaustive_for_declared_scope",
            "retrieval_coverage": retrieval_coverage,
            "provider_visibility": "caller_dependent_unverified",
            "semantic_coverage": semantic_coverage,
            "principal_resolution": principal_resolution,
            "effective_access_status": "not_computed",
            "negative_conclusion_supported": False,
            "permissions_observed": permissions_observed,
            "entries_observed": entries_observed,
            "entries_emitted": len(emitted_entries),
            "entries_omitted": entries_omitted,
            "unknown_entries": unknown_permissions,
            "entry_set_hash": entry_set_hash,
            "exposure": final_exposure,
            "positive_evidence": positive_evidence,
            "limitations": limitations,
        }
        if error_code is not None:
            summary["error_code"] = error_code
            summary["failure_reason"] = failure_reason
        assessment_record: dict[str, object] = {
            "type": "permission_assessment",
            "run_id": self.run_id,
            "assessment_key": assessment_key,
            "subject_key": _subject_key(self.tenant_id, subject),
            "provider": "sharepoint",
            "permission_surface": PERMISSION_SURFACE,
            "semantics": PERMISSION_SEMANTICS,
            "method": PERMISSION_METHOD,
            "endpoint_key": subject.endpoint_key,
            "resource_name": subject.resource_name,
            "provider_resource_id": subject.drive_id,
            "subject_kind": subject.subject_kind,
            "assessment_state": state,
            "selection_scope": self.mode,
            "selection_coverage": "exhaustive_for_declared_scope",
            "retrieval_coverage": retrieval_coverage,
            "provider_visibility": "caller_dependent_unverified",
            "semantic_coverage": semantic_coverage,
            "principal_resolution": principal_resolution,
            "effective_access_status": "not_computed",
            "negative_conclusion_supported": False,
            "entries_observed": entries_observed,
            "entries_emitted": len(emitted_entries),
            "entries_omitted": entries_omitted,
            "unknown_entries": unknown_permissions,
            "entry_set_hash": entry_set_hash,
            "observed_at": datetime.now(tz=UTC).isoformat(),
            "limitations": limitations,
            "permission_summary": summary,
        }
        if error_code is not None:
            assessment_record["error_code"] = error_code
            assessment_record["provider_details"] = {"failure_reason": failure_reason}
        if assessment_item_id is not None:
            assessment_record["provider_item_id"] = assessment_item_id
        if subject.subject_path:
            assessment_record["subject_path"] = subject.subject_path
        evidence: dict[str, object] = {
            "basis": "graph_permission_evidence" if exposure else base_evidence.get("basis"),
            "classification_scope": "positive_exposure_evidence_only",
            "permission_summary": summary,
        }
        if base_evidence.get("assessed_identity"):
            evidence["assessed_identity"] = base_evidence["assessed_identity"]
        self._finish(complete=complete, exposure=final_exposure)
        return PermissionAssessmentResult(
            assessment_record,
            tuple(emitted_entries),
            summary,
            final_exposure,
            evidence,
            state,
            complete,
        )

    def _failure_result(
        self,
        subject: PermissionSubject,
        exc: BaseException,
        *,
        base_exposure: str,
        base_evidence: dict[str, object],
    ) -> PermissionAssessmentResult:
        failure_reason, _retrieval_hint, error_code = self._error_state(exc)
        state = "failed"
        retrieval_coverage = "not_attempted"
        self._trip_circuit(failure_reason)
        self._report_error_once(error_code, exc, subject)
        assessment_key = _assessment_key(self.tenant_id, subject)
        limitations = list(_BASE_LIMITATIONS) + [f"permission_{failure_reason}"]
        summary: dict[str, object] = {
            "assessment_key": assessment_key,
            "assessment_state": state,
            "selection_scope": self.mode,
            "selection_coverage": "exhaustive_for_declared_scope",
            "retrieval_coverage": retrieval_coverage,
            "provider_visibility": "caller_dependent_unverified",
            "semantic_coverage": "not_assessed",
            "principal_resolution": "not_assessed",
            "effective_access_status": "not_computed",
            "negative_conclusion_supported": False,
            "entries_observed": 0,
            "entries_emitted": 0,
            "entries_omitted": 0,
            "unknown_entries": 0,
            "entry_set_hash": None,
            "exposure": base_exposure,
            "positive_evidence": [],
            "limitations": limitations,
            "error_code": error_code,
            "failure_reason": failure_reason,
        }
        assessment_record: dict[str, object] = {
            "type": "permission_assessment",
            "run_id": self.run_id,
            "assessment_key": assessment_key,
            "subject_key": _subject_key(self.tenant_id, subject),
            "provider": "sharepoint",
            "permission_surface": PERMISSION_SURFACE,
            "semantics": PERMISSION_SEMANTICS,
            "method": PERMISSION_METHOD,
            "endpoint_key": subject.endpoint_key,
            "resource_name": subject.resource_name,
            "provider_resource_id": subject.drive_id,
            "subject_kind": subject.subject_kind,
            "assessment_state": state,
            "selection_scope": self.mode,
            "selection_coverage": "exhaustive_for_declared_scope",
            "retrieval_coverage": retrieval_coverage,
            "provider_visibility": "caller_dependent_unverified",
            "semantic_coverage": "not_assessed",
            "principal_resolution": "not_assessed",
            "effective_access_status": "not_computed",
            "negative_conclusion_supported": False,
            "entries_observed": 0,
            "entries_emitted": 0,
            "entries_omitted": 0,
            "unknown_entries": 0,
            "entry_set_hash": None,
            "observed_at": datetime.now(tz=UTC).isoformat(),
            "limitations": limitations,
            "error_code": error_code,
            "provider_details": {"failure_reason": failure_reason},
            "permission_summary": summary,
        }
        if subject.item_id is not None and subject.subject_kind == "item":
            assessment_record["provider_item_id"] = subject.item_id
        if subject.subject_path:
            assessment_record["subject_path"] = subject.subject_path
        evidence = dict(base_evidence)
        evidence["classification_scope"] = "positive_exposure_evidence_only"
        evidence["permission_summary"] = summary
        self._finish(complete=False, exposure=base_exposure)
        return PermissionAssessmentResult(
            assessment_record,
            (),
            summary,
            base_exposure,
            evidence,
            state,
            False,
        )

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            counters = dict(self._counters)
            circuit_reason = self._circuit_reason
            selection_partial_reasons = set(self._selection_partial_reasons)
        if counters["skipped_objects"] and circuit_reason == "budget_exhausted":
            request_coverage = "budget_exhausted"
        elif counters["selection_incomplete_scopes"]:
            request_coverage = "partial"
        elif counters["candidate_objects"] == 0:
            request_coverage = "complete"
        elif (
            counters["failed_objects"]
            or counters["skipped_objects"]
            or counters["entries_omitted"]
            or counters["selection_incomplete_scopes"]
        ):
            request_coverage = "partial"
        elif counters["completed_objects"] == counters["candidate_objects"]:
            request_coverage = "complete"
        else:
            request_coverage = "partial"
        return {
            "contract_version": 1,
            "requested": True,
            "mode": self.mode,
            "permission_surface": PERMISSION_SURFACE,
            "semantics": PERMISSION_SEMANTICS,
            "classification_policy": "positive_evidence_only_v1",
            "response_scope": "effective_sharing_permissions",
            "provider_visibility": "caller_dependent_unverified",
            "request_coverage": request_coverage,
            **counters,
            "circuit_reason": circuit_reason,
            "budgets": {
                "max_objects": self.max_objects,
                "max_http_attempts": self.max_http_attempts,
                "max_entries": self.max_entries,
                "concurrency": self.concurrency,
            },
            "partial_reasons": sorted(
                {
                    reason
                    for reason, present in (
                        ("object_or_http_budget_exhausted", circuit_reason == "budget_exhausted"),
                        ("authentication_failed", circuit_reason == "authentication_failed"),
                        ("temporarily_unreachable", circuit_reason == "temporarily_unreachable"),
                        ("object_assessment_failed", counters["failed_objects"] > 0),
                        ("entry_budget_truncated", counters["entries_omitted"] > 0),
                        ("unknown_provider_semantics", counters["unknown_entries"] > 0),
                    )
                    if present
                }
                | {f"selection_{reason}" for reason in selection_partial_reasons}
            ),
        }


def not_requested_permission_summary() -> dict[str, object]:
    return {
        "contract_version": 1,
        "requested": False,
        "mode": "none",
        "permission_surface": PERMISSION_SURFACE,
        "semantics": PERMISSION_SEMANTICS,
        "classification_policy": "positive_evidence_only_v1",
        "response_scope": "not_requested",
        "provider_visibility": "not_assessed",
        "request_coverage": "not_requested",
        "candidate_objects": 0,
        "attempted_objects": 0,
        "completed_objects": 0,
        "failed_objects": 0,
        "skipped_objects": 0,
        "http_attempts": 0,
        "entries_observed": 0,
        "entries_emitted": 0,
        "entries_omitted": 0,
        "unknown_entries": 0,
        "anonymous_objects": 0,
        "broad_internal_objects": 0,
        "selection_incomplete_scopes": 0,
        "circuit_reason": None,
        "partial_reasons": [],
    }
