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
    # 已废弃：正文与签名自 0008 起归属 chapter，保留列仅为兼容旧库结构
    Column("source_text", Text, nullable=False, server_default=""),
    Column("source_signature", Text, nullable=False, server_default=""),
    Column("source_revision", Integer, nullable=False, server_default="1"),
    Column(
        "style_prompt",
        Text,
        nullable=False,
        server_default="电影感现实主义二维国漫，稳定自然的半写实人物比例，沉稳细腻线条，低饱和冷暖协调色调，真实清晰的微表情，层次分明的电影光影。",
    ),
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


chapter = Table(
    "chapter",
    metadata,
    Column("id", Text, primary_key=True),
    Column("project_id", Text, ForeignKey("project.id", ondelete="CASCADE"), nullable=False),
    Column("chapter_no", Integer, nullable=False),
    Column("title", Text, nullable=False, server_default=""),
    Column("source_text", Text, nullable=False, server_default=""),
    Column("source_signature", Text, nullable=False, server_default=""),
    Column("source_revision", Integer, nullable=False, server_default="1"),
    Column("status", Text, nullable=False, server_default="draft"),
    Column("created_at", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
    CheckConstraint("status IN ('draft','ready','confirmed','failed')", name="status"),
    UniqueConstraint("project_id", "chapter_no", name="uq_chapter_project_no"),
)
Index("idx_chapter_project_no", chapter.c.project_id, chapter.c.chapter_no)


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
    CheckConstraint("kind IN ('text','image','video','tts')", name="kind"),
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
        "chapter_id",
        Text,
        ForeignKey("chapter.id", ondelete="CASCADE"),
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
        "chapter_id",
        Text,
        ForeignKey("chapter.id", ondelete="CASCADE"),
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
    Column("prompt_override", Text, nullable=False, server_default=""),
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
    Column("chapter_id", Text, ForeignKey("chapter.id", ondelete="CASCADE"), nullable=False),
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
    # 与上一镜的衔接类型，见 app/schemas.py ShotTransitionFromPrev。
    # 存量数据默认 scene_change：不引用上一镜末帧是安全侧的选择。
    Column(
        "transition_from_prev",
        Text,
        nullable=False,
        server_default="scene_change",
    ),
    Column("frame_description", Text, nullable=False, server_default=""),
    Column("image_prompt", Text, nullable=False, server_default=""),
    Column("image_prompt_override", Text, nullable=False, server_default=""),
    Column("video_prompt", Text, nullable=False, server_default=""),
    Column("video_prompt_override", Text, nullable=False, server_default=""),
    Column("negative_prompt", Text, nullable=False, server_default=""),
    Column("reference_bindings_json", Text, nullable=False, server_default="[]"),
    Column("status", Text, nullable=False, server_default="draft"),
    Column("own_signature", Text, nullable=False, server_default=""),
    Column("revision", Integer, nullable=False, server_default="1"),
    Column("created_at", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
    CheckConstraint("source_type IN ('novel','screenplay')", name="source_type"),
    CheckConstraint("status IN ('draft','ready','confirmed','failed')", name="status"),
    UniqueConstraint("chapter_id", "shot_no", name="uq_shot_chapter_no"),
)
Index("idx_shot_chapter_order", shot.c.chapter_id, shot.c.shot_order)


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
    Column("end_frame_path", Text),
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
    Column("chapter_id", Text, ForeignKey("chapter.id", ondelete="CASCADE")),
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


audio_config = Table(
    "audio_config",
    metadata,
    Column("project_id", Text, ForeignKey("project.id", ondelete="CASCADE"), primary_key=True),
    Column("tts_provider_id", Text, ForeignKey("api_provider.id", ondelete="SET NULL")),
    Column("bgm_volume", Float, nullable=False, server_default="0.3"),
    Column("dialogue_volume", Float, nullable=False, server_default="1.0"),
    Column("sound_effect_volume", Float, nullable=False, server_default="0.5"),
    Column("updated_at", Text, nullable=False),
    CheckConstraint("bgm_volume >= 0 AND bgm_volume <= 1", name="bgm_volume"),
    CheckConstraint("dialogue_volume >= 0 AND dialogue_volume <= 1", name="dialogue_volume"),
    CheckConstraint("sound_effect_volume >= 0 AND sound_effect_volume <= 1", name="sound_effect_volume"),
)


shot_audio = Table(
    "shot_audio",
    metadata,
    Column("id", Text, primary_key=True),
    Column("shot_id", Text, ForeignKey("shot.id", ondelete="CASCADE"), nullable=False),
    Column("dialogue_text", Text, nullable=False),
    Column("speaker_name", Text, nullable=False, server_default=""),
    Column("voice_id", Text, nullable=False, server_default=""),
    Column("file_path", Text),
    Column("duration_sec", Float),
    Column("version_no", Integer, nullable=False),
    Column("provider_id", Text, ForeignKey("api_provider.id")),
    Column("model", Text, nullable=False, server_default=""),
    Column("status", Text, nullable=False, server_default="queued"),
    Column("is_current", Integer, nullable=False, server_default="0"),
    Column("source", Text, nullable=False, server_default="generated"),
    Column("error", Text, nullable=False, server_default=""),
    Column("created_at", Text, nullable=False),
    CheckConstraint(
        "status IN ('queued','running','completed','failed')",
        name="status",
    ),
    CheckConstraint("source IN ('generated','uploaded')", name="source"),
    UniqueConstraint("shot_id", "version_no", name="uq_shot_audio_shot_version"),
)
Index(
    "ux_shot_audio_current",
    shot_audio.c.shot_id,
    unique=True,
    sqlite_where=shot_audio.c.is_current == 1,
)


shot_subtitle = Table(
    "shot_subtitle",
    metadata,
    Column("id", Text, primary_key=True),
    Column("shot_id", Text, ForeignKey("shot.id", ondelete="CASCADE"), nullable=False),
    Column("text", Text, nullable=False),
    Column("start_time", Float, nullable=False),
    Column("end_time", Float, nullable=False),
    Column("style_preset", Text, nullable=False, server_default="default"),
    Column("created_at", Text, nullable=False),
    CheckConstraint("start_time >= 0 AND end_time > start_time", name="time_range"),
)
Index("idx_subtitle_shot", shot_subtitle.c.shot_id)


project_bgm = Table(
    "project_bgm",
    metadata,
    Column("id", Text, primary_key=True),
    Column("project_id", Text, ForeignKey("project.id", ondelete="CASCADE"), nullable=False),
    Column("name", Text, nullable=False),
    Column("file_path", Text, nullable=False),
    Column("duration_sec", Float),
    Column("mood_tags", Text, nullable=False, server_default="[]"),
    Column("created_at", Text, nullable=False),
)
Index("idx_bgm_project", project_bgm.c.project_id)


chapter_composition = Table(
    "chapter_composition",
    metadata,
    Column("id", Text, primary_key=True),
    Column("chapter_id", Text, ForeignKey("chapter.id", ondelete="CASCADE"), nullable=False),
    Column("job_id", Text, ForeignKey("job.id", ondelete="SET NULL")),
    Column("file_path", Text),
    Column("duration_sec", Float),
    Column("resolution", Text, nullable=False, server_default="1080p"),
    Column("include_audio", Integer, nullable=False, server_default="1"),
    Column("burn_subtitles", Integer, nullable=False, server_default="1"),
    Column("bgm_id", Text, ForeignKey("project_bgm.id", ondelete="SET NULL")),
    Column("status", Text, nullable=False, server_default="pending"),
    Column("error", Text, nullable=False, server_default=""),
    Column("created_at", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
    CheckConstraint(
        "status IN ('pending','processing','completed','failed')",
        name="status",
    ),
    CheckConstraint("resolution IN ('720p','1080p','4k')", name="resolution"),
)
Index(
    "ux_composition_chapter",
    chapter_composition.c.chapter_id,
    unique=True,
    sqlite_where=chapter_composition.c.status.in_(["pending", "processing"]),
)


ALL_TABLES = (
    project,
    chapter,
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
    audio_config,
    shot_audio,
    shot_subtitle,
    project_bgm,
    chapter_composition,
)
