"""audio_config 增加 voice_mapping_json 列 + shot_audio legacy 当前音频唯一索引。

Revision ID: 20260901_0019_audio_config_voice_mapping
Revises: 20260830_0018_scene_pack
Create Date: 2026-09-01

背景：
- voice_mapping 音色映射从前端 localStorage 迁移到服务端持久化，
  模型表 audio_config 已新增 voice_mapping_json 列（见 app/models/tables.py）。
- 该列此前只存在于模型定义，缺少对应迁移，导致经过 alembic 迁移的数据库
  （含测试 migrate_schema 与生产库）在整行读取 audio_config 时
  「no such column: audio_config.voice_mapping_json」崩溃。
- 本迁移补齐列定义，与 tables.py 保持一致：NOT NULL + 默认 '{}'，
  旧行自动获得空映射。
- 补建 ux_shot_audio_current_legacy 子集唯一索引：0017 引入的
  (shot_id, dialogue_line_id) 部分唯一索引对 NULL 互不相同，
  legacy 单镜音频（dialogue_line_id IS NULL）不再受数据库约束；
  本索引恢复「每镜最多一条 legacy 当前音频」的兜底约束，与
  ux_asset_image_current_legacy（0018）同模式。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260901_0019_audio_config_voice_mapping"
down_revision: str | None = "20260830_0018_scene_pack"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_AUDIO_CONFIG_TABLE = "audio_config"
_COLUMN = "voice_mapping_json"
_SHOT_AUDIO_TABLE = "shot_audio"
_LEGACY_AUDIO_INDEX = "ux_shot_audio_current_legacy"


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return False
    return any(item["name"] == column for item in inspector.get_columns(table))


def _has_index(table: str, index: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return False
    return any(item["name"] == index for item in inspector.get_indexes(table))


def upgrade() -> None:
    # 加列（幂等：列已存在则跳过）。
    if not _has_column(_AUDIO_CONFIG_TABLE, _COLUMN):
        dialect = op.get_bind().dialect.name
        column = sa.Column(
            _COLUMN,
            sa.Text(),
            nullable=False,
            server_default="{}",
        )
        if dialect == "sqlite":
            with op.batch_alter_table(_AUDIO_CONFIG_TABLE) as batch_op:
                batch_op.add_column(column)
        else:
            op.add_column(_AUDIO_CONFIG_TABLE, column)
    # legacy 当前音频唯一索引（幂等：已存在则跳过）。
    if not _has_index(_SHOT_AUDIO_TABLE, _LEGACY_AUDIO_INDEX):
        op.create_index(
            _LEGACY_AUDIO_INDEX,
            _SHOT_AUDIO_TABLE,
            ["shot_id"],
            unique=True,
            sqlite_where=sa.text("is_current = 1 AND dialogue_line_id IS NULL"),
            postgresql_where=sa.text("is_current = 1 AND dialogue_line_id IS NULL"),
        )


def downgrade() -> None:
    if _has_index(_SHOT_AUDIO_TABLE, _LEGACY_AUDIO_INDEX):
        op.drop_index(_LEGACY_AUDIO_INDEX, table_name=_SHOT_AUDIO_TABLE)
    if not _has_column(_AUDIO_CONFIG_TABLE, _COLUMN):
        return
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        with op.batch_alter_table(_AUDIO_CONFIG_TABLE) as batch_op:
            batch_op.drop_column(_COLUMN)
    else:
        op.drop_column(_AUDIO_CONFIG_TABLE, _COLUMN)
