"""add normalized permission evidence and materialized run comparisons

Revision ID: 0012_permission_evidence
Revises: 0011_sharepoint_indexes
Create Date: 2026-08-27
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012_permission_evidence"
down_revision = "0011_sharepoint_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    jsonb = postgresql.JSONB(astext_type=sa.Text())
    empty_object = sa.text("'{}'::jsonb")
    empty_list = sa.text("'[]'::jsonb")

    # Keys are deliberately nullable. Existing enterprise inventories can be
    # very large, so comparisons prepare keys set-wise for only the selected
    # runs instead of turning the migration into an unbounded backfill.
    op.add_column("resources", sa.Column("identity_key", sa.String(length=64), nullable=True))
    op.add_column(
        "resources",
        sa.Column("permission_summary", jsonb, server_default=empty_object, nullable=False),
    )
    op.add_column("items", sa.Column("identity_key", sa.String(length=64), nullable=True))
    op.add_column(
        "items",
        sa.Column("permission_summary", jsonb, server_default=empty_object, nullable=False),
    )

    op.create_table(
        "permission_assessments",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("resource_id", sa.BigInteger(), nullable=False),
        sa.Column("item_id", sa.BigInteger(), nullable=True),
        sa.Column("assessment_key", sa.String(length=64), nullable=False),
        sa.Column("subject_kind", sa.String(length=32), nullable=False),
        sa.Column("subject_key", sa.String(length=64), nullable=False),
        sa.Column("subject_provider_id", sa.String(length=512), nullable=True),
        sa.Column("subject_path", sa.Text(), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("semantics", sa.String(length=80), nullable=False),
        sa.Column("permission_surface", sa.String(length=80), nullable=False),
        sa.Column("method", sa.String(length=80), nullable=False),
        sa.Column("assessment_state", sa.String(length=40), nullable=False),
        sa.Column("selection_scope", sa.String(length=64), nullable=False),
        sa.Column("selection_coverage", sa.String(length=64), nullable=False),
        sa.Column("retrieval_coverage", sa.String(length=64), nullable=False),
        sa.Column("provider_visibility", sa.String(length=64), nullable=False),
        sa.Column("semantic_coverage", sa.String(length=64), nullable=False),
        sa.Column("principal_resolution", sa.String(length=64), nullable=False),
        sa.Column("effective_access_status", sa.String(length=40), nullable=False),
        sa.Column("negative_conclusion_supported", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("entries_observed", sa.Integer(), server_default="0", nullable=False),
        sa.Column("entries_emitted", sa.Integer(), server_default="0", nullable=False),
        sa.Column("entries_omitted", sa.Integer(), server_default="0", nullable=False),
        sa.Column("unknown_entries", sa.Integer(), server_default="0", nullable=False),
        sa.Column("evidence_hash", sa.String(length=64), nullable=True),
        sa.Column("entry_set_hash", sa.String(length=64), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("limitations", jsonb, server_default=empty_list, nullable=False),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("errors", jsonb, server_default=empty_list, nullable=False),
        sa.Column("provider_details", jsonb, server_default=empty_object, nullable=False),
        sa.Column("summary", jsonb, server_default=empty_object, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "entries_observed >= 0 AND entries_emitted >= 0 AND entries_omitted >= 0 AND unknown_entries >= 0",
            name="ck_permission_assessments_nonnegative_counts",
        ),
        sa.ForeignKeyConstraint(["item_id"], ["items.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["resource_id"], ["resources.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["scan_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "assessment_key", name="uq_permission_assessments_run_key"),
        sa.UniqueConstraint(
            "run_id",
            "resource_id",
            "subject_key",
            "semantics",
            "permission_surface",
            name="uq_permission_assessments_subject_surface",
        ),
    )
    op.create_index(
        "ix_permission_assessments_run_resource_id",
        "permission_assessments",
        ["run_id", "resource_id", "id"],
    )
    op.create_index(
        "ix_permission_assessments_run_item_id",
        "permission_assessments",
        ["run_id", "item_id", "id"],
    )
    op.create_index(
        "ix_permission_assessments_run_state_id",
        "permission_assessments",
        ["run_id", "assessment_state", "id"],
    )

    op.create_table(
        "permission_principals",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("principal_key", sa.String(length=64), nullable=False),
        sa.Column("identifier_namespace", sa.String(length=80), nullable=False),
        sa.Column("authority", sa.String(length=512), nullable=False),
        sa.Column("native_id", sa.String(length=512), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("display_name", sa.String(length=1024), nullable=True),
        sa.Column("login_name", sa.String(length=1024), nullable=True),
        sa.Column("email", sa.String(length=1024), nullable=True),
        sa.Column("resolution_state", sa.String(length=40), nullable=False),
        sa.Column("resolution_source", sa.String(length=80), nullable=True),
        sa.Column("aliases", jsonb, server_default=empty_list, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["scan_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "provider", "principal_key", name="uq_permission_principals_run_key"),
    )
    op.create_index(
        "ix_permission_principals_run_kind_id",
        "permission_principals",
        ["run_id", "kind", "id"],
    )
    op.create_index(
        "ix_permission_principals_run_native_id",
        "permission_principals",
        ["run_id", "identifier_namespace", "native_id"],
    )

    op.create_table(
        "permission_entries",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("assessment_id", sa.BigInteger(), nullable=False),
        sa.Column("principal_id", sa.BigInteger(), nullable=True),
        sa.Column("entry_key", sa.String(length=64), nullable=False),
        sa.Column("provider_entry_id", sa.String(length=512), nullable=True),
        sa.Column("ordinal", sa.Integer(), nullable=True),
        sa.Column("entry_kind", sa.String(length=64), nullable=False),
        sa.Column("entry_effect", sa.String(length=40), nullable=False),
        sa.Column("normalized_rights", jsonb, server_default=empty_list, nullable=False),
        sa.Column("inherited_state", sa.String(length=40), nullable=False),
        sa.Column("expiration_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("provider_details", jsonb, server_default=empty_object, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("ordinal IS NULL OR ordinal >= 0", name="ck_permission_entries_ordinal"),
        sa.ForeignKeyConstraint(["assessment_id"], ["permission_assessments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["principal_id"], ["permission_principals.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["run_id"], ["scan_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("assessment_id", "entry_key", name="uq_permission_entries_assessment_key"),
    )
    op.create_index(
        "ix_permission_entries_run_assessment_id",
        "permission_entries",
        ["run_id", "assessment_id", "id"],
    )
    op.create_index(
        "ix_permission_entries_run_principal_id",
        "permission_entries",
        ["run_id", "principal_id", "id"],
    )
    op.create_index(
        "ix_permission_entries_assessment_effect_id",
        "permission_entries",
        ["assessment_id", "entry_effect", "id"],
    )

    op.create_table(
        "run_comparisons",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("baseline_run_id", sa.Uuid(), nullable=False),
        sa.Column("current_run_id", sa.Uuid(), nullable=False),
        sa.Column("algorithm_version", sa.String(length=64), nullable=False),
        sa.Column("options_hash", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=24), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("compatibility", jsonb, server_default=empty_object, nullable=False),
        sa.Column("progress", jsonb, server_default=empty_object, nullable=False),
        sa.Column("summary", jsonb, server_default=empty_object, nullable=False),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_by_token_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("baseline_run_id <> current_run_id", name="ck_run_comparisons_distinct_runs"),
        sa.CheckConstraint("attempt_count >= 0", name="ck_run_comparisons_attempt_count"),
        sa.ForeignKeyConstraint(["baseline_run_id"], ["scan_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_token_id"], ["api_tokens.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["current_run_id"], ["scan_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id",
            "baseline_run_id",
            "current_run_id",
            "algorithm_version",
            "options_hash",
            name="uq_run_comparisons_identity",
        ),
    )
    op.create_index(
        "ix_run_comparisons_project_created_id",
        "run_comparisons",
        ["project_id", "created_at", "id"],
    )
    op.create_index(
        "ix_run_comparisons_state_heartbeat",
        "run_comparisons",
        ["state", "heartbeat_at"],
    )

    op.create_table(
        "comparison_resource_changes",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("comparison_id", sa.Uuid(), nullable=False),
        sa.Column("identity_key", sa.String(length=64), nullable=False),
        sa.Column("change_type", sa.String(length=24), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=False),
        sa.Column("provider_resource_id", sa.String(length=512), nullable=True),
        sa.Column("match_basis", sa.String(length=40), nullable=False),
        sa.Column("match_quality", sa.String(length=24), nullable=False),
        sa.Column("before_resource_id", sa.BigInteger(), nullable=True),
        sa.Column("after_resource_id", sa.BigInteger(), nullable=True),
        sa.Column("endpoint_key_before", sa.String(length=255), nullable=True),
        sa.Column("endpoint_key_after", sa.String(length=255), nullable=True),
        sa.Column("resource_name_before", sa.String(length=255), nullable=True),
        sa.Column("resource_name_after", sa.String(length=255), nullable=True),
        sa.Column("change_categories", jsonb, server_default=empty_list, nullable=False),
        sa.Column("structural_state", sa.String(length=32), nullable=False),
        sa.Column("access_state", sa.String(length=32), nullable=False),
        sa.Column("content_state", sa.String(length=32), nullable=False),
        sa.Column("access_interpretation", sa.Text(), nullable=False),
        sa.Column("item_count_before", sa.BigInteger(), nullable=True),
        sa.Column("item_count_after", sa.BigInteger(), nullable=True),
        sa.Column("before_snapshot", jsonb, server_default=empty_object, nullable=False),
        sa.Column("after_snapshot", jsonb, server_default=empty_object, nullable=False),
        sa.Column("search_text", sa.Text(), server_default="", nullable=False),
        sa.Column("impact_rank", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(["after_resource_id"], ["resources.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["before_resource_id"], ["resources.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["comparison_id"], ["run_comparisons.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("comparison_id", "identity_key", name="uq_comparison_resource_changes_identity"),
    )
    op.create_index(
        "ix_comparison_changes_type_impact_id",
        "comparison_resource_changes",
        ["comparison_id", "change_type", "impact_rank", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_comparison_changes_type_impact_id", table_name="comparison_resource_changes")
    op.drop_table("comparison_resource_changes")
    op.drop_index("ix_run_comparisons_state_heartbeat", table_name="run_comparisons")
    op.drop_index("ix_run_comparisons_project_created_id", table_name="run_comparisons")
    op.drop_table("run_comparisons")
    op.drop_index("ix_permission_entries_assessment_effect_id", table_name="permission_entries")
    op.drop_index("ix_permission_entries_run_principal_id", table_name="permission_entries")
    op.drop_index("ix_permission_entries_run_assessment_id", table_name="permission_entries")
    op.drop_table("permission_entries")
    op.drop_index("ix_permission_principals_run_native_id", table_name="permission_principals")
    op.drop_index("ix_permission_principals_run_kind_id", table_name="permission_principals")
    op.drop_table("permission_principals")
    op.drop_index("ix_permission_assessments_run_state_id", table_name="permission_assessments")
    op.drop_index("ix_permission_assessments_run_item_id", table_name="permission_assessments")
    op.drop_index("ix_permission_assessments_run_resource_id", table_name="permission_assessments")
    op.drop_table("permission_assessments")
    op.drop_column("items", "permission_summary")
    op.drop_column("items", "identity_key")
    op.drop_column("resources", "permission_summary")
    op.drop_column("resources", "identity_key")
