"""音频、字幕与合成功能

Revision ID: 20260808_0011
Revises: 20260807_0010
Create Date: 2026-08-08

新增表：
- audio_config: 项目音频配置（TTS Provider、音量等）
- shot_audio: 分镜配音（TTS 生成或上传）
- shot_subtitle: 分镜字幕（时间轴、样式）
- project_bgm: 项目 BGM 素材库
- chapter_composition: 章节合成任务与输出

同时扩展 api_provider.kind 支持 'tts'。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260808_0011"
down_revision: str | None = "20260807_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(table: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table in inspector.get_table_names()


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return False
    return any(item["name"] == column for item in inspector.get_columns(table))


def upgrade() -> None:
    # 1. 修改 api_provider.kind 约束，支持 tts
    # 注意：SQLite 的 CHECK 约束在 INSERT 时对已存在的旧表立即生效，
    # tables.py 的模型约束不会作用于已建成的表。因此在 0011~0016
    # 区间的已有库上插入 tts Provider 会触发旧 CHECK 失败；
    # 真正的约束修复由 0017 的表重建完成（含 kind CHECK 重建）。

    # 2. 创建 audio_config 表
    if not _has_table("audio_config"):
        op.create_table(
            "audio_config",
            sa.Column("project_id", sa.Text(), nullable=False),
            sa.Column("tts_provider_id", sa.Text(), nullable=True),
            sa.Column("bgm_volume", sa.Float(), nullable=False, server_default="0.3"),
            sa.Column("dialogue_volume", sa.Float(), nullable=False, server_default="1.0"),
            sa.Column("sound_effect_volume", sa.Float(), nullable=False, server_default="0.5"),
            sa.Column("updated_at", sa.Text(), nullable=False),
            sa.CheckConstraint(
                "bgm_volume >= 0 AND bgm_volume <= 1",
                name="ck_audio_config_bgm_volume",
            ),
            sa.CheckConstraint(
                "dialogue_volume >= 0 AND dialogue_volume <= 1",
                name="ck_audio_config_dialogue_volume",
            ),
            sa.CheckConstraint(
                "sound_effect_volume >= 0 AND sound_effect_volume <= 1",
                name="ck_audio_config_sound_effect_volume",
            ),
            sa.ForeignKeyConstraint(
                ["project_id"],
                ["project.id"],
                name="fk_audio_config_project_id_project",
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["tts_provider_id"],
                ["api_provider.id"],
                name="fk_audio_config_tts_provider_id_api_provider",
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("project_id", name="pk_audio_config"),
        )

    # 3. 创建 shot_audio 表
    if not _has_table("shot_audio"):
        op.create_table(
            "shot_audio",
            sa.Column("id", sa.Text(), nullable=False),
            sa.Column("shot_id", sa.Text(), nullable=False),
            sa.Column("dialogue_text", sa.Text(), nullable=False),
            sa.Column("speaker_name", sa.Text(), nullable=False, server_default=""),
            sa.Column("voice_id", sa.Text(), nullable=False, server_default=""),
            sa.Column("file_path", sa.Text(), nullable=True),
            sa.Column("duration_sec", sa.Float(), nullable=True),
            sa.Column("version_no", sa.Integer(), nullable=False),
            sa.Column("provider_id", sa.Text(), nullable=True),
            sa.Column("model", sa.Text(), nullable=False, server_default=""),
            sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
            sa.Column("is_current", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("source", sa.Text(), nullable=False, server_default="generated"),
            sa.Column("error", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.Text(), nullable=False),
            sa.CheckConstraint(
                "status IN ('queued','running','completed','failed')",
                name="ck_shot_audio_status",
            ),
            sa.CheckConstraint("source IN ('generated','uploaded')", name="ck_shot_audio_source"),
            sa.ForeignKeyConstraint(
                ["shot_id"],
                ["shot.id"],
                name="fk_shot_audio_shot_id_shot",
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["provider_id"],
                ["api_provider.id"],
                name="fk_shot_audio_provider_id_api_provider",
            ),
            sa.PrimaryKeyConstraint("id", name="pk_shot_audio"),
            sa.UniqueConstraint("shot_id", "version_no", name="uq_shot_audio_shot_version"),
        )
        op.create_index(
            "ux_shot_audio_current",
            "shot_audio",
            ["shot_id"],
            unique=True,
            sqlite_where=sa.text("is_current = 1"),
        )

    # 4. 创建 shot_subtitle 表
    if not _has_table("shot_subtitle"):
        op.create_table(
            "shot_subtitle",
            sa.Column("id", sa.Text(), nullable=False),
            sa.Column("shot_id", sa.Text(), nullable=False),
            sa.Column("text", sa.Text(), nullable=False),
            sa.Column("start_time", sa.Float(), nullable=False),
            sa.Column("end_time", sa.Float(), nullable=False),
            sa.Column("style_preset", sa.Text(), nullable=False, server_default="default"),
            sa.Column("created_at", sa.Text(), nullable=False),
            sa.CheckConstraint(
                "start_time >= 0 AND end_time > start_time",
                name="ck_shot_subtitle_time_range",
            ),
            sa.ForeignKeyConstraint(
                ["shot_id"],
                ["shot.id"],
                name="fk_shot_subtitle_shot_id_shot",
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id", name="pk_shot_subtitle"),
        )
        op.create_index("idx_subtitle_shot", "shot_subtitle", ["shot_id"])

    # 5. 创建 project_bgm 表
    if not _has_table("project_bgm"):
        op.create_table(
            "project_bgm",
            sa.Column("id", sa.Text(), nullable=False),
            sa.Column("project_id", sa.Text(), nullable=False),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("file_path", sa.Text(), nullable=False),
            sa.Column("duration_sec", sa.Float(), nullable=True),
            sa.Column("mood_tags", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("created_at", sa.Text(), nullable=False),
            sa.ForeignKeyConstraint(
                ["project_id"],
                ["project.id"],
                name="fk_project_bgm_project_id_project",
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id", name="pk_project_bgm"),
        )
        op.create_index("idx_bgm_project", "project_bgm", ["project_id"])

    # 6. 创建 chapter_composition 表
    if not _has_table("chapter_composition"):
        op.create_table(
            "chapter_composition",
            sa.Column("id", sa.Text(), nullable=False),
            sa.Column("chapter_id", sa.Text(), nullable=False),
            sa.Column("job_id", sa.Text(), nullable=True),
            sa.Column("file_path", sa.Text(), nullable=True),
            sa.Column("duration_sec", sa.Float(), nullable=True),
            sa.Column("resolution", sa.Text(), nullable=False, server_default="1080p"),
            sa.Column("include_audio", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("burn_subtitles", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("bgm_id", sa.Text(), nullable=True),
            sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
            sa.Column("error", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.Text(), nullable=False),
            sa.Column("updated_at", sa.Text(), nullable=False),
            sa.CheckConstraint(
                "status IN ('pending','processing','completed','failed')",
                name="ck_chapter_composition_status",
            ),
            sa.CheckConstraint(
                "resolution IN ('720p','1080p','4k')",
                name="ck_chapter_composition_resolution",
            ),
            sa.ForeignKeyConstraint(
                ["chapter_id"],
                ["chapter.id"],
                name="fk_chapter_composition_chapter_id_chapter",
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["job_id"],
                ["job.id"],
                name="fk_chapter_composition_job_id_job",
                ondelete="SET NULL",
            ),
            sa.ForeignKeyConstraint(
                ["bgm_id"],
                ["project_bgm.id"],
                name="fk_chapter_composition_bgm_id_project_bgm",
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id", name="pk_chapter_composition"),
        )
        op.create_index(
            "ux_composition_chapter",
            "chapter_composition",
            ["chapter_id"],
            unique=True,
            sqlite_where=sa.text("status IN ('pending', 'processing')"),
        )


def downgrade() -> None:
    # 逆序删除表
    if _has_table("chapter_composition"):
        op.drop_index("ux_composition_chapter", table_name="chapter_composition")
        op.drop_table("chapter_composition")

    if _has_table("project_bgm"):
        op.drop_index("idx_bgm_project", table_name="project_bgm")
        op.drop_table("project_bgm")

    if _has_table("shot_subtitle"):
        op.drop_index("idx_subtitle_shot", table_name="shot_subtitle")
        op.drop_table("shot_subtitle")

    if _has_table("shot_audio"):
        op.drop_index("ux_shot_audio_current", table_name="shot_audio")
        op.drop_table("shot_audio")

    if _has_table("audio_config"):
        op.drop_table("audio_config")

    # 恢复 api_provider.kind 约束
    # SQLite 约束修改复杂，且 downgrade 很少使用，跳过
    pass
