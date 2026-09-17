"""删除 chapter 表冗余索引 idx_chapter_project_no，消除元数据与存量库漂移。

Revision ID: 20260915_0023_drop_chapter_redundant_index
Revises: 20260904_0022_remove_scene_pack
Create Date: 2026-09-15

背景：
- 0008_multi_chapter 同时建了 uq_chapter_project_no 唯一约束和同列
  （project_id, chapter_no）的 idx_chapter_project_no；唯一约束自带同前缀
  索引，后者对查询完全冗余。
- models/tables.py 后来删掉了该索引声明但没有配套迁移：alembic 建的存量库
  仍带着这个索引，与元数据不一致，autogenerate 会持续产生差异噪音。
- 本迁移把库结构对齐到 tables.py；downgrade 重建索引，保证 0008 的
  downgrade（会 drop 该索引）沿迁移链回退时不报「索引不存在」。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260915_0023_drop_chapter_redundant_index"
down_revision: str | None = "20260904_0022_remove_scene_pack"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "chapter"
_INDEX = "idx_chapter_project_no"


def _has_index(table: str, index: str) -> bool:
    """幂等守卫：索引真实存在才操作，重放或异构库结构不报错。"""
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return False
    return any(item["name"] == index for item in inspector.get_indexes(table))


def upgrade() -> None:
    if _has_index(_TABLE, _INDEX):
        op.drop_index(_INDEX, table_name=_TABLE)


def downgrade() -> None:
    if not _has_index(_TABLE, _INDEX):
        op.create_index(_INDEX, _TABLE, ["project_id", "chapter_no"])
