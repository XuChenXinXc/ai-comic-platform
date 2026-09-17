"""initial schema

Revision ID: 20260728_0001
Revises:
Create Date: 2026-07-28 18:28:23.439988
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260728_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """应用迁移。"""
    op.create_table(
        "api_provider",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("adapter", sa.Text(), nullable=False),
        sa.Column("base_url", sa.Text(), server_default="", nullable=False),
        sa.Column("api_key_env", sa.Text(), server_default="", nullable=False),
        sa.Column("model", sa.Text(), server_default="", nullable=False),
        sa.Column("params_json", sa.Text(), server_default="{}", nullable=False),
        sa.Column("timeout_sec", sa.Integer(), server_default="120", nullable=False),
        sa.Column("max_retries", sa.Integer(), server_default="2", nullable=False),
        sa.Column("enabled", sa.Integer(), server_default="1", nullable=False),
        sa.Column("is_default", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_test_ok", sa.Integer(), nullable=True),
        sa.Column("last_test_message", sa.Text(), server_default="", nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint("kind IN ('text','image','video')", name=op.f("ck_api_provider_kind")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_provider")),
    )
    with op.batch_alter_table("api_provider", schema=None) as batch_op:
        batch_op.create_index(
            "ux_provider_default", ["kind"], unique=True, sqlite_where=sa.text("is_default = 1")
        )

    op.create_table(
        "project",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("source_text", sa.Text(), server_default="", nullable=False),
        sa.Column("source_signature", sa.Text(), server_default="", nullable=False),
        sa.Column("source_revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("style_prompt", sa.Text(), server_default="", nullable=False),
        sa.Column("aspect_ratio", sa.Text(), server_default="16:9", nullable=False),
        sa.Column("image_size", sa.Text(), server_default="1920x1080", nullable=False),
        sa.Column("video_size", sa.Text(), server_default="16:9", nullable=False),
        sa.Column("default_duration_sec", sa.Float(), server_default="6", nullable=False),
        sa.Column("default_text_provider_id", sa.Text(), nullable=True),
        sa.Column("default_image_provider_id", sa.Text(), nullable=True),
        sa.Column("default_video_provider_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_project")),
    )
    op.create_table(
        "asset",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("asset_type", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("profile_json", sa.Text(), server_default="{}", nullable=False),
        sa.Column("prompt", sa.Text(), server_default="", nullable=False),
        sa.Column("negative_prompt", sa.Text(), server_default="", nullable=False),
        sa.Column("consistency_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column("composition", sa.Text(), server_default="", nullable=False),
        sa.Column("importance", sa.Text(), server_default="normal", nullable=False),
        sa.Column("status", sa.Text(), server_default="draft", nullable=False),
        sa.Column("source_signature", sa.Text(), server_default="", nullable=False),
        sa.Column("own_signature", sa.Text(), server_default="", nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "asset_type IN ('character','scene','prop')", name=op.f("ck_asset_asset_type")
        ),
        sa.CheckConstraint("importance IN ('core','normal')", name=op.f("ck_asset_importance")),
        sa.CheckConstraint(
            "status IN ('draft','ready','confirmed','failed')", name=op.f("ck_asset_status")
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            name=op.f("fk_asset_project_id_project"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_asset")),
        sa.UniqueConstraint("project_id", "asset_type", "name", name="uq_asset_project_type_name"),
    )
    with op.batch_alter_table("asset", schema=None) as batch_op:
        batch_op.create_index("idx_asset_project_type", ["project_id", "asset_type"], unique=False)

    op.create_table(
        "job",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("job_type", sa.Text(), nullable=False),
        sa.Column("target_type", sa.Text(), server_default="", nullable=False),
        sa.Column("target_id", sa.Text(), nullable=True),
        sa.Column("batch_id", sa.Text(), nullable=True),
        sa.Column("provider_kind", sa.Text(), server_default="", nullable=False),
        sa.Column("provider_id", sa.Text(), nullable=True),
        sa.Column("provider_task_id", sa.Text(), server_default="", nullable=False),
        sa.Column("status", sa.Text(), server_default="queued", nullable=False),
        sa.Column("progress_current", sa.Integer(), server_default="0", nullable=False),
        sa.Column("progress_total", sa.Integer(), server_default="1", nullable=False),
        sa.Column("message", sa.Text(), server_default="", nullable=False),
        sa.Column("input_json", sa.Text(), server_default="{}", nullable=False),
        sa.Column("result_json", sa.Text(), server_default="{}", nullable=False),
        sa.Column("error", sa.Text(), server_default="", nullable=False),
        sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.Column("started_at", sa.Text(), nullable=True),
        sa.Column("finished_at", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued','running','waiting_provider','downloading',"
            "'completed','failed','cancelled','interrupted')",
            name=op.f("ck_job_status"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            name=op.f("fk_job_project_id_project"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"], ["api_provider.id"], name=op.f("fk_job_provider_id_api_provider")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job")),
    )
    with op.batch_alter_table("job", schema=None) as batch_op:
        batch_op.create_index("idx_job_batch", ["batch_id"], unique=False)
        batch_op.create_index("idx_job_project_status", ["project_id", "status"], unique=False)

    op.create_table(
        "novel_analysis",
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("content_json", sa.Text(), server_default="{}", nullable=False),
        sa.Column("status", sa.Text(), server_default="draft", nullable=False),
        sa.Column("source_signature", sa.Text(), server_default="", nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("generated_by", sa.Text(), server_default="", nullable=False),
        sa.Column("error", sa.Text(), server_default="", nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft','generating','ready','confirmed','failed')",
            name=op.f("ck_novel_analysis_status"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            name=op.f("fk_novel_analysis_project_id_project"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("project_id", name=op.f("pk_novel_analysis")),
    )
    op.create_table(
        "screenplay",
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("content_json", sa.Text(), server_default="{}", nullable=False),
        sa.Column("status", sa.Text(), server_default="draft", nullable=False),
        sa.Column("source_signature", sa.Text(), server_default="", nullable=False),
        sa.Column("own_signature", sa.Text(), server_default="", nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("generated_by", sa.Text(), server_default="", nullable=False),
        sa.Column("error", sa.Text(), server_default="", nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft','generating','ready','confirmed','failed')",
            name=op.f("ck_screenplay_status"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            name=op.f("fk_screenplay_project_id_project"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("project_id", name=op.f("pk_screenplay")),
    )
    op.create_table(
        "asset_image",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("asset_id", sa.Text(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=True),
        sa.Column("prompt_snapshot", sa.Text(), server_default="", nullable=False),
        sa.Column("negative_prompt_snapshot", sa.Text(), server_default="", nullable=False),
        sa.Column("reference_files_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column("provider_id", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), server_default="", nullable=False),
        sa.Column("provider_task_id", sa.Text(), server_default="", nullable=False),
        sa.Column("status", sa.Text(), server_default="queued", nullable=False),
        sa.Column("is_current", sa.Integer(), server_default="0", nullable=False),
        sa.Column("input_signature", sa.Text(), server_default="", nullable=False),
        sa.Column("source", sa.Text(), server_default="generated", nullable=False),
        sa.Column("error", sa.Text(), server_default="", nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "source IN ('generated','uploaded')", name=op.f("ck_asset_image_source")
        ),
        sa.CheckConstraint(
            "status IN ('queued','running','waiting_provider','completed','failed')",
            name=op.f("ck_asset_image_status"),
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["asset.id"],
            name=op.f("fk_asset_image_asset_id_asset"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["api_provider.id"],
            name=op.f("fk_asset_image_provider_id_api_provider"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_asset_image")),
        sa.UniqueConstraint("asset_id", "version_no", name="uq_asset_image_asset_version"),
    )
    with op.batch_alter_table("asset_image", schema=None) as batch_op:
        batch_op.create_index(
            "ux_asset_image_current",
            ["asset_id"],
            unique=True,
            sqlite_where=sa.text("is_current = 1"),
        )

    op.create_table(
        "shot",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("shot_no", sa.Integer(), nullable=False),
        sa.Column("shot_order", sa.Integer(), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("source_ref", sa.Text(), server_default="", nullable=False),
        sa.Column("source_signature", sa.Text(), server_default="", nullable=False),
        sa.Column("scene_asset_id", sa.Text(), nullable=True),
        sa.Column("character_ids_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column("prop_ids_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column("positions_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column("start_state", sa.Text(), server_default="", nullable=False),
        sa.Column("action", sa.Text(), server_default="", nullable=False),
        sa.Column("expressions_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column("dialogue_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column("narration", sa.Text(), server_default="", nullable=False),
        sa.Column("shot_type", sa.Text(), server_default="", nullable=False),
        sa.Column("camera_angle", sa.Text(), server_default="", nullable=False),
        sa.Column("camera_move", sa.Text(), server_default="", nullable=False),
        sa.Column("duration_sec", sa.Float(), server_default="6", nullable=False),
        sa.Column("end_state", sa.Text(), server_default="", nullable=False),
        sa.Column("next_hook", sa.Text(), server_default="", nullable=False),
        sa.Column("frame_description", sa.Text(), server_default="", nullable=False),
        sa.Column("image_prompt", sa.Text(), server_default="", nullable=False),
        sa.Column("video_prompt", sa.Text(), server_default="", nullable=False),
        sa.Column("negative_prompt", sa.Text(), server_default="", nullable=False),
        sa.Column("reference_bindings_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column("status", sa.Text(), server_default="draft", nullable=False),
        sa.Column("own_signature", sa.Text(), server_default="", nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "source_type IN ('novel','screenplay')", name=op.f("ck_shot_source_type")
        ),
        sa.CheckConstraint(
            "status IN ('draft','ready','confirmed','failed')", name=op.f("ck_shot_status")
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            name=op.f("fk_shot_project_id_project"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["scene_asset_id"],
            ["asset.id"],
            name=op.f("fk_shot_scene_asset_id_asset"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shot")),
        sa.UniqueConstraint("project_id", "shot_no", name="uq_shot_project_no"),
    )
    with op.batch_alter_table("shot", schema=None) as batch_op:
        batch_op.create_index("idx_shot_project_order", ["project_id", "shot_order"], unique=False)

    op.create_table(
        "keyframe_version",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("shot_id", sa.Text(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=True),
        sa.Column("prompt_snapshot", sa.Text(), server_default="", nullable=False),
        sa.Column("negative_prompt_snapshot", sa.Text(), server_default="", nullable=False),
        sa.Column("reference_bindings_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column("provider_id", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), server_default="", nullable=False),
        sa.Column("provider_task_id", sa.Text(), server_default="", nullable=False),
        sa.Column("status", sa.Text(), server_default="queued", nullable=False),
        sa.Column("is_current", sa.Integer(), server_default="0", nullable=False),
        sa.Column("input_signature", sa.Text(), server_default="", nullable=False),
        sa.Column("source", sa.Text(), server_default="generated", nullable=False),
        sa.Column("error", sa.Text(), server_default="", nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "source IN ('generated','uploaded')", name=op.f("ck_keyframe_version_source")
        ),
        sa.CheckConstraint(
            "status IN ('queued','running','waiting_provider','completed','failed')",
            name=op.f("ck_keyframe_version_status"),
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["api_provider.id"],
            name=op.f("fk_keyframe_version_provider_id_api_provider"),
        ),
        sa.ForeignKeyConstraint(
            ["shot_id"],
            ["shot.id"],
            name=op.f("fk_keyframe_version_shot_id_shot"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_keyframe_version")),
        sa.UniqueConstraint("shot_id", "version_no", name="uq_keyframe_shot_version"),
    )
    with op.batch_alter_table("keyframe_version", schema=None) as batch_op:
        batch_op.create_index(
            "ux_keyframe_current", ["shot_id"], unique=True, sqlite_where=sa.text("is_current = 1")
        )

    op.create_table(
        "video_version",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("shot_id", sa.Text(), nullable=False),
        sa.Column("keyframe_version_id", sa.Text(), nullable=True),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=True),
        sa.Column("prompt_snapshot", sa.Text(), server_default="", nullable=False),
        sa.Column("negative_prompt_snapshot", sa.Text(), server_default="", nullable=False),
        sa.Column("requested_duration_sec", sa.Float(), server_default="0", nullable=False),
        sa.Column("actual_duration_sec", sa.Float(), nullable=True),
        sa.Column("provider_id", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), server_default="", nullable=False),
        sa.Column("provider_task_id", sa.Text(), server_default="", nullable=False),
        sa.Column("status", sa.Text(), server_default="queued", nullable=False),
        sa.Column("is_current", sa.Integer(), server_default="0", nullable=False),
        sa.Column("input_signature", sa.Text(), server_default="", nullable=False),
        sa.Column("source", sa.Text(), server_default="generated", nullable=False),
        sa.Column("cost_estimate", sa.Float(), server_default="0", nullable=False),
        sa.Column("error", sa.Text(), server_default="", nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "source IN ('generated','uploaded')", name=op.f("ck_video_version_source")
        ),
        sa.CheckConstraint(
            "status IN ('queued','running','waiting_provider','downloading','completed','failed')",
            name=op.f("ck_video_version_status"),
        ),
        sa.ForeignKeyConstraint(
            ["keyframe_version_id"],
            ["keyframe_version.id"],
            name=op.f("fk_video_version_keyframe_version_id_keyframe_version"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["api_provider.id"],
            name=op.f("fk_video_version_provider_id_api_provider"),
        ),
        sa.ForeignKeyConstraint(
            ["shot_id"], ["shot.id"], name=op.f("fk_video_version_shot_id_shot"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_video_version")),
        sa.UniqueConstraint("shot_id", "version_no", name="uq_video_shot_version"),
    )
    with op.batch_alter_table("video_version", schema=None) as batch_op:
        batch_op.create_index(
            "ux_video_current", ["shot_id"], unique=True, sqlite_where=sa.text("is_current = 1")
        )


def downgrade() -> None:
    """回滚迁移。"""
    with op.batch_alter_table("video_version", schema=None) as batch_op:
        batch_op.drop_index("ux_video_current", sqlite_where=sa.text("is_current = 1"))

    op.drop_table("video_version")
    with op.batch_alter_table("keyframe_version", schema=None) as batch_op:
        batch_op.drop_index("ux_keyframe_current", sqlite_where=sa.text("is_current = 1"))

    op.drop_table("keyframe_version")
    with op.batch_alter_table("shot", schema=None) as batch_op:
        batch_op.drop_index("idx_shot_project_order")

    op.drop_table("shot")
    with op.batch_alter_table("asset_image", schema=None) as batch_op:
        batch_op.drop_index("ux_asset_image_current", sqlite_where=sa.text("is_current = 1"))

    op.drop_table("asset_image")
    op.drop_table("screenplay")
    op.drop_table("novel_analysis")
    with op.batch_alter_table("job", schema=None) as batch_op:
        batch_op.drop_index("idx_job_project_status")
        batch_op.drop_index("idx_job_batch")

    op.drop_table("job")
    with op.batch_alter_table("asset", schema=None) as batch_op:
        batch_op.drop_index("idx_asset_project_type")

    op.drop_table("asset")
    op.drop_table("project")
    with op.batch_alter_table("api_provider", schema=None) as batch_op:
        batch_op.drop_index("ux_provider_default", sqlite_where=sa.text("is_default = 1"))

    op.drop_table("api_provider")
