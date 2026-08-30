from __future__ import annotations

import math
from collections.abc import Iterable

import redis
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services import storage


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(values: dict[str, str]) -> str:
    return "{" + ",".join(f'{key}="{_escape(value)}"' for key, value in values.items()) + "}"


def _number(value: object) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if math.isfinite(parsed) else 0.0


def _family(name: str, help_text: str, values: Iterable[tuple[dict[str, str], object]]) -> list[str]:
    lines = [f"# HELP {name} {help_text}", f"# TYPE {name} gauge"]
    for labels, value in values:
        lines.append(f"{name}{_labels(labels)} {_number(value):g}")
    return lines


def _database_metrics(db: Session) -> list[str]:
    run_rows = db.execute(text("SELECT status::text, COUNT(*) FROM scan_runs GROUP BY status")).all()
    comparison_rows = db.execute(text("SELECT state::text, COUNT(*) FROM run_comparisons GROUP BY state")).all()
    oldest_run_age = db.execute(
        text(
            """
            SELECT COALESCE(EXTRACT(EPOCH FROM (NOW() - MIN(created_at))), 0)
            FROM scan_runs
            WHERE status::text IN ('UPLOADED', 'INGESTING')
            """
        )
    ).scalar()
    oldest_comparison_age = db.execute(
        text(
            """
            SELECT COALESCE(EXTRACT(EPOCH FROM (NOW() - MIN(created_at))), 0)
            FROM run_comparisons
            WHERE state::text IN ('queued', 'running')
            """
        )
    ).scalar()
    source_rows = db.execute(
        text(
            """
            SELECT
                CASE
                    WHEN NOT enabled THEN 'disabled'
                    WHEN last_success_at IS NULL THEN 'never_collected'
                    WHEN expected_interval_seconds IS NOT NULL
                         AND EXTRACT(EPOCH FROM (NOW() - last_success_at))
                             > GREATEST(900, expected_interval_seconds * 2) THEN 'stale'
                    WHEN last_failure_at IS NOT NULL AND last_failure_at > last_success_at
                        THEN 'degraded'
                    WHEN LOWER(COALESCE(coverage ->> 'state', 'unknown')) <> 'complete'
                        THEN 'degraded'
                    ELSE 'healthy'
                END AS health,
                COUNT(*)
            FROM collection_sources
            GROUP BY health
            """
        )
    ).all()
    finding_rows = db.execute(
        text(
            """
            SELECT status, severity, COUNT(*)
            FROM findings
            GROUP BY status, severity
            """
        )
    ).all()
    expired_accepted_risk = db.execute(
        text(
            """
            SELECT COUNT(*)
            FROM findings
            WHERE status = 'accepted_risk'
              AND accepted_risk_expires_at <= NOW()
            """
        )
    ).scalar()
    lines = _family(
        "share_sentinel_runs",
        "Persisted scan runs by bounded state.",
        (({"state": str(state).lower()}, count) for state, count in run_rows),
    )
    lines.extend(
        _family(
            "share_sentinel_comparisons",
            "Persisted materialized comparisons by bounded state.",
            (({"state": str(state).lower()}, count) for state, count in comparison_rows),
        )
    )
    lines.extend(
        _family(
            "share_sentinel_oldest_active_job_age_seconds",
            "Age of the oldest durable active job by job type.",
            [
                ({"job_type": "ingest"}, oldest_run_age),
                ({"job_type": "comparison"}, oldest_comparison_age),
            ],
        )
    )
    lines.extend(
        _family(
            "share_sentinel_collection_sources",
            "Configured collection sources by derived health state.",
            (({"health": str(health)}, count) for health, count in source_rows),
        )
    )
    lines.extend(
        _family(
            "share_sentinel_findings",
            "Actionable findings by workflow status and severity.",
            (({"status": str(status), "severity": str(severity)}, count) for status, severity, count in finding_rows),
        )
    )
    lines.extend(
        _family(
            "share_sentinel_expired_accepted_risk_findings",
            "Accepted-risk findings whose review deadline has elapsed.",
            [({}, expired_accepted_risk)],
        )
    )
    return lines


def _redis_metrics(redis_client) -> list[str]:
    stream_name = "ingest_jobs"
    groups = redis_client.xinfo_groups(stream_name)
    pending = sum(max(0, int(group.get("pending", 0) or 0)) for group in groups)
    lag = sum(max(0, int(group.get("lag", 0) or 0)) for group in groups)
    return _family(
        "share_sentinel_worker_stream_messages",
        "Redis worker stream state across configured consumer groups.",
        [
            ({"state": "retained"}, redis_client.xlen(stream_name)),
            ({"state": "pending"}, pending),
            ({"state": "lag"}, lag),
        ],
    )


def _storage_metrics() -> list[str]:
    status = storage.artifact_storage_status()
    lines = _family(
        "share_sentinel_artifact_storage_bytes",
        "Artifact filesystem capacity by state.",
        [
            ({"state": "total"}, status.get("total_bytes", 0)),
            ({"state": "used"}, status.get("used_bytes", 0)),
            ({"state": "free"}, status.get("free_bytes", 0)),
        ],
    )
    lines.extend(
        _family(
            "share_sentinel_artifact_storage_free_percent",
            "Percentage of artifact filesystem capacity currently free.",
            [({}, status.get("free_percent", 0))],
        )
    )
    lines.extend(
        _family(
            "share_sentinel_artifact_storage_ready",
            "Whether artifact storage is accessible and above configured headroom.",
            [({}, 1 if status.get("ok") else 0)],
        )
    )
    return lines


def render_operational_metrics(db: Session, redis_client) -> str:
    lines: list[str] = []
    collectors = (
        ("database", lambda: _database_metrics(db), Exception),
        ("redis", lambda: _redis_metrics(redis_client), redis.RedisError),
        ("artifact_storage", _storage_metrics, Exception),
    )
    collection_state: list[tuple[dict[str, str], int]] = []
    for component, collect, expected_error in collectors:
        try:
            lines.extend(collect())
        except expected_error:
            collection_state.append(({"component": component}, 0))
        else:
            collection_state.append(({"component": component}, 1))
    lines.extend(
        _family(
            "share_sentinel_operational_metrics_collection_success",
            "Whether each operational metric source was collected successfully.",
            collection_state,
        )
    )
    return "\n".join(lines) + "\n"
