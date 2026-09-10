"""project 新增 visual_bible_json

Revision ID: 20260904_0021
Revises: 20260901_0020
Create Date: 2026-09-04

项目级结构化视觉圣经（VisualBible）存储列。为空时由 style_prompt 自动推导，
非空时优先作为唯一画风来源。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260904_0021_project_visual_bible"
down_revision: str | None = "20260901_0020_shot_director_plan"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return False
    return any(item["name"] == column for item in inspector.get_columns(table))


def upgrade() -> None:
    if _has_column("project", "visual_bible_json"):
        return
    op.add_column("project", sa.Column("visual_bible_json", sa.Text()))


def downgrade() -> None:
    if not _has_column("project", "visual_bible_json"):
        return
    if op.get_bind().dialect.name == "sqlite":
        op.execute("ALTER TABLE project DROP COLUMN visual_bible_json")
        return
    with op.batch_alter_table("project") as batch_op:
        batch_op.drop_column("visual_bible_json")
