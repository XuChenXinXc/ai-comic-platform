"""Scene Pack 场景资产包：scene_view 表 + asset_image.view 关联 + shot.view 绑定。

Revision ID: 20260830_0018_scene_pack
Revises: 20260818_0017_dynamic_audio_current
Create Date: 2026-08-30

背景：
- 一个物理场景（asset，asset_type='scene'）从「一张图」升级为「Scene Pack」：
  MASTER + 多个 Scene View（front/reverse/left45/right45 等）。
- scene_view 表描述每个 View 的元数据（view_key/name/prompt）。
- asset_image.scene_view_id：该图属于哪个 View；NULL 表示旧版场景图（完全兼容）。
- 旧的部分唯一索引 ux_asset_image_current（asset_id 上 is_current=1 唯一）
  升级为按 (asset_id, scene_view_id) 分区：每个 View 各有自己的当前图。
- shot.scene_view_id：分镜当前使用的 Scene View（NULL=默认，回退 MASTER/旧图）。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260830_0018_scene_pack"
down_revision: str | None = "20260818_0017_dynamic_audio_current"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCENE_VIEW_TABLE = "scene_view"
_ASSET_IMAGE_TABLE = "asset_image"
_SHOT_TABLE = "shot"
_LEGACY_INDEX = "ux_asset_image_current"
_CURRENT_INDEX = "ux_asset_image_current_view"
_LEGACY_CURRENT_INDEX = "ux_asset_image_current_legacy"


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


def _create_scene_view_table() -> None:
    if _has_table(_SCENE_VIEW_TABLE):
        return
    op.create_table(
        _SCENE_VIEW_TABLE,
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "asset_id",
            sa.Text(),
            sa.ForeignKey("asset.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("view_key", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False, server_default=""),
        sa.Column("prompt", sa.Text(), nullable=False, server_default=""),
        sa.Column("prompt_override", sa.Text(), nullable=False, server_default=""),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.Text(), nullable=False, server_default="draft"),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint("view_key <> ''", name="view_key_not_empty"),
        sa.CheckConstraint(
            "status IN ('draft','ready','confirmed','failed')", name="status"
        ),
        sa.UniqueConstraint("asset_id", "view_key", name="uq_scene_view_asset_key"),
    )
    op.create_index("idx_scene_view_asset", _SCENE_VIEW_TABLE, ["asset_id"])


def _add_asset_image_scene_view_id() -> None:
    """asset_image 增加 scene_view_id 列（幂等）。

    SQLite 下重建带子表 FK 的 asset_image 会触发外键级联删除
    （参考 0015 先例 _copy_column_without_foreign_keys 的说明），因此
    加列时剥离 FK 定义，只加纯列；scene_view 与 asset_image 的引用关系
    由应用层维护（scene_view_id 只存 id，删除视图时服务层显式置空），
    不依赖数据库级 FK。
    """
    if not _has_table(_ASSET_IMAGE_TABLE) or _has_column(_ASSET_IMAGE_TABLE, "scene_view_id"):
        return
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        with op.batch_alter_table(_ASSET_IMAGE_TABLE) as batch_op:
            batch_op.add_column(sa.Column("scene_view_id", sa.Text(), nullable=True))
    else:
        op.add_column(
            _ASSET_IMAGE_TABLE,
            sa.Column(
                "scene_view_id",
                sa.Text(),
                sa.ForeignKey("scene_view.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )


def _add_shot_scene_view_id() -> None:
    """shot 增加 scene_view_id 列（幂等）。

    与 asset_image 同理：SQLite 下重建 shot 表会级联删除 shot_audio /
    shot_subtitle / keyframe_version / video_version / shot_audio 等子表数据，
    因此只加纯列，FK 由应用层维护（scene_view_id 存 id，删除视图时服务层
    显式置空分镜引用）。
    """
    if not _has_table(_SHOT_TABLE) or _has_column(_SHOT_TABLE, "scene_view_id"):
        return
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        with op.batch_alter_table(_SHOT_TABLE) as batch_op:
            batch_op.add_column(sa.Column("scene_view_id", sa.Text(), nullable=True))
    else:
        op.add_column(
            _SHOT_TABLE,
            sa.Column(
                "scene_view_id",
                sa.Text(),
                sa.ForeignKey("scene_view.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )


def _replace_current_index() -> None:
    """把 ux_asset_image_current 替换为按 (asset_id, scene_view_id) 分区的索引，
    并补一个 scene_view_id IS NULL 的子集唯一索引，保持角色/道具/旧场景图
    每素材一张当前图的约束（SQLite 下 NULL 在唯一索引中互不相同）。"""
    if not _has_table(_ASSET_IMAGE_TABLE):
        return
    dialect = op.get_bind().dialect.name
    if _has_index(_ASSET_IMAGE_TABLE, _LEGACY_INDEX):
        op.drop_index(_LEGACY_INDEX, table_name=_ASSET_IMAGE_TABLE)
    if not _has_index(_ASSET_IMAGE_TABLE, _CURRENT_INDEX):
        predicate = sa.text("is_current = 1")
        if dialect == "sqlite":
            op.create_index(
                _CURRENT_INDEX,
                _ASSET_IMAGE_TABLE,
                ["asset_id", "scene_view_id"],
                unique=True,
                sqlite_where=predicate,
            )
        elif dialect == "postgresql":
            op.create_index(
                _CURRENT_INDEX,
                _ASSET_IMAGE_TABLE,
                ["asset_id", "scene_view_id"],
                unique=True,
                postgresql_where=predicate,
            )
        else:
            raise NotImplementedError(
                f"{_ASSET_IMAGE_TABLE} current partial index not supported on {dialect}"
            )
    if not _has_index(_ASSET_IMAGE_TABLE, _LEGACY_CURRENT_INDEX):
        legacy_predicate = sa.text(
            "is_current = 1 AND scene_view_id IS NULL"
        )
        if dialect == "sqlite":
            op.create_index(
                _LEGACY_CURRENT_INDEX,
                _ASSET_IMAGE_TABLE,
                ["asset_id"],
                unique=True,
                sqlite_where=legacy_predicate,
            )
        elif dialect == "postgresql":
            op.create_index(
                _LEGACY_CURRENT_INDEX,
                _ASSET_IMAGE_TABLE,
                ["asset_id"],
                unique=True,
                postgresql_where=legacy_predicate,
            )
        else:
            raise NotImplementedError(
                f"{_ASSET_IMAGE_TABLE} legacy current partial index "
                f"not supported on {dialect}"
            )


def upgrade() -> None:
    # 幂等守卫：新索引存在且旧索引不存在 ⇒ 已跑过。
    if not _has_table(_SCENE_VIEW_TABLE):
        _create_scene_view_table()
    _add_asset_image_scene_view_id()
    _add_shot_scene_view_id()
    _replace_current_index()


def _drop_scene_view_columns() -> None:
    """从 shot / asset_image 移除 scene_view_id。

    两列在 SQLite 下都是无 FK 的纯列（见 upgrade 说明），且相关索引已在
    downgrade 前面删除，直接用原生 DROP COLUMN（SQLite 3.35+ 支持），
    避免重建表触发外键级联删除 shot_audio / keyframe_version 等子表数据。
    """
    if not _has_table(_SHOT_TABLE) and not _has_table(_ASSET_IMAGE_TABLE):
        return
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        if _has_column(_SHOT_TABLE, "scene_view_id"):
            op.drop_column(_SHOT_TABLE, "scene_view_id")
        if _has_column(_ASSET_IMAGE_TABLE, "scene_view_id"):
            op.drop_column(_ASSET_IMAGE_TABLE, "scene_view_id")
        return
    if _has_column(_SHOT_TABLE, "scene_view_id"):
        op.drop_column(_SHOT_TABLE, "scene_view_id")
    if _has_column(_ASSET_IMAGE_TABLE, "scene_view_id"):
        op.drop_column(_ASSET_IMAGE_TABLE, "scene_view_id")


def _collapse_current_images_for_legacy_constraint() -> None:
    """downgrade 前收敛 is_current：旧唯一索引只允许每资产一条当前图。

    upgrade 后每个 Scene View（含 NULL 旧图分区）各有一张 current 图；
    直接重建旧的 asset_id 唯一索引会在多 View 当前图存在时失败。
    此处按 asset_id 保留一条（视图分区最新），其余置 0，与 0017 的
    _collapse_current_audio_for_legacy_constraint 做法对齐。
    """
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


def downgrade() -> None:
    # 回退守卫：旧索引已恢复 ⇒ 已回退过。
    if not _has_table(_ASSET_IMAGE_TABLE):
        return
    if _has_index(_ASSET_IMAGE_TABLE, _CURRENT_INDEX):
        op.drop_index(_CURRENT_INDEX, table_name=_ASSET_IMAGE_TABLE)
    if _has_index(_ASSET_IMAGE_TABLE, _LEGACY_CURRENT_INDEX):
        op.drop_index(_LEGACY_CURRENT_INDEX, table_name=_ASSET_IMAGE_TABLE)
    # 多 View 当前图收敛为每资产一条，否则重建唯一索引必然失败。
    _collapse_current_images_for_legacy_constraint()
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