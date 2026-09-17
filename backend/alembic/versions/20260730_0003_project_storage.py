"""项目素材存储目录

Revision ID: 20260730_0003
Revises: 20260729_0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260730_0003"
down_revision: str | None = "20260729_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """保存稳定的项目目录名，避免重命名后改变素材路径。"""
    op.add_column(
        "project",
        sa.Column("storage_name", sa.Text(), server_default="", nullable=False),
    )
    project_table = sa.table(
        "project",
        sa.column("name", sa.Text()),
        sa.column("storage_name", sa.Text()),
    )
    op.execute(project_table.update().values(storage_name=project_table.c.name))


def downgrade() -> None:
    """移除项目目录名。"""
    op.drop_column("project", "storage_name")
