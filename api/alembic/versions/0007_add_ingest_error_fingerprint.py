"""add ingest error fingerprint

Revision ID: 0007_ingest_error_fp
Revises: 0006_add_user_session_version
Create Date: 2026-04-20
"""

from alembic import op
import sqlalchemy as sa

revision = "0007_ingest_error_fp"
down_revision = "0006_add_user_session_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ingest_errors", sa.Column("fingerprint", sa.String(length=32), nullable=True))
    op.execute(
        """
        UPDATE ingest_errors
        SET fingerprint = md5(
            concat_ws(
                chr(31),
                coalesce(severity::text, ''),
                coalesce(code, ''),
                coalesce(message, ''),
                coalesce(endpoint_key, ''),
                coalesce(resource_name, ''),
                coalesce(path, '')
            )
        )
        """
    )
    op.execute(
        """
        DELETE FROM ingest_errors
        WHERE id IN (
            SELECT id
            FROM (
                SELECT
                    id,
                    row_number() OVER (
                        PARTITION BY run_id, fingerprint
                        ORDER BY id
                    ) AS duplicate_rank
                FROM ingest_errors
            ) ranked
            WHERE duplicate_rank > 1
        )
        """
    )
    op.alter_column("ingest_errors", "fingerprint", existing_type=sa.String(length=32), nullable=False)
    op.create_unique_constraint("uq_ingest_errors_run_fingerprint", "ingest_errors", ["run_id", "fingerprint"])


def downgrade() -> None:
    op.drop_constraint("uq_ingest_errors_run_fingerprint", "ingest_errors", type_="unique")
    op.drop_column("ingest_errors", "fingerprint")
