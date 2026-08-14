"""add is_free_create flag to project

Revision ID: 20260814_0013
Revises: 20260808_0012
Create Date: 2026-08-14

区分常规项目与自由制作项目：
- 常规项目显示在外层左侧边栏；
- 自由制作项目只在自由制作页展示。
历史「手动分镜 / 手动 / 自由制作」项目自动迁移为自由制作项目。
"""

import sqlalchemy as sa

from alembic import op

revision: str = "20260814_0013"
down_revision: str | None = "20260808_0012"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("project")}
    if "is_free_create" not in columns:
        op.add_column(
            "project",
            sa.Column(
                "is_free_create",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )
    op.execute(
        sa.text(
            "UPDATE project SET is_free_create = 1 "
            "WHERE name IN ('手动分镜', '手动') OR name LIKE '自由制作%'"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("project") as batch_op:
        batch_op.drop_column("is_free_create")
