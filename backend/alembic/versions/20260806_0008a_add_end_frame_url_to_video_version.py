"""add_end_frame_url_to_video_version

Revision ID: 20260806_0008a
Revises: 20260806_0008
Create Date: 2026-08-06 16:46:04.702149
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = '20260806_0008a'
down_revision: str | None = '20260806_0008'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """应用迁移。"""
    # 检查表是否存在再添加列
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if 'video_version' in inspector.get_table_names():
        op.add_column('video_version', sa.Column('end_frame_url', sa.Text(), nullable=True))


def downgrade() -> None:
    """回滚迁移。"""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if 'video_version' in inspector.get_table_names():
        op.drop_column('video_version', 'end_frame_url')
