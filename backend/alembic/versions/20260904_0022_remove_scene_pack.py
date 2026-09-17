"""移除 Scene Pack：删除 scene_view 表、scene_view_id 列，恢复每素材一张当前图。

Revision ID: 20260904_0022_remove_scene_pack
Revises: 20260904_0021_project_visual_bible
Create Date: 2026-09-04

背景：
- 产品决策：场景不再按 MASTER + 多视角（Scene Views）拆图，每个场景素材
  只保留一张场景参考图，与角色/道具一致。
- 本迁移等价于 0018_scene_pack 的 downgrade：删除分区 current 索引、
  恢复旧的「每素材一张当前图」唯一索引、收敛 is_current、
  删除 shot/asset_image 的 scene_view_id 列、删除 scene_view 表。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260904_0022_remove_scene_pack"
down_revision: str | None = "20260904_0021_project_visual_bible"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCENE_VIEW_TABLE = "scene_view"
_ASSET_IMAGE_TABLE = "asset_image"
_SHOT_TABLE = "shot"
_LEGACY_INDEX = "ux_asset_image_current"
_VIEW_INDEX = "ux_asset_image_current_view"
_VIEW_LEGACY_INDEX = "ux_asset_image_current_legacy"


def _has_table(table: str) -> bool:
    return table in sa.inspect(op.get_bind()).get_table_names()


def _has_index(table: str, index: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return False
    return any(item["name"] == index for item in inspector.get_indexes(table))


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return False
    return any(item["name"] == column for item in inspector.get_columns(table))


def _collapse_current_images() -> None:
    """把多个 View 各自的 current 图收敛为每素材一条，保证旧唯一索引可重建。"""
    if not _has_table(_ASSET_IMAGE_TABLE):
        return
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT id, asset_id, version_no, created_at FROM asset_image "
            "WHERE is_current = 1 "
            "ORDER BY asset_id, version_no DESC, created_at DESC, id DESC"
        )
    ).mappings().all()
    kept_assets: set[str] = set()
    for row in rows:
        asset_id = str(row.get("asset_id") or "")
        image_id = str(row.get("id") or "")
        if not asset_id or not image_id:
            continue
        if asset_id not in kept_assets:
            kept_assets.add(asset_id)
            continue
        connection.execute(
            sa.text("UPDATE asset_image SET is_current = 0 WHERE id = :image_id"),
            {"image_id": image_id},
        )


def _drop_scene_view_columns() -> None:
    """从 shot / asset_image 移除 scene_view_id（SQLite 直接 DROP COLUMN）。"""
    if not _has_table(_SHOT_TABLE) and not _has_table(_ASSET_IMAGE_TABLE):
        return
    if _has_column(_SHOT_TABLE, "scene_view_id"):
        op.drop_column(_SHOT_TABLE, "scene_view_id")
    if _has_column(_ASSET_IMAGE_TABLE, "scene_view_id"):
        op.drop_column(_ASSET_IMAGE_TABLE, "scene_view_id")


def upgrade() -> None:
    # 幂等守卫：scene_view 表不存在且旧索引已恢复 ⇒ 已跑过。
    if not _has_table(_ASSET_IMAGE_TABLE):
        return
    if _has_index(_ASSET_IMAGE_TABLE, _VIEW_INDEX):
        op.drop_index(_VIEW_INDEX, table_name=_ASSET_IMAGE_TABLE)
    if _has_index(_ASSET_IMAGE_TABLE, _VIEW_LEGACY_INDEX):
        op.drop_index(_VIEW_LEGACY_INDEX, table_name=_ASSET_IMAGE_TABLE)
    if _has_index(_ASSET_IMAGE_TABLE, _LEGACY_INDEX):
        op.drop_index(_LEGACY_INDEX, table_name=_ASSET_IMAGE_TABLE)
    _collapse_current_images()
    predicate = sa.text("is_current = 1")
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.create_index(
            _LEGACY_INDEX,
            _ASSET_IMAGE_TABLE,
            ["asset_id"],
            unique=True,
            sqlite_where=predicate,
        )
    elif dialect == "postgresql":
        op.create_index(
            _LEGACY_INDEX,
            _ASSET_IMAGE_TABLE,
            ["asset_id"],
            unique=True,
            postgresql_where=predicate,
        )
    _drop_scene_view_columns()
    if _has_table(_SCENE_VIEW_TABLE):
        op.drop_table(_SCENE_VIEW_TABLE)


def downgrade() -> None:
    # 回滚 Scene Pack 移除：重新执行 0018 的升级逻辑（建表、加列、重建分区索引）。
    # 0018 的升级函数自带幂等守卫，重复执行安全。
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "scene_pack_0018",
        Path(__file__).with_name("20260830_0018_scene_pack.py"),
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载 20260830_0018_scene_pack 迁移模块")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.upgrade()
