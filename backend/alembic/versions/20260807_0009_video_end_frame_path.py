"""video_version.end_frame_url 改名为 end_frame_path

Revision ID: 20260807_0009
Revises: 7384338f6bea
Create Date: 2026-08-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260807_0009"
down_revision: str | None = "7384338f6bea"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return False
    return any(item["name"] == column for item in inspector.get_columns(table))


def upgrade() -> None:
    """应用迁移：该列存的是项目内相对路径而非 URL，改名与 file_path 约定保持一致。"""
    if not _has_column("video_version", "end_frame_url"):
        return
    with op.batch_alter_table("video_version") as batch_op:
        batch_op.alter_column("end_frame_url", new_column_name="end_frame_path")
    # 旧值写入的是 /media/... URL，消费侧无法当相对路径解析（该功能此前从未生效），
    # 统一清空；后续视频生成会重新提取并写入相对路径。
    op.get_bind().execute(sa.text("UPDATE video_version SET end_frame_path = NULL"))


def downgrade() -> None:
    """回滚迁移。"""
    if not _has_column("video_version", "end_frame_path"):
        return
    with op.batch_alter_table("video_version") as batch_op:
        batch_op.alter_column("end_frame_path", new_column_name="end_frame_url")
