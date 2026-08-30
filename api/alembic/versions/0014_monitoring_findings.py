"""add monitored sources, findings, and item-level comparison history

Revision ID: 0014_monitoring_findings
Revises: 0013_comparison_indexes
Create Date: 2026-08-30

The new indexed tables are empty at migration time. Existing scan and
comparison rows receive nullable foreign keys so this migration does not run
an unbounded data backfill while upgrading a large installation.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0014_monitoring_findings"
down_revision = "0013_comparison_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    jsonb = postgresql.JSONB(astext_type=sa.Text())
    empty_object = sa.text("'{}'::jsonb")
    empty_list = sa.text("'[]'::jsonb")

    op.create_table(
        "collection_sources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("source_key", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("assessed_identity", sa.String(length=512), nullable=True),
        sa.Column("target_scope", jsonb, server_default=empty_object, nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("expected_interval_seconds", sa.Integer(), nullable=True),
        sa.Column("last_run_id", sa.Uuid(), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_comparison_id", sa.Uuid(), nullable=True),
        sa.Column("collector_version", sa.String(length=64), nullable=True),
        sa.Column("coverage", jsonb, server_default=empty_object, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "expected_interval_seconds IS NULL OR "
            "expected_interval_seconds BETWEEN 300 AND 31536000",
            name="ck_collection_sources_expected_interval",
        ),
        sa.ForeignKeyConstraint(["last_comparison_id"], ["run_comparisons.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["last_run_id"], ["scan_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "source_key", name="uq_collection_sources_project_key"),
    )
    op.create_index(
        "ix_collection_sources_project_updated_id",
        "collection_sources",
        ["project_id", sa.text("updated_at DESC"), sa.text("id DESC")],
    )
    op.create_index(
        "ix_collection_sources_project_provider_id",
        "collection_sources",
        ["project_id", "provider", "id"],
    )

    op.add_column("scan_runs", sa.Column("source_id", sa.Uuid(), nullable=True))
    op.execute(
        "ALTER TABLE scan_runs ADD CONSTRAINT fk_scan_runs_source_id "
        "FOREIGN KEY (source_id) REFERENCES collection_sources(id) ON DELETE SET NULL NOT VALID"
    )

    op.add_column("run_comparisons", sa.Column("source_id", sa.Uuid(), nullable=True))
    op.add_column(
        "run_comparisons",
        sa.Column("trigger", sa.String(length=24), server_default=sa.text("'manual'"), nullable=False),
    )
    op.execute(
        "ALTER TABLE run_comparisons ADD CONSTRAINT fk_run_comparisons_source_id "
        "FOREIGN KEY (source_id) REFERENCES collection_sources(id) ON DELETE SET NULL NOT VALID"
    )
    op.execute(
        "ALTER TABLE run_comparisons ADD CONSTRAINT ck_run_comparisons_trigger "
        "CHECK (trigger IN ('manual', 'automatic')) NOT VALID"
    )

    op.create_table(
        "comparison_item_changes",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("comparison_id", sa.Uuid(), nullable=False),
        sa.Column("resource_change_id", sa.BigInteger(), nullable=False),
        sa.Column("identity_key", sa.String(length=64), nullable=False),
        sa.Column("change_type", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("before_item_id", sa.BigInteger(), nullable=True),
        sa.Column("after_item_id", sa.BigInteger(), nullable=True),
        sa.Column("match_basis", sa.String(length=40), nullable=False),
        sa.Column("match_quality", sa.String(length=24), nullable=False),
        sa.Column("change_categories", jsonb, server_default=empty_list, nullable=False),
        sa.Column("evidence_state", sa.String(length=24), nullable=False),
        sa.Column("limitations", jsonb, server_default=empty_list, nullable=False),
        sa.Column("before_snapshot", jsonb, server_default=empty_object, nullable=False),
        sa.Column("after_snapshot", jsonb, server_default=empty_object, nullable=False),
        sa.Column("search_text", sa.Text(), server_default="", nullable=False),
        sa.Column("impact_rank", sa.Integer(), server_default="0", nullable=False),
        sa.CheckConstraint(
            "change_type IN ('added','removed','moved','renamed','metadata_changed','permission_changed','indeterminate')",
            name="ck_comparison_item_changes_type",
        ),
        sa.CheckConstraint(
            "evidence_state IN ('exact','bounded','indeterminate')",
            name="ck_comparison_item_changes_evidence_state",
        ),
        sa.ForeignKeyConstraint(["after_item_id"], ["items.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["before_item_id"], ["items.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["comparison_id"], ["run_comparisons.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["resource_change_id"], ["comparison_resource_changes.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "comparison_id",
            "resource_change_id",
            "identity_key",
            name="uq_comparison_item_changes_identity",
        ),
    )
    op.create_index(
        "ix_comparison_item_changes_impact_id",
        "comparison_item_changes",
        ["comparison_id", sa.text("impact_rank DESC"), sa.text("id DESC")],
    )
    op.create_index(
        "ix_comparison_item_changes_resource_id",
        "comparison_item_changes",
        ["comparison_id", "resource_change_id", "id"],
    )
    op.create_index(
        "ix_comparison_item_changes_type_id",
        "comparison_item_changes",
        ["comparison_id", "change_type", "id"],
    )
    op.create_index(
        "ix_comparison_item_changes_search_trgm",
        "comparison_item_changes",
        ["search_text"],
        postgresql_using="gin",
        postgresql_ops={"search_text": "gin_trgm_ops"},
    )

    op.create_table(
        "findings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=True),
        sa.Column("policy_id", sa.String(length=120), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("dedupe_key", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=24), server_default=sa.text("'open'"), nullable=False),
        sa.Column("resource_identity_key", sa.String(length=64), nullable=True),
        sa.Column("resource_type", sa.String(length=64), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=True),
        sa.Column("resource_name", sa.String(length=255), nullable=True),
        sa.Column("search_text", sa.Text(), server_default="", nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_risk_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("assignee_user_id", sa.Uuid(), nullable=True),
        sa.Column("latest_run_id", sa.Uuid(), nullable=True),
        sa.Column("latest_comparison_id", sa.Uuid(), nullable=True),
        sa.Column("evidence", jsonb, server_default=empty_object, nullable=False),
        sa.Column("occurrence_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "severity IN ('critical','high','medium','low','info')",
            name="ck_findings_severity",
        ),
        sa.CheckConstraint(
            "status IN ('open','acknowledged','accepted_risk','resolved')",
            name="ck_findings_status",
        ),
        sa.CheckConstraint("occurrence_count >= 1 AND revision >= 1", name="ck_findings_counts"),
        sa.CheckConstraint("policy_version >= 1", name="ck_findings_policy_version"),
        sa.CheckConstraint(
            "(status = 'accepted_risk') = (accepted_risk_expires_at IS NOT NULL)",
            name="ck_findings_accepted_risk_expiry",
        ),
        sa.ForeignKeyConstraint(["assignee_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["latest_comparison_id"], ["run_comparisons.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["latest_run_id"], ["scan_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_id"], ["collection_sources.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "dedupe_key", name="uq_findings_project_dedupe"),
    )
    op.create_index(
        "ix_findings_project_updated_id",
        "findings",
        ["project_id", sa.text("updated_at DESC"), sa.text("id DESC")],
    )
    op.create_index(
        "ix_findings_project_status_severity_id",
        "findings",
        ["project_id", "status", "severity", "id"],
    )
    op.create_index(
        "ix_findings_project_status_updated_id",
        "findings",
        ["project_id", "status", sa.text("updated_at DESC"), sa.text("id DESC")],
    )
    op.create_index(
        "ix_findings_source_policy_status_id",
        "findings",
        ["source_id", "policy_id", "status", "id"],
    )
    op.create_index(
        "ix_findings_search_trgm",
        "findings",
        ["search_text"],
        postgresql_using="gin",
        postgresql_ops={"search_text": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_findings_status_risk_expiry_id",
        "findings",
        ["status", "accepted_risk_expires_at", "id"],
    )

    op.create_table(
        "finding_occurrences",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("finding_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("comparison_id", sa.Uuid(), nullable=True),
        sa.Column("occurrence_key", sa.String(length=64), nullable=False),
        sa.Column("policy_id", sa.String(length=120), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("evidence_state", sa.String(length=24), nullable=False),
        sa.Column("evidence", jsonb, server_default=empty_object, nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "evidence_state IN ('exact','bounded','indeterminate')",
            name="ck_finding_occurrences_evidence_state",
        ),
        sa.ForeignKeyConstraint(["comparison_id"], ["run_comparisons.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["finding_id"], ["findings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["scan_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("finding_id", "occurrence_key", name="uq_finding_occurrences_finding_key"),
    )
    op.create_index(
        "ix_finding_occurrences_finding_observed_id",
        "finding_occurrences",
        ["finding_id", sa.text("observed_at DESC"), sa.text("id DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_finding_occurrences_finding_observed_id", table_name="finding_occurrences")
    op.drop_table("finding_occurrences")
    op.drop_index("ix_findings_status_risk_expiry_id", table_name="findings")
    op.drop_index("ix_findings_search_trgm", table_name="findings")
    op.drop_index("ix_findings_source_policy_status_id", table_name="findings")
    op.drop_index("ix_findings_project_status_updated_id", table_name="findings")
    op.drop_index("ix_findings_project_status_severity_id", table_name="findings")
    op.drop_index("ix_findings_project_updated_id", table_name="findings")
    op.drop_table("findings")
    op.drop_index("ix_comparison_item_changes_search_trgm", table_name="comparison_item_changes")
    op.drop_index("ix_comparison_item_changes_type_id", table_name="comparison_item_changes")
    op.drop_index("ix_comparison_item_changes_resource_id", table_name="comparison_item_changes")
    op.drop_index("ix_comparison_item_changes_impact_id", table_name="comparison_item_changes")
    op.drop_table("comparison_item_changes")
    op.drop_constraint("ck_run_comparisons_trigger", "run_comparisons", type_="check")
    op.drop_constraint("fk_run_comparisons_source_id", "run_comparisons", type_="foreignkey")
    op.drop_column("run_comparisons", "trigger")
    op.drop_column("run_comparisons", "source_id")
    op.drop_constraint("fk_scan_runs_source_id", "scan_runs", type_="foreignkey")
    op.drop_column("scan_runs", "source_id")
    op.drop_index("ix_collection_sources_project_provider_id", table_name="collection_sources")
    op.drop_index("ix_collection_sources_project_updated_id", table_name="collection_sources")
    op.drop_table("collection_sources")
