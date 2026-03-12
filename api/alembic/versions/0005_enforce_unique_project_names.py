"""enforce unique project names

Revision ID: 0005_enforce_unique_project_names
Revises: 0004_add_saved_investigations
Create Date: 2026-03-12
"""

from alembic import op
import sqlalchemy as sa

revision = "0005_enforce_unique_project_names"
down_revision = "0004_add_saved_investigations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    duplicates = connection.execute(
        sa.text(
            """
            select lower(name) as normalized_name, array_agg(name order by name) as names
            from projects
            group by lower(name)
            having count(*) > 1
            order by lower(name)
            limit 5
            """
        )
    ).mappings().all()
    if duplicates:
        samples = "; ".join(f"{row['normalized_name']}: {', '.join(row['names'])}" for row in duplicates)
        raise RuntimeError(
            "Cannot enforce unique project names while duplicates exist. "
            f"Rename duplicate projects first: {samples}"
        )
    op.create_index("uq_projects_name_ci", "projects", [sa.text("lower(name)")], unique=True)


def downgrade() -> None:
    op.drop_index("uq_projects_name_ci", table_name="projects")
