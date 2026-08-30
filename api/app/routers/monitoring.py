from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import String, and_, cast, func, or_, select
from sqlalchemy.orm import Session, aliased

from app.config import get_settings
from app.db import escape_like, get_db
from app.deps import AuthContext, get_auth_context, request_meta, require_project_role, require_token_scopes
from app.enums import ProjectRole
from app.models import (
    AuditEvent,
    CollectionSource,
    Finding,
    FindingOccurrence,
    ProjectMember,
    RunComparison,
    ScanRun,
    User,
)
from app.pagination import (
    KeysetColumn,
    apply_keyset_pagination,
    paginate_rows,
    parse_datetime_cursor_value,
    parse_int_cursor_value,
    parse_uuid_cursor_value,
)
from app.rate_limit import RateLimiter
from app.schemas import CollectionSourceUpdateIn, FindingBulkUpdateIn, FindingUpdateIn
from app.services.audit import write_audit_event
from app.services.monitoring import (
    BUILT_IN_FINDING_POLICIES,
    FINDING_SEVERITIES,
    FINDING_STATUSES,
    POLICY_BY_ID,
    SOURCE_HEALTH_STATES,
    finding_payload,
    source_payload,
    utc_datetime,
)
from app.token_scopes import (
    SCOPE_READ_FINDINGS,
    SCOPE_READ_SOURCES,
    SCOPE_WRITE_FINDINGS,
    SCOPE_WRITE_SOURCES,
)

router = APIRouter(prefix="/projects/{project_id}", tags=["monitoring"])
rate_limiter = RateLimiter()
SOURCE_CURSOR = (
    KeysetColumn("updated_at", CollectionSource.updated_at, direction="desc", parser=parse_datetime_cursor_value),
    KeysetColumn("id", CollectionSource.id, direction="desc", parser=parse_uuid_cursor_value),
)
FINDING_CURSOR = (
    KeysetColumn("updated_at", Finding.updated_at, direction="desc", parser=parse_datetime_cursor_value),
    KeysetColumn("id", Finding.id, direction="desc", parser=parse_uuid_cursor_value),
)
OCCURRENCE_CURSOR = (
    KeysetColumn(
        "observed_at",
        FindingOccurrence.observed_at,
        direction="desc",
        parser=parse_datetime_cursor_value,
    ),
    KeysetColumn("id", FindingOccurrence.id, direction="desc", parser=parse_int_cursor_value),
)
ACTIVITY_CURSOR = (
    KeysetColumn("ts", AuditEvent.ts, direction="desc", parser=parse_datetime_cursor_value),
    KeysetColumn("id", AuditEvent.id, direction="desc", parser=parse_int_cursor_value),
)
FINDING_ACTIVITY_ACTIONS = frozenset(
    {
        "FINDING_UPDATED",
        "FINDING_BULK_UPDATED",
        "FINDING_OBSERVED",
        "FINDING_AUTO_RESOLVED",
        "FINDING_ACCEPTED_RISK_EXPIRED",
    }
)


def _get_source(db: Session, project_id: uuid.UUID, source_id: uuid.UUID, *, lock: bool = False) -> CollectionSource:
    stmt = select(CollectionSource).where(
        CollectionSource.id == source_id,
        CollectionSource.project_id == project_id,
    )
    if lock:
        stmt = stmt.with_for_update()
    source = db.execute(stmt).scalar_one_or_none()
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="collection source not found")
    return source


def _get_finding(db: Session, project_id: uuid.UUID, finding_id: uuid.UUID, *, lock: bool = False) -> Finding:
    stmt = select(Finding).where(Finding.id == finding_id, Finding.project_id == project_id)
    if lock:
        stmt = stmt.with_for_update()
    finding = db.execute(stmt).scalar_one_or_none()
    if finding is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="finding not found")
    return finding


def _source_health_filter(value: str):
    now = func.now()
    stale = and_(
        CollectionSource.enabled.is_(True),
        CollectionSource.last_success_at.is_not(None),
        CollectionSource.expected_interval_seconds.is_not(None),
        func.extract("epoch", now - CollectionSource.last_success_at)
        > func.greatest(900, CollectionSource.expected_interval_seconds * 2),
    )
    failed_after_success = and_(
        CollectionSource.last_failure_at.is_not(None),
        CollectionSource.last_success_at.is_not(None),
        CollectionSource.last_failure_at > CollectionSource.last_success_at,
    )
    incomplete = or_(
        func.lower(func.coalesce(CollectionSource.coverage["state"].astext, "unknown")) != "complete",
        func.lower(
            func.coalesce(CollectionSource.coverage["monitoring_findings"]["state"].astext, "unknown")
        )
        != "complete",
        func.lower(
            func.coalesce(CollectionSource.coverage["automatic_baseline"]["state"].astext, "unknown")
        )
        != "established",
        func.lower(
            func.coalesce(
                CollectionSource.coverage["automatic_baseline"]["findings_evaluation_state"].astext,
                "complete",
            )
        )
        != "complete",
    )
    if value == "disabled":
        return CollectionSource.enabled.is_(False)
    if value == "never_collected":
        return and_(CollectionSource.enabled.is_(True), CollectionSource.last_success_at.is_(None))
    if value == "stale":
        return stale
    if value == "degraded":
        return and_(
            CollectionSource.enabled.is_(True),
            CollectionSource.last_success_at.is_not(None),
            ~stale,
            or_(failed_after_success, incomplete),
        )
    return and_(
        CollectionSource.enabled.is_(True),
        CollectionSource.last_success_at.is_not(None),
        ~stale,
        ~failed_after_success,
        ~incomplete,
    )


@router.get("/sources", response_model=dict)
def list_sources(
    project_id: uuid.UUID,
    provider: str | None = Query(default=None, max_length=32),
    health_status: str | None = Query(default=None, max_length=24),
    q: str | None = Query(default=None, max_length=512),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_SOURCES)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    stmt = select(CollectionSource).where(CollectionSource.project_id == project_id)
    if provider:
        stmt = stmt.where(CollectionSource.provider == provider.strip().lower())
    if health_status:
        normalized_health = health_status.strip().lower()
        if normalized_health not in SOURCE_HEALTH_STATES:
            raise HTTPException(status_code=400, detail="unsupported health_status")
        stmt = stmt.where(_source_health_filter(normalized_health))
    if q and q.strip():
        pattern = f"%{escape_like(q.strip())}%"
        stmt = stmt.where(
            or_(
                CollectionSource.display_name.ilike(pattern, escape="\\"),
                CollectionSource.provider.ilike(pattern, escape="\\"),
                CollectionSource.assessed_identity.ilike(pattern, escape="\\"),
                cast(CollectionSource.target_scope, String).ilike(pattern, escape="\\"),
            )
        )
    stmt = apply_keyset_pagination(stmt, SOURCE_CURSOR, cursor, limit)
    rows, next_cursor = paginate_rows(db.execute(stmt).scalars().all(), SOURCE_CURSOR, limit)
    now = datetime.now(tz=UTC)
    return {"items": [source_payload(row, now=now) for row in rows], "next_cursor": next_cursor}


@router.get("/sources/{source_id}", response_model=dict)
def get_source(
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_SOURCES)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    return source_payload(_get_source(db, project_id, source_id))


@router.patch("/sources/{source_id}", response_model=dict)
def update_source(
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    payload: CollectionSourceUpdateIn,
    request: Request,
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_WRITE_SOURCES)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.ADMIN, auth, db)
    if not payload.model_fields_set:
        raise HTTPException(status_code=400, detail="at least one source field is required")
    source = _get_source(db, project_id, source_id, lock=True)
    before = {
        "display_name": source.display_name,
        "enabled": source.enabled,
        "expected_interval_seconds": source.expected_interval_seconds,
    }
    if "display_name" in payload.model_fields_set:
        if payload.display_name is None:
            raise HTTPException(status_code=400, detail="display_name cannot be null")
        source.display_name = payload.display_name
    if "enabled" in payload.model_fields_set:
        if payload.enabled is None:
            raise HTTPException(status_code=400, detail="enabled cannot be null")
        source.enabled = payload.enabled
    if "expected_interval_seconds" in payload.model_fields_set:
        source.expected_interval_seconds = payload.expected_interval_seconds
    source.updated_at = datetime.now(tz=UTC)
    write_audit_event(
        db,
        action="COLLECTION_SOURCE_UPDATED",
        object_type="collection_source",
        object_id=str(source.id),
        actor_user_id=auth.user_id,
        actor_token_id=auth.token_id,
        project_id=project_id,
        metadata={
            **request_meta(request),
            "before": before,
            "after": {
                "display_name": source.display_name,
                "enabled": source.enabled,
                "expected_interval_seconds": source.expected_interval_seconds,
            },
        },
    )
    db.commit()
    db.refresh(source)
    return source_payload(source)


@router.post("/runs/{run_id}/monitoring/retry", response_model=dict, status_code=status.HTTP_202_ACCEPTED)
def retry_run_monitoring_evaluation(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_WRITE_FINDINGS)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.OPERATOR, auth, db)
    settings = get_settings()
    try:
        rate_limiter.check(
            request,
            "monitoring_evaluation_retry",
            limit=settings.api_comparison_rate_limit,
            window_seconds=settings.api_comparison_rate_window_seconds,
            actor_key=f"{auth.user_id or auth.token_id}:{project_id}",
        )
    except HTTPException as exc:
        if exc.status_code == status.HTTP_429_TOO_MANY_REQUESTS:
            exc.detail = {
                "code": "MONITORING_RETRY_RATE_LIMITED",
                "message": "Too many monitoring retries were requested; wait before retrying again.",
            }
        raise
    run = db.execute(
        select(ScanRun)
        .where(ScanRun.id == run_id, ScanRun.project_id == project_id)
        .with_for_update()
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    run_status = run.status.value if hasattr(run.status, "value") else str(run.status)
    progress = dict(run.ingest_progress or {})
    findings_progress = (
        dict(progress.get("monitoring_findings"))
        if isinstance(progress.get("monitoring_findings"), dict)
        else {}
    )
    current_state = str(findings_progress.get("state") or "unknown")
    if run_status != "COMPLETE" or run.source_id is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "MONITORING_RUN_NOT_RETRYABLE",
                "message": "Only a monitored COMPLETE run can retry finding evaluation.",
            },
        )
    source = db.get(CollectionSource, run.source_id)
    if source is None or not source.enabled:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "MONITORING_SOURCE_DISABLED",
                "message": "Enable the collection source before retrying finding evaluation.",
            },
        )
    newer_run = aliased(ScanRun)
    superseding_run_id = db.execute(
        select(newer_run.id)
        .where(
            newer_run.source_id == run.source_id,
            newer_run.status == "COMPLETE",
            or_(
                newer_run.created_at > run.created_at,
                and_(newer_run.created_at == run.created_at, newer_run.id > run.id),
            ),
        )
        .limit(1)
    ).scalar_one_or_none()
    if superseding_run_id is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "MONITORING_RUN_SUPERSEDED",
                "message": "A newer successful snapshot supersedes this run; retry its evaluation instead.",
                "superseding_run_id": str(superseding_run_id),
            },
        )
    if current_state in {"queued", "retrying", "evaluating"}:
        return {"run_id": str(run.id), "state": current_state, "monitoring_findings": findings_progress}
    if current_state != "degraded":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "MONITORING_EVALUATION_NOT_RETRYABLE",
                "message": "Finding evaluation is not in a degraded terminal state.",
            },
        )
    queued = {
        **findings_progress,
        "state": "queued",
        "phase": "candidates" if findings_progress.get("phase") == "failed" else findings_progress.get("phase"),
        "attempt_count": 0,
        "next_retry_at": None,
        "operator_retry_requested_at": datetime.now(tz=UTC).isoformat(),
    }
    progress["monitoring_findings"] = queued
    run.ingest_progress = progress
    coverage = dict(source.coverage or {})
    coverage["monitoring_findings"] = {
        "state": "queued",
        "phase": queued.get("phase"),
        "attempt_count": 0,
        "next_retry_at": None,
        "error_code": queued.get("error_code"),
        "run_id": str(run.id),
        "retryable": False,
    }
    coverage["state"] = "partial"
    reasons = [str(reason) for reason in coverage.get("reasons", []) if str(reason).strip()]
    reasons.append("Security finding evaluation is pending or retrying.")
    coverage["reasons"] = list(dict.fromkeys(reasons))
    source.coverage = coverage
    source.updated_at = datetime.now(tz=UTC)
    write_audit_event(
        db,
        action="MONITORING_EVALUATION_RETRY_REQUESTED",
        object_type="scan_run",
        object_id=str(run.id),
        actor_user_id=auth.user_id,
        actor_token_id=auth.token_id,
        project_id=project_id,
        metadata={**request_meta(request), "source_id": str(run.source_id), "previous_state": current_state},
    )
    db.commit()
    return {"run_id": str(run.id), "state": "queued", "monitoring_findings": queued}


@router.post(
    "/comparisons/{comparison_id}/findings/retry",
    response_model=dict,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_comparison_finding_evaluation(
    project_id: uuid.UUID,
    comparison_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_WRITE_FINDINGS)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.OPERATOR, auth, db)
    settings = get_settings()
    try:
        rate_limiter.check(
            request,
            "comparison_findings_retry",
            limit=settings.api_comparison_rate_limit,
            window_seconds=settings.api_comparison_rate_window_seconds,
            actor_key=f"{auth.user_id or auth.token_id}:{project_id}",
        )
    except HTTPException as exc:
        if exc.status_code == status.HTTP_429_TOO_MANY_REQUESTS:
            exc.detail = {
                "code": "MONITORING_RETRY_RATE_LIMITED",
                "message": "Too many monitoring retries were requested; wait before retrying again.",
            }
        raise
    comparison = db.execute(
        select(RunComparison)
        .where(RunComparison.id == comparison_id, RunComparison.project_id == project_id)
        .with_for_update()
    ).scalar_one_or_none()
    if comparison is None:
        raise HTTPException(status_code=404, detail="comparison not found")
    summary = dict(comparison.summary or {})
    evaluation = (
        dict(summary.get("findings_evaluation"))
        if isinstance(summary.get("findings_evaluation"), dict)
        else {}
    )
    evaluation_state = str(evaluation.get("state") or "unknown")
    if comparison.state == "complete" and evaluation_state in {"queued", "retrying", "evaluating"}:
        return {"comparison_id": str(comparison.id), "state": comparison.state, "findings_evaluation": evaluation}
    if comparison.state != "complete" or evaluation_state != "degraded":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "COMPARISON_FINDINGS_NOT_RETRYABLE",
                "message": "Only a completed comparison with degraded finding evaluation can be retried.",
            },
        )
    progress = dict(comparison.progress or {})
    queued_evaluation = {
        **evaluation,
        "state": "queued",
        "attempt_count": 0,
        "next_retry_at": None,
    }
    summary["findings_evaluation"] = queued_evaluation
    comparison.progress = {
        **progress,
        "phase": "complete",
        "findings_attempt_count": 0,
        "findings_evaluation_state": "queued",
        "findings_retry_requested": True,
    }
    comparison.summary = summary
    write_audit_event(
        db,
        action="COMPARISON_FINDINGS_RETRY_REQUESTED",
        object_type="run_comparison",
        object_id=str(comparison.id),
        actor_user_id=auth.user_id,
        actor_token_id=auth.token_id,
        project_id=project_id,
        metadata={**request_meta(request), "previous_state": evaluation_state},
    )
    db.commit()
    return {"comparison_id": str(comparison.id), "state": "complete", "findings_evaluation": queued_evaluation}


@router.get("/finding-policies", response_model=dict)
def list_finding_policies(
    project_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_FINDINGS)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    return {"items": [dict(policy) for policy in BUILT_IN_FINDING_POLICIES]}


@router.get("/findings/assignee-candidates", response_model=dict)
def list_finding_assignee_candidates(
    project_id: uuid.UUID,
    q: str | None = Query(default=None, max_length=320),
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_FINDINGS)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.OPERATOR, auth, db)
    stmt = (
        select(User)
        .join(ProjectMember, ProjectMember.user_id == User.id)
        .where(
            ProjectMember.project_id == project_id,
            User.is_active.is_(True),
            User.is_approved.is_(True),
        )
        .order_by(User.email, User.id)
        .limit(limit)
    )
    if q and q.strip():
        stmt = stmt.where(User.email.ilike(f"%{escape_like(q.strip())}%", escape="\\"))
    rows = db.execute(stmt).scalars().all()
    return {"items": [{"id": str(user.id), "email": user.email} for user in rows]}


def _finding_filters(
    project_id: uuid.UUID,
    *,
    severity: str | None,
    policy_id: str | None,
    source_id: uuid.UUID | None,
    query_text: str | None,
) -> list:
    filters = [Finding.project_id == project_id]
    if severity:
        filters.append(Finding.severity == severity)
    if policy_id:
        filters.append(Finding.policy_id == policy_id)
    if source_id:
        filters.append(Finding.source_id == source_id)
    if query_text:
        pattern = f"%{escape_like(query_text)}%"
        filters.append(Finding.search_text.ilike(pattern, escape="\\"))
    return filters


@router.get("/findings", response_model=dict)
def list_findings(
    project_id: uuid.UUID,
    finding_status: str | None = Query(default=None, alias="status", max_length=24),
    severity: str | None = Query(default=None, max_length=16),
    policy_id: str | None = Query(default=None, max_length=120),
    source_id: uuid.UUID | None = Query(default=None),
    q: str | None = Query(default=None, max_length=512),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_FINDINGS)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    normalized_status = finding_status.strip().lower() if finding_status else None
    normalized_severity = severity.strip().lower() if severity else None
    normalized_policy = policy_id.strip() if policy_id else None
    normalized_query = q.strip() if q else None
    if normalized_status and normalized_status not in FINDING_STATUSES:
        raise HTTPException(status_code=400, detail="unsupported finding status")
    if normalized_severity and normalized_severity not in FINDING_SEVERITIES:
        raise HTTPException(status_code=400, detail="unsupported finding severity")
    if normalized_policy and normalized_policy not in POLICY_BY_ID:
        raise HTTPException(status_code=400, detail="unsupported policy_id")
    if source_id:
        _get_source(db, project_id, source_id)
    base_filters = _finding_filters(
        project_id,
        severity=normalized_severity,
        policy_id=normalized_policy,
        source_id=source_id,
        query_text=normalized_query,
    )
    stmt = select(Finding).where(*base_filters)
    if normalized_status:
        stmt = stmt.where(Finding.status == normalized_status)
    stmt = apply_keyset_pagination(stmt, FINDING_CURSOR, cursor, limit)
    rows, next_cursor = paginate_rows(db.execute(stmt).scalars().all(), FINDING_CURSOR, limit)
    counts = {key: 0 for key in FINDING_STATUSES}
    for row_status, count in db.execute(
        select(Finding.status, func.count(Finding.id)).where(*base_filters).group_by(Finding.status)
    ).all():
        if row_status in counts:
            counts[row_status] = int(count)
    return {
        "items": [finding_payload(row) for row in rows],
        "next_cursor": next_cursor,
        "summary": {**counts, "total": sum(counts.values())},
    }


@router.get("/findings/{finding_id}", response_model=dict)
def get_finding(
    project_id: uuid.UUID,
    finding_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_FINDINGS)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    finding = _get_finding(db, project_id, finding_id)
    write_audit_event(
        db,
        action="FINDING_EVIDENCE_VIEWED",
        object_type="finding",
        object_id=str(finding_id),
        actor_user_id=auth.user_id,
        actor_token_id=auth.token_id,
        project_id=project_id,
        metadata={
            **request_meta(request),
            "policy_id": finding.policy_id,
            "evidence_state": str((finding.evidence or {}).get("state") or "indeterminate"),
        },
    )
    db.commit()
    return finding_payload(finding)


def _validate_assignee(db: Session, project_id: uuid.UUID, user_id: uuid.UUID | None) -> None:
    if user_id is None:
        return
    membership = db.get(ProjectMember, {"project_id": project_id, "user_id": user_id})
    user = db.get(User, user_id)
    if membership is None or user is None or not user.is_active or not user.is_approved:
        raise HTTPException(status_code=400, detail="assignee must be an active member of this project")


def _normalize_expiry(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise HTTPException(status_code=400, detail="accepted-risk expiry must include a timezone")
    normalized = utc_datetime(value)
    if normalized is None or normalized <= datetime.now(tz=UTC):
        raise HTTPException(status_code=400, detail="accepted-risk expiry must be in the future")
    return normalized


def _apply_finding_update(
    finding: Finding,
    *,
    fields_set: set[str],
    new_status: str | None,
    assignee_user_id: uuid.UUID | None,
    accepted_risk_expires_at: datetime | None,
) -> tuple[str, uuid.UUID | None, datetime | None]:
    old_status = finding.status
    old_assignee = finding.assignee_user_id
    old_expiry = finding.accepted_risk_expires_at
    if "status" in fields_set and new_status is None:
        raise HTTPException(status_code=400, detail="status cannot be null")
    target_status = new_status if "status" in fields_set else finding.status
    if "accepted_risk_expires_at" in fields_set and target_status != "accepted_risk":
        raise HTTPException(
            status_code=400,
            detail="accepted-risk expiry can only be updated when the target status is accepted_risk",
        )
    if "assignee_user_id" in fields_set:
        finding.assignee_user_id = assignee_user_id
    if "accepted_risk_expires_at" in fields_set:
        finding.accepted_risk_expires_at = _normalize_expiry(accepted_risk_expires_at)
    if target_status == "accepted_risk":
        if finding.accepted_risk_expires_at is None:
            raise HTTPException(status_code=400, detail="accepted_risk status requires a future expiry")
        finding.accepted_risk_expires_at = _normalize_expiry(finding.accepted_risk_expires_at)
    else:
        finding.accepted_risk_expires_at = None
    finding.status = target_status
    if target_status != old_status:
        finding.resolved_at = datetime.now(tz=UTC) if target_status == "resolved" else None
    finding.updated_at = datetime.now(tz=UTC)
    finding.revision += 1
    return old_status, old_assignee, old_expiry


def _finding_update_would_change(
    finding: Finding,
    *,
    fields_set: set[str],
    new_status: str | None,
    assignee_user_id: uuid.UUID | None,
    accepted_risk_expires_at: datetime | None,
) -> bool:
    """Validate and compare a lifecycle mutation without advancing revision."""

    if "status" in fields_set and new_status is None:
        raise HTTPException(status_code=400, detail="status cannot be null")
    target_status = new_status if "status" in fields_set else finding.status
    target_assignee = assignee_user_id if "assignee_user_id" in fields_set else finding.assignee_user_id
    if "accepted_risk_expires_at" in fields_set and target_status != "accepted_risk":
        raise HTTPException(
            status_code=400,
            detail="accepted-risk expiry can only be updated when the target status is accepted_risk",
        )
    if target_status == "accepted_risk":
        target_expiry = (
            _normalize_expiry(accepted_risk_expires_at)
            if "accepted_risk_expires_at" in fields_set
            else _normalize_expiry(finding.accepted_risk_expires_at)
        )
        if target_expiry is None:
            raise HTTPException(status_code=400, detail="accepted_risk status requires a future expiry")
    else:
        target_expiry = None
    return (
        target_status != finding.status
        or target_assignee != finding.assignee_user_id
        or utc_datetime(target_expiry) != utc_datetime(finding.accepted_risk_expires_at)
    )


@router.patch("/findings/{finding_id}", response_model=dict)
def update_finding(
    project_id: uuid.UUID,
    finding_id: uuid.UUID,
    payload: FindingUpdateIn,
    request: Request,
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_WRITE_FINDINGS)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.OPERATOR, auth, db)
    mutable_fields = payload.model_fields_set - {"revision"}
    if not mutable_fields:
        raise HTTPException(status_code=400, detail="at least one finding update is required")
    if mutable_fields == {"note"} and not (payload.note and payload.note.strip()):
        raise HTTPException(status_code=400, detail="note-only update requires a non-blank note")
    _validate_assignee(db, project_id, payload.assignee_user_id if "assignee_user_id" in mutable_fields else None)
    finding = _get_finding(db, project_id, finding_id, lock=True)
    if finding.revision != payload.revision:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "FINDING_REVISION_CONFLICT",
                "message": "The finding changed after it was loaded; refresh and retry.",
                "current_revision": finding.revision,
            },
        )
    has_note = bool(payload.note and payload.note.strip())
    if not _finding_update_would_change(
        finding,
        fields_set=mutable_fields,
        new_status=payload.status,
        assignee_user_id=payload.assignee_user_id,
        accepted_risk_expires_at=payload.accepted_risk_expires_at,
    ) and not has_note:
        raise HTTPException(status_code=400, detail="finding update would not change state or add a note")
    old_status, old_assignee, old_expiry = _apply_finding_update(
        finding,
        fields_set=mutable_fields,
        new_status=payload.status,
        assignee_user_id=payload.assignee_user_id,
        accepted_risk_expires_at=payload.accepted_risk_expires_at,
    )
    write_audit_event(
        db,
        action="FINDING_UPDATED",
        object_type="finding",
        object_id=str(finding.id),
        actor_user_id=auth.user_id,
        actor_token_id=auth.token_id,
        project_id=project_id,
        metadata={
            **request_meta(request),
            "old_status": old_status,
            "new_status": finding.status,
            "old_assignee_user_id": str(old_assignee) if old_assignee else None,
            "new_assignee_user_id": str(finding.assignee_user_id) if finding.assignee_user_id else None,
            "old_accepted_risk_expires_at": old_expiry.isoformat() if old_expiry else None,
            "new_accepted_risk_expires_at": (
                finding.accepted_risk_expires_at.isoformat() if finding.accepted_risk_expires_at else None
            ),
            "note": payload.note.strip() if payload.note and payload.note.strip() else None,
            "revision": finding.revision,
        },
    )
    db.commit()
    db.refresh(finding)
    return finding_payload(finding)


@router.post("/findings/bulk", response_model=dict)
def bulk_update_findings(
    project_id: uuid.UUID,
    payload: FindingBulkUpdateIn,
    request: Request,
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_WRITE_FINDINGS)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.OPERATOR, auth, db)
    mutable_fields = payload.model_fields_set - {"finding_ids", "expected_revisions"}
    if not mutable_fields:
        raise HTTPException(status_code=400, detail="at least one bulk finding update is required")
    if mutable_fields == {"note"} and not (payload.note and payload.note.strip()):
        raise HTTPException(status_code=400, detail="note-only update requires a non-blank note")
    _validate_assignee(db, project_id, payload.assignee_user_id if "assignee_user_id" in mutable_fields else None)
    rows = list(
        db.execute(
            select(Finding)
            .where(Finding.project_id == project_id, Finding.id.in_(payload.finding_ids))
            .order_by(Finding.id)
            .with_for_update()
        ).scalars()
    )
    if len(rows) != len(payload.finding_ids):
        raise HTTPException(status_code=404, detail="one or more findings were not found in this project")
    conflicts = [
        {"finding_id": str(row.id), "current_revision": row.revision}
        for row in rows
        if payload.expected_revisions.get(row.id) != row.revision
    ]
    if conflicts:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "FINDING_BULK_REVISION_CONFLICT",
                "message": "One or more findings changed after they were loaded; refresh and retry.",
                "conflicts": conflicts,
            },
        )
    has_note = bool(payload.note and payload.note.strip())
    state_changes = {
        row.id: _finding_update_would_change(
            row,
            fields_set=mutable_fields,
            new_status=payload.status,
            assignee_user_id=payload.assignee_user_id,
            accepted_risk_expires_at=payload.accepted_risk_expires_at,
        )
        for row in rows
    }
    if not any(state_changes.values()) and not has_note:
        raise HTTPException(status_code=400, detail="bulk update would not change state or add a note")
    changes: list[dict] = []
    for finding in rows:
        if not state_changes[finding.id] and not has_note:
            continue
        old_status, old_assignee, _old_expiry = _apply_finding_update(
            finding,
            fields_set=mutable_fields,
            new_status=payload.status,
            assignee_user_id=payload.assignee_user_id,
            accepted_risk_expires_at=payload.accepted_risk_expires_at,
        )
        changes.append(
            {
                "finding_id": str(finding.id),
                "old_status": old_status,
                "new_status": finding.status,
                "old_assignee_user_id": str(old_assignee) if old_assignee else None,
                "new_assignee_user_id": str(finding.assignee_user_id) if finding.assignee_user_id else None,
                "revision": finding.revision,
            }
        )
    batch_action_id = str(uuid.uuid4())
    for change in changes:
        write_audit_event(
            db,
            action="FINDING_BULK_UPDATED",
            object_type="finding",
            object_id=change["finding_id"],
            actor_user_id=auth.user_id,
            actor_token_id=auth.token_id,
            project_id=project_id,
            metadata={
                **request_meta(request),
                **change,
                "batch_action_id": batch_action_id,
                "note": payload.note.strip() if payload.note and payload.note.strip() else None,
            },
        )
    write_audit_event(
        db,
        action="FINDINGS_BULK_UPDATED",
        object_type="project",
        object_id=str(project_id),
        actor_user_id=auth.user_id,
        actor_token_id=auth.token_id,
        project_id=project_id,
        metadata={
            **request_meta(request),
            "updated_count": len(changes),
            "changes": changes,
            "batch_action_id": batch_action_id,
            "note": payload.note.strip() if payload.note and payload.note.strip() else None,
        },
    )
    db.commit()
    return {"items": [finding_payload(row) for row in rows], "updated_count": len(changes)}


@router.get("/findings/{finding_id}/occurrences", response_model=dict)
def list_finding_occurrences(
    project_id: uuid.UUID,
    finding_id: uuid.UUID,
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_FINDINGS)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    _get_finding(db, project_id, finding_id)
    stmt = select(FindingOccurrence).where(FindingOccurrence.finding_id == finding_id)
    stmt = apply_keyset_pagination(stmt, OCCURRENCE_CURSOR, cursor, limit)
    rows, next_cursor = paginate_rows(db.execute(stmt).scalars().all(), OCCURRENCE_CURSOR, limit)
    write_audit_event(
        db,
        action="FINDING_OCCURRENCES_LISTED",
        object_type="finding",
        object_id=str(finding_id),
        actor_user_id=auth.user_id,
        actor_token_id=auth.token_id,
        project_id=project_id,
        metadata={
            **request_meta(request),
            "limit": limit,
            "cursor_applied": cursor is not None,
            "result_count": len(rows),
            "has_next_page": next_cursor is not None,
        },
    )
    db.commit()
    return {
        "items": [
            {
                "id": row.id,
                "run_id": str(row.run_id) if row.run_id else None,
                "comparison_id": str(row.comparison_id) if row.comparison_id else None,
                "policy_id": row.policy_id,
                "policy_version": row.policy_version,
                "evidence_state": row.evidence_state,
                "evidence": row.evidence or {},
                "observed_at": row.observed_at.isoformat(),
            }
            for row in rows
        ],
        "next_cursor": next_cursor,
    }


@router.get("/findings/{finding_id}/activity", response_model=dict)
def list_finding_activity(
    project_id: uuid.UUID,
    finding_id: uuid.UUID,
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_FINDINGS)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    _get_finding(db, project_id, finding_id)
    stmt = select(AuditEvent).where(
        AuditEvent.project_id == project_id,
        AuditEvent.object_type == "finding",
        AuditEvent.object_id == str(finding_id),
        AuditEvent.action.in_(FINDING_ACTIVITY_ACTIONS),
    )
    stmt = apply_keyset_pagination(stmt, ACTIVITY_CURSOR, cursor, limit)
    rows, next_cursor = paginate_rows(db.execute(stmt).scalars().all(), ACTIVITY_CURSOR, limit)
    write_audit_event(
        db,
        action="FINDING_ACTIVITY_LISTED",
        object_type="finding",
        object_id=str(finding_id),
        actor_user_id=auth.user_id,
        actor_token_id=auth.token_id,
        project_id=project_id,
        metadata={
            **request_meta(request),
            "limit": limit,
            "cursor_applied": cursor is not None,
            "result_count": len(rows),
            "has_next_page": next_cursor is not None,
        },
    )
    db.commit()
    return {
        "items": [
            {
                "id": row.id,
                "ts": row.ts.isoformat(),
                "action": row.action,
                "actor_user_id": str(row.actor_user_id) if row.actor_user_id else None,
                "actor_token_id": str(row.actor_token_id) if row.actor_token_id else None,
                "metadata": row.metadata_json or {},
            }
            for row in rows
        ],
        "next_cursor": next_cursor,
    }
