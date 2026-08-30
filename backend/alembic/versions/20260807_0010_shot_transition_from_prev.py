"""shot 新增 transition_from_prev

Revision ID: 20260807_0010
Revises: 20260807_0009
Create Date: 2026-08-07

与上一镜的衔接类型由剧情动作是否连续决定，此前用「场景素材是否相同」推断，
两个方向都不准：同场景可能是对切/时间跳跃，跨场景也可能是连续动作（如推门出屋）。
改为提取分镜时显式声明。

存量分镜按用户要求不回填，统一取 server_default 的 scene_change——
即不引用上一镜末帧，是安全侧的默认。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260807_0010"
down_revision: str | None = "20260807_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return False
    return any(item["name"] == column for item in inspector.get_columns(table))


def upgrade() -> None:
    # 从 0003 等早期版本链式升级时 shot 表尚未建立，此处跳过；
    # 建表走 metadata 时列已包含在定义中
    inspector = sa.inspect(op.get_bind())
    if "shot" not in inspector.get_table_names():
        return
    if _has_column("shot", "transition_from_prev"):
        return
    op.add_column(
        "shot",
        sa.Column(
            "transition_from_prev",
            sa.Text(),
            nullable=False,
            server_default="scene_change",
        ),
    )


def downgrade() -> None:
    if not _has_column("shot", "transition_from_prev"):
        return
    with op.batch_alter_table("shot") as batch_op:
        batch_op.drop_column("transition_from_prev")
