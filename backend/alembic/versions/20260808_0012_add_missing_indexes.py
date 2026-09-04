"""add missing indexes for performance

Revision ID: 20260808_0012
Revises: 20260808_0011
Create Date: 2026-08-08

添加缺失的数据库索引以提升查询性能：
- shot 表：chapter_id 索引（用于按章节查询）
- keyframe_version 表：shot_id + status 复合索引（用于查询完成的版本）
- video_version 表：shot_id + status 复合索引（用于查询完成的版本）
- novel_analysis 表：status 索引（用于查询状态）
- screenplay 表：status 索引（用于查询状态）
- asset 表：status 索引（用于查询状态）
"""

import sqlalchemy as sa

from alembic import op

revision: str = "20260808_0012"
down_revision: str | None = "20260808_0011"
branch_labels: str | None = None
depends_on: str | None = None


def _has_index(table: str, index: str) -> bool:
    """幂等守卫：索引已存在则跳过，避免中断重放时报 already exists。"""
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return False
    return any(item["name"] == index for item in inspector.get_indexes(table))


def _create_index_if_missing(table: str, index: str, columns: list[str]) -> None:
    if table in sa.inspect(op.get_bind()).get_table_names() and _has_index(table, index):
        return
    op.create_index(index, table, columns)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())

    # shot 表：按章节查询的索引
    if "shot" in tables:
        _create_index_if_missing("shot", "idx_shot_chapter", ["chapter_id"])

    # keyframe_version 表：查询特定分镜的完成版本
    if "keyframe_version" in tables:
        _create_index_if_missing(
            "keyframe_version",
            "idx_keyframe_shot_status",
            ["shot_id", "status"],
        )

    # video_version 表：查询特定分镜的完成版本
    if "video_version" in tables:
        _create_index_if_missing(
            "video_version",
            "idx_video_shot_status",
            ["shot_id", "status"],
        )

    # novel_analysis 表：按状态查询
    if "novel_analysis" in tables:
        _create_index_if_missing("novel_analysis", "idx_novel_analysis_status", ["status"])

    # screenplay 表：按状态查询
    if "screenplay" in tables:
        _create_index_if_missing("screenplay", "idx_screenplay_status", ["status"])

    # asset 表：按状态查询（project_id + status 复合索引）
    if "asset" in tables:
        _create_index_if_missing("asset", "idx_asset_status", ["project_id", "status"])

    # job 表：按 chapter_id 查询（如果已存在则跳过）
    if "job" in tables:
        _create_index_if_missing("job", "idx_job_chapter", ["chapter_id"])


def downgrade() -> None:
    op.drop_index("idx_job_chapter", table_name="job")
    op.drop_index("idx_asset_status", table_name="asset")
    op.drop_index("idx_screenplay_status", table_name="screenplay")
    op.drop_index("idx_novel_analysis_status", table_name="novel_analysis")
    op.drop_index("idx_video_shot_status", table_name="video_version")
    op.drop_index("idx_keyframe_shot_status", table_name="keyframe_version")
    op.drop_index("idx_shot_chapter", table_name="shot")
