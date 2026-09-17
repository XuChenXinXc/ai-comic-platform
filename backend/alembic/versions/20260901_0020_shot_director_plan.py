"""shot 新增 director_plan_json

Revision ID: 20260901_0020
Revises: 20260901_0019
Create Date: 2026-09-01

导演设计层（DirectorPlan）的存储列。导演计划由文字模型按分镜事实 + 相邻镜关系生成，
只决定「如何呈现」，不修改剧情/连续性/资产事实；为空对象表示尚未生成。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260901_0020_shot_director_plan"
down_revision: str | None = "20260901_0019_audio_config_voice_mapping"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return False
    return any(item["name"] == column for item in inspector.get_columns(table))


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "shot" not in inspector.get_table_names():
        return
    if _has_column("shot", "director_plan_json"):
        return
    op.add_column(
        "shot",
        sa.Column(
            "director_plan_json",
            sa.Text(),
            nullable=False,
            server_default="{}",
        ),
    )


def downgrade() -> None:
    if not _has_column("shot", "director_plan_json"):
        return
    # SQLite 3.35+ 原生 DROP COLUMN：不走 batch 重建表，避免外键 ON 时
    # 隐式 DELETE 级联清空 shot_audio 等子表行（batch_alter_table 会重建表）。
    if op.get_bind().dialect.name == "sqlite":
        op.execute("ALTER TABLE shot DROP COLUMN director_plan_json")
        return
    with op.batch_alter_table("shot") as batch_op:
        batch_op.drop_column("director_plan_json")
