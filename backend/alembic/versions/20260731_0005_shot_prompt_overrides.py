"""分镜最终提示词覆盖值

Revision ID: 20260731_0005
Revises: 20260730_0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260731_0005"
down_revision: str | None = "20260730_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """保存用户编辑后的最终关键帧和视频提示词。"""
    inspector = sa.inspect(op.get_bind())
    if "shot" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("shot")}
    if "image_prompt_override" not in columns:
        op.add_column(
            "shot",
            sa.Column("image_prompt_override", sa.Text(), server_default="", nullable=False),
        )
    if "video_prompt_override" not in columns:
        op.add_column(
            "shot",
            sa.Column("video_prompt_override", sa.Text(), server_default="", nullable=False),
        )


def downgrade() -> None:
    """移除分镜最终提示词覆盖值。"""
    inspector = sa.inspect(op.get_bind())
    if "shot" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("shot")}
    if "video_prompt_override" in columns:
        op.drop_column("shot", "video_prompt_override")
    if "image_prompt_override" in columns:
        op.drop_column("shot", "image_prompt_override")
