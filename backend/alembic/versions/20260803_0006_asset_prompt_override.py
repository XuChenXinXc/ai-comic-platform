"""素材图提示词覆盖值

Revision ID: 20260803_0006
Revises: 20260731_0005
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260803_0006"
down_revision: str | None = "20260731_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """保存用户编辑后的素材图最终提示词。"""
    inspector = sa.inspect(op.get_bind())
    if "asset" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("asset")}
    if "prompt_override" not in columns:
        op.add_column(
            "asset",
            sa.Column("prompt_override", sa.Text(), server_default="", nullable=False),
        )


def downgrade() -> None:
    """移除素材图提示词覆盖值。"""
    inspector = sa.inspect(op.get_bind())
    if "asset" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("asset")}
    if "prompt_override" in columns:
        op.drop_column("asset", "prompt_override")
