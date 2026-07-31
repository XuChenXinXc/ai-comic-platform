from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
)

metadata = MetaData(
    naming_convention={
        "ix": "ix_%(column_0_label)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)


project = Table(
    "project",
    metadata,
    Column("id", Text, primary_key=True),
    Column("name", Text, nullable=False),
    Column("storage_name", Text, nullable=False, server_default=""),
    Column("storage_root", Text, nullable=False, server_default=""),
    Column("storage_layout", Text, nullable=False, server_default="prefixed"),
    Column("source_text", Text, nullable=False, server_default=""),
    Column("source_signature", Text, nullable=False, server_default=""),
    Column("source_revision", Integer, nullable=False, server_default="1"),
    Column("style_prompt", Text, nullable=False, server_default=""),
    Column("aspect_ratio", Text, nullable=False, server_default="16:9"),
    Column("image_size", Text, nullable=False, server_default="1920x1080"),
    Column("video_size", Text, nullable=False, server_default="16:9"),
    Column("default_duration_sec", Float, nullable=False, server_default="6"),
    Column("default_text_provider_id", Text),
    Column("default_image_provider_id", Text),
    Column("default_video_provider_id", Text),
    Column("created_at", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
)


platform_setting = Table(
    "platform_setting",
    metadata,
    Column("key", Text, primary_key=True),
    Column("value", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
)


api_provider = Table(
    "api_provider",
    metadata,
    Column("id", Text, primary_key=True),
    Column("kind", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("adapter", Text, nullable=False),
    Column("base_url", Text, nullable=False, server_default=""),
    Column("api_key_env", Text, nullable=False, server_default=""),
    Column("api_key_ciphertext", Text),
    Column("model", Text, nullable=False, server_default=""),
    Column("params_json", Text, nullable=False, server_default="{}"),
    Column("timeout_sec", Integer, nullable=False, server_default="120"),
    Column("max_retries", Integer, nullable=False, server_default="2"),
    Column("enabled", Integer, nullable=False, server_default="1"),
    Column("is_default", Integer, nullable=False, server_default="0"),
    Column("last_test_ok", Integer),
    Column("last_test_message", Text, nullable=False, server_default=""),
    Column("created_at", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
    CheckConstraint("kind IN ('text','image','video')", name="kind"),
)
Index(
    "ux_provider_default",
    api_provider.c.kind,
    unique=True,
    sqlite_where=api_provider.c.is_default == 1,
)


novel_analysis = Table(
    "novel_analysis",
    metadata,
    Column(
        "project_id",
        Text,
        ForeignKey("project.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("content_json", Text, nullable=False, server_default="{}"),
    Column("status", Text, nullable=False, server_default="draft"),
    Column("source_signature", Text, nullable=False, server_default=""),
    Column("revision", Integer, nullable=False, server_default="1"),
    Column("generated_by", Text, nullable=False, server_default=""),
    Column("error", Text, nullable=False, server_default=""),
    Column("updated_at", Text, nullable=False),
    CheckConstraint(
        "status IN ('draft','generating','ready','confirmed','failed')",
        name="status",
    ),
)


screenplay = Table(
    "screenplay",
    metadata,
    Column(
        "project_id",
        Text,
        ForeignKey("project.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("content_json", Text, nullable=False, server_default="{}"),
    Column("status", Text, nullable=False, server_default="draft"),
    Column("source_signature", Text, nullable=False, server_default=""),
    Column("own_signature", Text, nullable=False, server_default=""),
    Column("revision", Integer, nullable=False, server_default="1"),
    Column("generated_by", Text, nullable=False, server_default=""),
    Column("error", Text, nullable=False, server_default=""),
    Column("updated_at", Text, nullable=False),
    CheckConstraint(
        "status IN ('draft','generating','ready','confirmed','failed')",
        name="status",
    ),
)


asset = Table(
    "asset",
    metadata,
    Column("id", Text, primary_key=True),
    Column("project_id", Text, ForeignKey("project.id", ondelete="CASCADE"), nullable=False),
    Column("asset_type", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("profile_json", Text, nullable=False, server_default="{}"),
    Column("prompt", Text, nullable=False, server_default=""),
    Column("negative_prompt", Text, nullable=False, server_default=""),
    Column("consistency_json", Text, nullable=False, server_default="[]"),
    Column("composition", Text, nullable=False, server_default=""),
    Column("importance", Text, nullable=False, server_default="normal"),
    Column("status", Text, nullable=False, server_default="draft"),
    Column("source_signature", Text, nullable=False, server_default=""),
    Column("own_signature", Text, nullable=False, server_default=""),
    Column("revision", Integer, nullable=False, server_default="1"),
    Column("created_at", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
    CheckConstraint("asset_type IN ('character','scene','prop')", name="asset_type"),
    CheckConstraint("importance IN ('core','normal')", name="importance"),
    CheckConstraint("status IN ('draft','ready','confirmed','failed')", name="status"),
    UniqueConstraint("project_id", "asset_type", "name", name="uq_asset_project_type_name"),
)
Index("idx_asset_project_type", asset.c.project_id, asset.c.asset_type)


asset_image = Table(
    "asset_image",
    metadata,
    Column("id", Text, primary_key=True),
    Column("asset_id", Text, ForeignKey("asset.id", ondelete="CASCADE"), nullable=False),
    Column("version_no", Integer, nullable=False),
    Column("file_path", Text),
    Column("prompt_snapshot", Text, nullable=False, server_default=""),
    Column("negative_prompt_snapshot", Text, nullable=False, server_default=""),
    Column("reference_files_json", Text, nullable=False, server_default="[]"),
    Column("provider_id", Text, ForeignKey("api_provider.id")),
    Column("model", Text, nullable=False, server_default=""),
    Column("provider_task_id", Text, nullable=False, server_default=""),
    Column("status", Text, nullable=False, server_default="queued"),
    Column("is_current", Integer, nullable=False, server_default="0"),
    Column("input_signature", Text, nullable=False, server_default=""),
    Column("source", Text, nullable=False, server_default="generated"),
    Column("error", Text, nullable=False, server_default=""),
    Column("created_at", Text, nullable=False),
    CheckConstraint(
        "status IN ('queued','running','waiting_provider','completed','failed')",
        name="status",
    ),
    CheckConstraint("source IN ('generated','uploaded')", name="source"),
    UniqueConstraint("asset_id", "version_no", name="uq_asset_image_asset_version"),
)
Index(
    "ux_asset_image_current",
    asset_image.c.asset_id,
    unique=True,
    sqlite_where=asset_image.c.is_current == 1,
)


shot = Table(
    "shot",
    metadata,
    Column("id", Text, primary_key=True),
    Column("project_id", Text, ForeignKey("project.id", ondelete="CASCADE"), nullable=False),
    Column("shot_no", Integer, nullable=False),
    Column("shot_order", Integer, nullable=False),
    Column("source_type", Text, nullable=False),
    Column("source_ref", Text, nullable=False, server_default=""),
    Column("source_signature", Text, nullable=False, server_default=""),
    Column("scene_asset_id", Text, ForeignKey("asset.id", ondelete="SET NULL")),
    Column("character_ids_json", Text, nullable=False, server_default="[]"),
    Column("prop_ids_json", Text, nullable=False, server_default="[]"),
    Column("positions_json", Text, nullable=False, server_default="[]"),
    Column("start_state", Text, nullable=False, server_default=""),
    Column("action", Text, nullable=False, server_default=""),
    Column("expressions_json", Text, nullable=False, server_default="[]"),
    Column("dialogue_json", Text, nullable=False, server_default="[]"),
    Column("narration", Text, nullable=False, server_default=""),
    Column("shot_type", Text, nullable=False, server_default=""),
    Column("camera_angle", Text, nullable=False, server_default=""),
    Column("camera_move", Text, nullable=False, server_default=""),
    Column("duration_sec", Float, nullable=False, server_default="6"),
    Column("end_state", Text, nullable=False, server_default=""),
    Column("next_hook", Text, nullable=False, server_default=""),
    Column("frame_description", Text, nullable=False, server_default=""),
    Column("image_prompt", Text, nullable=False, server_default=""),
    Column("video_prompt", Text, nullable=False, server_default=""),
    Column("negative_prompt", Text, nullable=False, server_default=""),
    Column("reference_bindings_json", Text, nullable=False, server_default="[]"),
    Column("status", Text, nullable=False, server_default="draft"),
    Column("own_signature", Text, nullable=False, server_default=""),
    Column("revision", Integer, nullable=False, server_default="1"),
    Column("created_at", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
    CheckConstraint("source_type IN ('novel','screenplay')", name="source_type"),
    CheckConstraint("status IN ('draft','ready','confirmed','failed')", name="status"),
    UniqueConstraint("project_id", "shot_no", name="uq_shot_project_no"),
)
Index("idx_shot_project_order", shot.c.project_id, shot.c.shot_order)


keyframe_version = Table(
    "keyframe_version",
    metadata,
    Column("id", Text, primary_key=True),
    Column("shot_id", Text, ForeignKey("shot.id", ondelete="CASCADE"), nullable=False),
    Column("version_no", Integer, nullable=False),
    Column("file_path", Text),
    Column("prompt_snapshot", Text, nullable=False, server_default=""),
    Column("negative_prompt_snapshot", Text, nullable=False, server_default=""),
    Column("reference_bindings_json", Text, nullable=False, server_default="[]"),
    Column("provider_id", Text, ForeignKey("api_provider.id")),
    Column("model", Text, nullable=False, server_default=""),
    Column("provider_task_id", Text, nullable=False, server_default=""),
    Column("status", Text, nullable=False, server_default="queued"),
    Column("is_current", Integer, nullable=False, server_default="0"),
    Column("input_signature", Text, nullable=False, server_default=""),
    Column("source", Text, nullable=False, server_default="generated"),
    Column("error", Text, nullable=False, server_default=""),
    Column("created_at", Text, nullable=False),
    CheckConstraint(
        "status IN ('queued','running','waiting_provider','completed','failed')",
        name="status",
    ),
    CheckConstraint("source IN ('generated','uploaded')", name="source"),
    UniqueConstraint("shot_id", "version_no", name="uq_keyframe_shot_version"),
)
Index(
    "ux_keyframe_current",
    keyframe_version.c.shot_id,
    unique=True,
    sqlite_where=keyframe_version.c.is_current == 1,
)


video_version = Table(
    "video_version",
    metadata,
    Column("id", Text, primary_key=True),
    Column("shot_id", Text, ForeignKey("shot.id", ondelete="CASCADE"), nullable=False),
    Column(
        "keyframe_version_id",
        Text,
        ForeignKey("keyframe_version.id", ondelete="SET NULL"),
    ),
    Column("version_no", Integer, nullable=False),
    Column("file_path", Text),
    Column("prompt_snapshot", Text, nullable=False, server_default=""),
    Column("negative_prompt_snapshot", Text, nullable=False, server_default=""),
    Column("requested_duration_sec", Float, nullable=False, server_default="0"),
    Column("actual_duration_sec", Float),
    Column("provider_id", Text, ForeignKey("api_provider.id")),
    Column("model", Text, nullable=False, server_default=""),
    Column("provider_task_id", Text, nullable=False, server_default=""),
    Column("status", Text, nullable=False, server_default="queued"),
    Column("is_current", Integer, nullable=False, server_default="0"),
    Column("input_signature", Text, nullable=False, server_default=""),
    Column("source", Text, nullable=False, server_default="generated"),
    Column("cost_estimate", Float, nullable=False, server_default="0"),
    Column("error", Text, nullable=False, server_default=""),
    Column("created_at", Text, nullable=False),
    CheckConstraint(
        "status IN ('queued','running','waiting_provider','downloading','completed','failed')",
        name="status",
    ),
    CheckConstraint("source IN ('generated','uploaded')", name="source"),
    UniqueConstraint("shot_id", "version_no", name="uq_video_shot_version"),
)
Index(
    "ux_video_current",
    video_version.c.shot_id,
    unique=True,
    sqlite_where=video_version.c.is_current == 1,
)


job = Table(
    "job",
    metadata,
    Column("id", Text, primary_key=True),
    Column("project_id", Text, ForeignKey("project.id", ondelete="CASCADE"), nullable=False),
    Column("job_type", Text, nullable=False),
    Column("target_type", Text, nullable=False, server_default=""),
    Column("target_id", Text),
    Column("batch_id", Text),
    Column("provider_kind", Text, nullable=False, server_default=""),
    Column("provider_id", Text, ForeignKey("api_provider.id")),
    Column("provider_task_id", Text, nullable=False, server_default=""),
    Column("status", Text, nullable=False, server_default="queued"),
    Column("progress_current", Integer, nullable=False, server_default="0"),
    Column("progress_total", Integer, nullable=False, server_default="1"),
    Column("message", Text, nullable=False, server_default=""),
    Column("input_json", Text, nullable=False, server_default="{}"),
    Column("result_json", Text, nullable=False, server_default="{}"),
    Column("error", Text, nullable=False, server_default=""),
    Column("retry_count", Integer, nullable=False, server_default="0"),
    Column("created_at", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
    Column("started_at", Text),
    Column("finished_at", Text),
    CheckConstraint(
        "status IN "
        "('queued','running','waiting_provider','downloading','completed','failed',"
        "'cancelled','interrupted')",
        name="status",
    ),
)
Index("idx_job_project_status", job.c.project_id, job.c.status)
Index("idx_job_batch", job.c.batch_id)


ALL_TABLES = (
    project,
    platform_setting,
    api_provider,
    novel_analysis,
    screenplay,
    asset,
    asset_image,
    shot,
    keyframe_version,
    video_version,
    job,
)
