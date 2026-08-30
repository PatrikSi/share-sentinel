from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.comparison_contract import COMPARISON_ALGORITHM_VERSION, comparison_algorithm_is_current
from app.locking import lock_monitoring_source
from app.models import CollectionSource, Finding, RunComparison

BUILT_IN_FINDING_POLICIES: tuple[dict[str, Any], ...] = (
    {
        "id": "sharepoint.anonymous_access",
        "version": 1,
        "title": "Anonymous SharePoint access",
        "description": "A SharePoint resource has explicit evidence of an anonymous sharing link.",
        "severity": "critical",
        "category": "exposure",
        "enabled": True,
        "mode": "state",
        "evidence_requirements": ["explicit anonymous link scope"],
    },
    {
        "id": "sharepoint.broad_internal_access",
        "version": 1,
        "title": "Organization-wide SharePoint access",
        "description": "A SharePoint resource has explicit organization-wide sharing evidence.",
        "severity": "medium",
        "category": "exposure",
        "enabled": True,
        "mode": "state",
        "evidence_requirements": ["explicit organization link scope"],
    },
    {
        "id": "smb.write_observed",
        "version": 1,
        "title": "SMB write capability observed",
        "description": "One or more bounded, non-mutating SMB capability probes indicate write access.",
        "severity": "high",
        "category": "access",
        "enabled": True,
        "mode": "state",
        "evidence_requirements": ["allowed or mixed write capability observation", "finalized probe evidence"],
    },
    {
        "id": "resource.appeared",
        "version": 1,
        "title": "Resource appeared",
        "description": "A resource appeared between structurally comparable collection runs.",
        "severity": "info",
        "category": "change",
        "enabled": True,
        "mode": "event",
        "evidence_requirements": ["structurally comparable runs"],
    },
    {
        "id": "resource.disappeared",
        "version": 1,
        "title": "Resource disappeared",
        "description": "A resource disappeared between structurally comparable collection runs.",
        "severity": "low",
        "category": "change",
        "enabled": True,
        "mode": "event",
        "evidence_requirements": ["authoritative structurally comparable runs"],
    },
    {
        "id": "permission.evidence_changed",
        "version": 1,
        "title": "Permission evidence changed",
        "description": "Comparable direct-permission or bounded capability evidence changed.",
        "severity": "high",
        "category": "access_change",
        "enabled": True,
        "mode": "event",
        "evidence_requirements": ["comparable permission or capability evidence"],
    },
    {
        "id": "comparison.indeterminate",
        "version": 1,
        "title": "Change is indeterminate",
        "description": "Collection coverage or identity continuity does not support a definitive change conclusion.",
        "severity": "low",
        "category": "coverage",
        "enabled": True,
        "mode": "event",
        "evidence_requirements": ["materialized comparison with an indeterminate result"],
    },
)


class AutomaticSourceDisabledError(RuntimeError):
    pass

POLICY_BY_ID = {policy["id"]: policy for policy in BUILT_IN_FINDING_POLICIES}
FINDING_STATUSES = frozenset({"open", "acknowledged", "accepted_risk", "resolved"})
FINDING_SEVERITIES = frozenset({"critical", "high", "medium", "low", "info"})
SOURCE_HEALTH_STATES = frozenset({"healthy", "stale", "degraded", "never_collected", "disabled"})


def utc_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def source_health(source: CollectionSource, *, now: datetime | None = None) -> dict[str, Any]:
    current_time = utc_datetime(now) or datetime.now(tz=UTC)
    success_at = utc_datetime(source.last_success_at)
    failure_at = utc_datetime(source.last_failure_at)
    expected = source.expected_interval_seconds
    age_seconds = max(0, int((current_time - success_at).total_seconds())) if success_at else None
    stale_after = max(900, expected * 2) if expected is not None else None

    coverage = dict(source.coverage or {})
    coverage_state = str(coverage.get("state") or "unknown").lower()
    coverage_reasons = [str(reason) for reason in coverage.get("reasons", []) if str(reason).strip()]
    findings_coverage = (
        coverage.get("monitoring_findings") if isinstance(coverage.get("monitoring_findings"), dict) else {}
    )
    baseline_coverage = (
        coverage.get("automatic_baseline") if isinstance(coverage.get("automatic_baseline"), dict) else {}
    )
    findings_state = str(findings_coverage.get("state") or "unknown").lower()
    baseline_state = str(baseline_coverage.get("state") or "unknown").lower()
    baseline_findings_state = str(baseline_coverage.get("findings_evaluation_state") or "unknown").lower()
    baseline_algorithm_current = comparison_algorithm_is_current(baseline_coverage.get("algorithm_version"))
    monitoring_complete = (
        findings_state == "complete"
        and baseline_state == "established"
        and baseline_findings_state == "complete"
        and baseline_algorithm_current
    )
    reasons: list[str] = []
    if not source.enabled:
        health_status = "disabled"
        freshness_state = "disabled"
        reasons.append("Monitoring is disabled for this source.")
    elif success_at is None:
        health_status = "never_collected"
        freshness_state = "unknown"
        reasons.append("No successful collection has been registered.")
    elif stale_after is not None and age_seconds is not None and age_seconds > stale_after:
        health_status = "stale"
        freshness_state = "stale"
        reasons.append("The last successful collection is older than the configured freshness window.")
    elif failure_at is not None and failure_at > success_at:
        health_status = "degraded"
        freshness_state = "fresh" if stale_after is None or age_seconds <= stale_after else "stale"
        reasons.append("The most recent collection attempt failed after the last success.")
    elif coverage_state != "complete" or not monitoring_complete:
        health_status = "degraded"
        freshness_state = "fresh"
        reasons.extend(coverage_reasons or ["The latest collection reported partial or unknown coverage."])
        if baseline_state == "established" and not baseline_algorithm_current:
            reasons.append("Automatic comparison coverage uses an obsolete or unknown algorithm.")
    else:
        health_status = "healthy"
        freshness_state = "fresh"

    return {
        "health_status": health_status,
        "health_reasons": list(dict.fromkeys(reasons)),
        "coverage": {
            **coverage,
            "automatic_baseline": {
                **baseline_coverage,
                "algorithm_current": baseline_algorithm_current,
            },
            "state": coverage_state if coverage_state in {"complete", "partial", "unknown"} else "unknown",
            "reasons": coverage_reasons,
        },
        "freshness": {
            "state": freshness_state,
            "age_seconds": age_seconds,
            "expected_interval_seconds": expected,
            "stale_after_seconds": stale_after,
        },
    }


def source_payload(source: CollectionSource, *, now: datetime | None = None) -> dict[str, Any]:
    health = source_health(source, now=now)
    return {
        "id": str(source.id),
        "project_id": str(source.project_id),
        "source_key": source.source_key,
        "display_name": source.display_name,
        "provider": source.provider,
        "assessed_identity": source.assessed_identity,
        "target_scope": source.target_scope or {},
        "enabled": source.enabled,
        "expected_interval_seconds": source.expected_interval_seconds,
        "last_run_id": str(source.last_run_id) if source.last_run_id else None,
        "last_success_at": source.last_success_at.isoformat() if source.last_success_at else None,
        "last_failure_at": source.last_failure_at.isoformat() if source.last_failure_at else None,
        "last_comparison_id": str(source.last_comparison_id) if source.last_comparison_id else None,
        "collector_version": source.collector_version,
        **health,
        "created_at": source.created_at.isoformat() if source.created_at else None,
        "updated_at": source.updated_at.isoformat() if source.updated_at else None,
    }


def publish_automatic_baseline_recovery(
    db: Session,
    comparison: RunComparison,
    *,
    findings_only: bool,
    require_enabled: bool = False,
) -> bool:
    """Publish recovery only while this comparison is the source's current baseline."""

    if (
        comparison.trigger != "automatic"
        or comparison.source_id is None
        or not comparison_algorithm_is_current(comparison.algorithm_version)
    ):
        return False
    candidate = db.get(CollectionSource, comparison.source_id)
    if candidate is None:
        return False
    lock_monitoring_source(db, candidate.source_key)
    source = db.execute(
        select(CollectionSource)
        .where(
            CollectionSource.id == comparison.source_id,
            CollectionSource.project_id == comparison.project_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if source is None:
        return False
    if require_enabled and not source.enabled:
        raise AutomaticSourceDisabledError("automatic comparison source is disabled")
    coverage = dict(source.coverage or {})
    baseline = (
        dict(coverage.get("automatic_baseline"))
        if isinstance(coverage.get("automatic_baseline"), dict)
        else {}
    )
    if str(baseline.get("comparison_id") or "") != str(comparison.id):
        return False
    if findings_only:
        baseline.update(
            {
                "state": "established",
                "algorithm_version": COMPARISON_ALGORITHM_VERSION,
                "findings_evaluation_state": "queued",
                "findings_next_retry_at": None,
            }
        )
    else:
        baseline.update(
            {
                "state": "queued",
                "algorithm_version": COMPARISON_ALGORITHM_VERSION,
                "findings_evaluation_state": "queued",
                "findings_next_retry_at": None,
                "next_retry_at": None,
            }
        )
    baseline.pop("error_code", None)
    baseline.pop("reason", None)
    coverage["automatic_baseline"] = baseline
    coverage["state"] = "partial"
    reasons = [str(reason) for reason in coverage.get("reasons", []) if str(reason).strip()]
    reasons.append(
        "Automatic comparison finding evaluation is pending or retrying."
        if findings_only
        else "Automatic comparison recovery is queued."
    )
    coverage["reasons"] = list(dict.fromkeys(reasons))
    source.coverage = coverage
    source.updated_at = datetime.now(tz=UTC)
    return True


def finding_payload(finding: Finding, *, include_evidence: bool = True) -> dict[str, Any]:
    evidence = finding.evidence or {}
    public_evidence = evidence if include_evidence else {"state": str(evidence.get("state") or "indeterminate")}
    return {
        "id": str(finding.id),
        "project_id": str(finding.project_id),
        "source_id": str(finding.source_id) if finding.source_id else None,
        "policy_id": finding.policy_id,
        "policy_version": finding.policy_version,
        "title": finding.title,
        "description": finding.description,
        "severity": finding.severity,
        "status": finding.status,
        "resource_identity_key": finding.resource_identity_key,
        "resource_type": finding.resource_type,
        "provider": finding.provider,
        "resource_name": finding.resource_name,
        "first_seen_at": finding.first_seen_at.isoformat() if finding.first_seen_at else None,
        "last_seen_at": finding.last_seen_at.isoformat() if finding.last_seen_at else None,
        "resolved_at": finding.resolved_at.isoformat() if finding.resolved_at else None,
        "accepted_risk_expires_at": (
            finding.accepted_risk_expires_at.isoformat() if finding.accepted_risk_expires_at else None
        ),
        "assignee_user_id": str(finding.assignee_user_id) if finding.assignee_user_id else None,
        "latest_run_id": str(finding.latest_run_id) if finding.latest_run_id else None,
        "latest_comparison_id": str(finding.latest_comparison_id) if finding.latest_comparison_id else None,
        "evidence": public_evidence,
        "occurrence_count": finding.occurrence_count,
        "revision": finding.revision,
        "created_at": finding.created_at.isoformat() if finding.created_at else None,
        "updated_at": finding.updated_at.isoformat() if finding.updated_at else None,
    }
