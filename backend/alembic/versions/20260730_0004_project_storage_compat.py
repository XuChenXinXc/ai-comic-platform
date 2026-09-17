"""平台项目存储设置与旧版 0003 兼容

Revision ID: 20260730_0004
Revises: 20260730_0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.config import get_settings

revision: str = "20260730_0004"
down_revision: str | None = "20260730_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """新增平台设置，并为旧项目保留原有目录布局。"""
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("project")}
    if "storage_name" not in columns:
        op.add_column(
            "project",
            sa.Column("storage_name", sa.Text(), server_default="", nullable=False),
        )
    if "storage_root" not in columns:
        op.add_column(
            "project",
            sa.Column("storage_root", sa.Text(), server_default="", nullable=False),
        )
    if "storage_layout" not in columns:
        op.add_column(
            "project",
            sa.Column(
                "storage_layout",
                sa.Text(),
                server_default="prefixed",
                nullable=False,
            ),
        )
    if "platform_setting" not in inspector.get_table_names():
        op.create_table(
            "platform_setting",
            sa.Column("key", sa.Text(), nullable=False),
            sa.Column("value", sa.Text(), nullable=False),
            sa.Column("updated_at", sa.Text(), nullable=False),
            sa.PrimaryKeyConstraint("key", name=op.f("pk_platform_setting")),
        )
    project_table = sa.table(
        "project",
        sa.column("name", sa.Text()),
        sa.column("storage_name", sa.Text()),
        sa.column("storage_root", sa.Text()),
        sa.column("storage_layout", sa.Text()),
    )
    op.execute(
        project_table.update()
        .where(project_table.c.storage_name == "")
        .values(storage_name=project_table.c.name)
    )
    op.execute(
        project_table.update()
        .where(project_table.c.storage_root == "")
        .values(
            storage_root=str(get_settings().projects_dir.resolve()),
            storage_layout="prefixed",
        )
    )


def downgrade() -> None:
    """移除平台设置和 0004 新增的项目存储字段。"""
    inspector = sa.inspect(op.get_bind())
    if "platform_setting" in inspector.get_table_names():
        op.drop_table("platform_setting")
    columns = {column["name"] for column in inspector.get_columns("project")}
    if "storage_layout" in columns:
        op.drop_column("project", "storage_layout")
    if "storage_root" in columns:
        op.drop_column("project", "storage_root")
