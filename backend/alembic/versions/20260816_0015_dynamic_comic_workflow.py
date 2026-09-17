"""动态漫工作流运行与阶段数据结构

Revision ID: 20260816_0015
Revises: 20260814_0014
Create Date: 2026-08-16

新增 workflow_run / workflow_stage，并为现有任务、媒体、分镜、音频、字幕和
合成表补充动态工作流所需的可空关联、签名与 provenance 字段。存量数据不回填。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260816_0015"
down_revision: str | None = "20260814_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_WORKFLOW_RUN_STATUS = (
    "'pending','queued','running','blocked','ready','confirmed','stale',"
    "'partial_failed','failed','interrupted','cancelled'"
)
_WORKFLOW_STAGE_STATUS = _WORKFLOW_RUN_STATUS


def _has_table(table: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table in inspector.get_table_names()


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


def _foreign_key_target_tables(column: sa.Column) -> set[str]:
    return {foreign_key._column_tokens[1] for foreign_key in column.foreign_keys}


def _sqlite_has_missing_foreign_key_target(table: str, column: sa.Column) -> bool:
    inspector = sa.inspect(op.get_bind())
    table_names = set(inspector.get_table_names())
    target_tables = _foreign_key_target_tables(column)
    target_tables.update(
        foreign_key["referred_table"] for foreign_key in inspector.get_foreign_keys(table)
    )
    return any(target_table not in table_names for target_table in target_tables)


def _copy_column_without_foreign_keys(column: sa.Column) -> sa.Column:
    return sa.Column(
        column.name,
        column.type,
        nullable=True,
        server_default=column.server_default,
    )


def _add_column(table: str, column: sa.Column) -> None:
    if _has_table(table) and not _has_column(table, column.name):
        if column.foreign_keys and op.get_bind().dialect.name == "sqlite":
            if _sqlite_has_missing_foreign_key_target(table, column):
                op.add_column(table, _copy_column_without_foreign_keys(column))
            else:
                with op.batch_alter_table(table, recreate="always") as batch:
                    batch.add_column(column)
        else:
            op.add_column(table, column)


def _drop_column(table: str, column: str) -> None:
    if _has_column(table, column):
        with op.batch_alter_table(table) as batch:
            batch.drop_column(column)


def _create_index(
    name: str,
    table: str,
    columns: list[str],
    *,
    unique: bool = False,
) -> None:
    if _has_table(table) and not _has_index(table, name):
        op.create_index(name, table, columns, unique=unique)


def _drop_index(table: str, index: str) -> None:
    if _has_index(table, index):
        op.drop_index(index, table_name=table)


def _create_workflow_tables() -> None:
    if not _has_table("workflow_run"):
        op.create_table(
            "workflow_run",
            sa.Column("id", sa.Text(), nullable=False),
            sa.Column("project_id", sa.Text(), nullable=False),
            sa.Column("chapter_id", sa.Text(), nullable=False),
            sa.Column(
                "kind",
                sa.Text(),
                nullable=False,
                server_default="dynamic_comic_0_6",
            ),
            sa.Column("schema_version", sa.Text(), nullable=False, server_default="1"),
            sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
            sa.Column("current_stage", sa.Text(), nullable=False, server_default=""),
            sa.Column("source_kind", sa.Text(), nullable=False),
            sa.Column("source_snapshot_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("config_snapshot_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("asset_mapping_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("input_signature", sa.Text(), nullable=False, server_default=""),
            sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("error", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.Text(), nullable=False),
            sa.Column("started_at", sa.Text(), nullable=True),
            sa.Column("updated_at", sa.Text(), nullable=False),
            sa.Column("finished_at", sa.Text(), nullable=True),
            sa.CheckConstraint(
                "kind IN ('dynamic_comic_0_6')",
                name="ck_workflow_run_kind",
            ),
            sa.CheckConstraint(
                f"status IN ({_WORKFLOW_RUN_STATUS})",
                name="ck_workflow_run_status",
            ),
            sa.CheckConstraint(
                "source_kind IN ('full_text','synopsis')",
                name="ck_workflow_run_source_kind",
            ),
            sa.ForeignKeyConstraint(
                ["project_id"],
                ["project.id"],
                name="fk_workflow_run_project_id_project",
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["chapter_id"],
                ["chapter.id"],
                name="fk_workflow_run_chapter_id_chapter",
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id", name="pk_workflow_run"),
        )

    if not _has_table("workflow_stage"):
        op.create_table(
            "workflow_stage",
            sa.Column("id", sa.Text(), nullable=False),
            sa.Column("workflow_run_id", sa.Text(), nullable=False),
            sa.Column("stage_key", sa.Text(), nullable=False),
            sa.Column("ordinal", sa.Integer(), nullable=False),
            sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
            sa.Column("input_signature", sa.Text(), nullable=False, server_default=""),
            sa.Column("raw_output_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("normalized_output_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("draft_output_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("output_refs_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("gate_result_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("error", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.Text(), nullable=False),
            sa.Column("started_at", sa.Text(), nullable=True),
            sa.Column("updated_at", sa.Text(), nullable=False),
            sa.Column("finished_at", sa.Text(), nullable=True),
            sa.Column("confirmed_at", sa.Text(), nullable=True),
            sa.Column("confirmed_revision", sa.Integer(), nullable=True),
            sa.CheckConstraint(
                f"status IN ({_WORKFLOW_STAGE_STATUS})",
                name="ck_workflow_stage_status",
            ),
            sa.ForeignKeyConstraint(
                ["workflow_run_id"],
                ["workflow_run.id"],
                name="fk_workflow_stage_workflow_run_id_workflow_run",
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id", name="pk_workflow_stage"),
            sa.UniqueConstraint(
                "workflow_run_id",
                "stage_key",
                "attempt",
                name="uq_workflow_stage_run_key_attempt",
            ),
        )


def upgrade() -> None:
    _create_workflow_tables()

    _create_index("idx_workflow_run_project_status", "workflow_run", ["project_id", "status"])
    _create_index("idx_workflow_run_chapter_status", "workflow_run", ["chapter_id", "status"])
    _create_index("idx_workflow_run_current_stage", "workflow_run", ["current_stage"])
    _create_index("idx_workflow_stage_run_status", "workflow_stage", ["workflow_run_id", "status"])
    _create_index("idx_workflow_stage_run_key", "workflow_stage", ["workflow_run_id", "stage_key"])
    _create_index("idx_workflow_stage_workflow_run_id", "workflow_stage", ["workflow_run_id"])
    _create_index("idx_workflow_stage_status", "workflow_stage", ["status"])
    _create_index("idx_workflow_stage_stage_key", "workflow_stage", ["stage_key"])

    _add_column(
        "job",
        sa.Column(
            "workflow_run_id",
            sa.Text(),
            sa.ForeignKey("workflow_run.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    _add_column(
        "job",
        sa.Column(
            "workflow_stage_id",
            sa.Text(),
            sa.ForeignKey("workflow_stage.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    _add_column("job", sa.Column("idempotency_key", sa.Text(), nullable=True))
    _add_column("job", sa.Column("input_signature", sa.Text(), nullable=True))
    _add_column("job", sa.Column("depends_on_json", sa.Text(), nullable=True))

    for table in ("asset_image", "keyframe_version", "video_version"):
        _add_column(table, sa.Column("prompt_ir_json", sa.Text(), nullable=True))
        _add_column(table, sa.Column("provenance_json", sa.Text(), nullable=True))

    _add_column(
        "asset_image",
        sa.Column("reference_purpose", sa.Text(), nullable=False, server_default=""),
    )
    _add_column(
        "asset_image",
        sa.Column("approved", sa.Integer(), nullable=False, server_default="0"),
    )
    _add_column(
        "asset",
        sa.Column("visual_anchor_json", sa.Text(), nullable=False, server_default="{}"),
    )

    _add_column("shot", sa.Column("external_key", sa.Text(), nullable=True))
    _add_column(
        "shot",
        sa.Column("action_events_json", sa.Text(), nullable=False, server_default="[]"),
    )
    _add_column(
        "shot",
        sa.Column("dialogue_cues_json", sa.Text(), nullable=False, server_default="[]"),
    )
    _add_column(
        "shot",
        sa.Column("sound_cues_json", sa.Text(), nullable=False, server_default="[]"),
    )
    _add_column(
        "shot",
        sa.Column("continuity_json", sa.Text(), nullable=False, server_default="{}"),
    )
    _add_column(
        "shot",
        sa.Column("pipeline_metadata_json", sa.Text(), nullable=False, server_default="{}"),
    )

    _add_column("shot_audio", sa.Column("dialogue_line_id", sa.Text(), nullable=True))
    _add_column(
        "shot_audio",
        sa.Column("line_order", sa.Integer(), nullable=False, server_default="0"),
    )
    _add_column(
        "shot_audio",
        sa.Column("source_text_snapshot", sa.Text(), nullable=False, server_default=""),
    )
    _add_column(
        "shot_audio",
        sa.Column("input_signature", sa.Text(), nullable=False, server_default=""),
    )
    _add_column(
        "shot_audio",
        sa.Column("provenance_json", sa.Text(), nullable=False, server_default="{}"),
    )

    _add_column("shot_subtitle", sa.Column("dialogue_line_id", sa.Text(), nullable=True))
    _add_column("shot_subtitle", sa.Column("audio_id", sa.Text(), nullable=True))
    _add_column(
        "shot_subtitle",
        sa.Column("provenance_json", sa.Text(), nullable=False, server_default="{}"),
    )

    _add_column(
        "chapter_composition",
        sa.Column(
            "workflow_run_id",
            sa.Text(),
            sa.ForeignKey("workflow_run.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    _add_column(
        "chapter_composition",
        sa.Column("input_signature", sa.Text(), nullable=False, server_default=""),
    )
    _add_column(
        "chapter_composition",
        sa.Column("preflight_json", sa.Text(), nullable=False, server_default="{}"),
    )
    _add_column(
        "chapter_composition",
        sa.Column("provenance_json", sa.Text(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    for table, column in (
        ("chapter_composition", "provenance_json"),
        ("chapter_composition", "preflight_json"),
        ("chapter_composition", "input_signature"),
        ("chapter_composition", "workflow_run_id"),
        ("shot_subtitle", "provenance_json"),
        ("shot_subtitle", "audio_id"),
        ("shot_subtitle", "dialogue_line_id"),
        ("shot_audio", "provenance_json"),
        ("shot_audio", "input_signature"),
        ("shot_audio", "source_text_snapshot"),
        ("shot_audio", "line_order"),
        ("shot_audio", "dialogue_line_id"),
        ("shot", "pipeline_metadata_json"),
        ("shot", "continuity_json"),
        ("shot", "sound_cues_json"),
        ("shot", "dialogue_cues_json"),
        ("shot", "action_events_json"),
        ("shot", "external_key"),
        ("asset", "visual_anchor_json"),
        ("asset_image", "approved"),
        ("asset_image", "reference_purpose"),
        ("video_version", "provenance_json"),
        ("video_version", "prompt_ir_json"),
        ("keyframe_version", "provenance_json"),
        ("keyframe_version", "prompt_ir_json"),
        ("asset_image", "provenance_json"),
        ("asset_image", "prompt_ir_json"),
        ("job", "depends_on_json"),
        ("job", "input_signature"),
        ("job", "idempotency_key"),
        ("job", "workflow_stage_id"),
        ("job", "workflow_run_id"),
    ):
        _drop_column(table, column)

    _drop_index("workflow_stage", "idx_workflow_stage_stage_key")
    _drop_index("workflow_stage", "idx_workflow_stage_status")
    _drop_index("workflow_stage", "idx_workflow_stage_workflow_run_id")
    _drop_index("workflow_stage", "idx_workflow_stage_run_key")
    _drop_index("workflow_stage", "idx_workflow_stage_run_status")
    _drop_index("workflow_run", "idx_workflow_run_current_stage")
    _drop_index("workflow_run", "idx_workflow_run_chapter_status")
    _drop_index("workflow_run", "idx_workflow_run_project_status")

    if _has_table("workflow_stage"):
        op.drop_table("workflow_stage")
    if _has_table("workflow_run"):
        op.drop_table("workflow_run")
