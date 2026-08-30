from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import func, insert, inspect, select, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from app.config import BACKEND_DIR
from app.db import Database, migrate_schema, utc_now
from app.models.tables import (
    api_provider,
    chapter,
    job,
    metadata,
    project,
    shot,
    shot_audio,
    workflow_run,
    workflow_stage,
)
from app.schemas import ProviderConfig

_WORKFLOW_STATUS_SQL = (
    "status IN ('pending','queued','running','blocked','ready','confirmed','stale',"
    "'partial_failed','failed','interrupted','cancelled')"
)


def _alembic_config() -> Config:
    config = Config()
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.set_main_option("prepend_sys_path", str(BACKEND_DIR))
    config.set_main_option("path_separator", "os")
    return config


def _schema_snapshot(database: Database) -> dict[str, tuple[str, ...]]:
    inspector = inspect(database.engine)
    return {
        table_name: tuple(column["name"] for column in inspector.get_columns(table_name))
        for table_name in sorted(inspector.get_table_names())
    }


def _table_sql(database: Database, table_name: str) -> str:
    with database.engine.connect() as connection:
        return connection.scalar(
            text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :table_name"),
            {"table_name": table_name},
        )


@pytest.fixture
def migrated_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SKIP_DATA_DELETION", "1")
    database = Database(f"sqlite:///{(tmp_path / 'migration.db').as_posix()}")
    try:
        migrate_schema(database)
        yield database
    finally:
        database.dispose()


def test_migration_script_has_single_0017_head_and_expected_revision() -> None:
    script_directory = ScriptDirectory.from_config(_alembic_config())

    assert script_directory.get_heads() == ["20260818_0017_dynamic_audio_current"]
    revision = script_directory.get_revision("20260818_0017_dynamic_audio_current")
    assert revision is not None
    assert revision.revision == "20260818_0017_dynamic_audio_current"
    assert revision.down_revision == "20260816_0016_dynamic_job_idempotency"


def test_fresh_migration_creates_workflow_tables_with_constraints(
    migrated_database: Database,
) -> None:
    inspector = inspect(migrated_database.engine)
    assert {"workflow_run", "workflow_stage"} <= set(inspector.get_table_names())

    workflow_run_columns = {
        column["name"]: column for column in inspector.get_columns("workflow_run")
    }
    assert {
        "id",
        "project_id",
        "chapter_id",
        "kind",
        "schema_version",
        "status",
        "current_stage",
        "source_kind",
        "source_snapshot_json",
        "config_snapshot_json",
        "asset_mapping_json",
        "input_signature",
        "revision",
        "error",
        "created_at",
        "started_at",
        "updated_at",
        "finished_at",
    } <= set(workflow_run_columns)
    assert workflow_run_columns["id"]["nullable"] is False
    assert workflow_run_columns["project_id"]["nullable"] is False
    assert workflow_run_columns["chapter_id"]["nullable"] is False
    assert workflow_run_columns["started_at"]["nullable"] is True

    workflow_stage_columns = {
        column["name"]: column for column in inspector.get_columns("workflow_stage")
    }
    assert {
        "id",
        "workflow_run_id",
        "stage_key",
        "ordinal",
        "status",
        "input_signature",
        "raw_output_json",
        "normalized_output_json",
        "draft_output_json",
        "output_refs_json",
        "gate_result_json",
        "attempt",
        "error",
        "created_at",
        "started_at",
        "updated_at",
        "finished_at",
        "confirmed_at",
        "confirmed_revision",
    } <= set(workflow_stage_columns)
    assert workflow_stage_columns["id"]["nullable"] is False
    assert workflow_stage_columns["workflow_run_id"]["nullable"] is False
    assert workflow_stage_columns["stage_key"]["nullable"] is False
    assert workflow_stage_columns["attempt"]["nullable"] is False

    run_foreign_keys = {
        (
            tuple(foreign_key["constrained_columns"]),
            foreign_key["referred_table"],
            tuple(foreign_key["referred_columns"]),
            (foreign_key.get("options") or {}).get("ondelete"),
        )
        for foreign_key in inspector.get_foreign_keys("workflow_run")
    }
    assert run_foreign_keys == {
        (("project_id",), "project", ("id",), "CASCADE"),
        (("chapter_id",), "chapter", ("id",), "CASCADE"),
    }

    stage_foreign_keys = {
        (
            tuple(foreign_key["constrained_columns"]),
            foreign_key["referred_table"],
            tuple(foreign_key["referred_columns"]),
            (foreign_key.get("options") or {}).get("ondelete"),
        )
        for foreign_key in inspector.get_foreign_keys("workflow_stage")
    }
    assert stage_foreign_keys == {
        (("workflow_run_id",), "workflow_run", ("id",), "CASCADE"),
    }

    run_sql = _table_sql(migrated_database, "workflow_run")
    stage_sql = _table_sql(migrated_database, "workflow_stage")
    assert "ck_workflow_run_status" in run_sql
    assert _WORKFLOW_STATUS_SQL.replace(" ", "") in run_sql.replace(" ", "")
    assert "ck_workflow_stage_status" in stage_sql
    assert _WORKFLOW_STATUS_SQL.replace(" ", "") in stage_sql.replace(" ", "")

    unique_constraints = {
        (item["name"], tuple(item["column_names"]))
        for item in inspector.get_unique_constraints("workflow_stage")
    }
    assert (
        "uq_workflow_stage_run_key_attempt",
        ("workflow_run_id", "stage_key", "attempt"),
    ) in unique_constraints
    indexes = {item["name"]: item for item in inspector.get_indexes("workflow_stage")}
    assert indexes["idx_workflow_stage_run_status"]["column_names"] == [
        "workflow_run_id",
        "status",
    ]


@pytest.mark.parametrize(
    ("table_name", "expected_columns"),
    [
        (
            "job",
            {
                "workflow_run_id",
                "workflow_stage_id",
                "idempotency_key",
                "input_signature",
                "depends_on_json",
            },
        ),
        ("asset", {"visual_anchor_json"}),
        ("asset_image", {"prompt_ir_json", "provenance_json", "reference_purpose", "approved"}),
        (
            "shot",
            {
                "external_key",
                "action_events_json",
                "dialogue_cues_json",
                "sound_cues_json",
                "continuity_json",
                "pipeline_metadata_json",
            },
        ),
        ("keyframe_version", {"prompt_ir_json", "provenance_json"}),
        ("video_version", {"prompt_ir_json", "provenance_json"}),
        (
            "shot_audio",
            {
                "dialogue_line_id",
                "line_order",
                "source_text_snapshot",
                "input_signature",
                "provenance_json",
            },
        ),
        ("shot_subtitle", {"dialogue_line_id", "audio_id", "provenance_json"}),
        (
            "chapter_composition",
            {"workflow_run_id", "input_signature", "preflight_json", "provenance_json"},
        ),
    ],
)
def test_fresh_migration_adds_0015_columns_to_existing_tables(
    migrated_database: Database,
    table_name: str,
    expected_columns: set[str],
) -> None:
    inspector = inspect(migrated_database.engine)
    assert table_name in inspector.get_table_names()
    actual_columns = {column["name"] for column in inspector.get_columns(table_name)}

    assert expected_columns <= actual_columns


def test_fresh_migration_creates_0016_job_indexes_and_enforces_idempotency(
    migrated_database: Database,
) -> None:
    inspector = inspect(migrated_database.engine)
    indexes = {item["name"]: item for item in inspector.get_indexes("job")}

    assert indexes["idx_job_workflow_run_status"]["column_names"] == [
        "workflow_run_id",
        "status",
    ]
    assert not indexes["idx_job_workflow_run_status"]["unique"]
    assert indexes["idx_job_workflow_stage_status"]["column_names"] == [
        "workflow_stage_id",
        "status",
    ]
    assert not indexes["idx_job_workflow_stage_status"]["unique"]

    idempotency_index = indexes["ux_job_idempotency_key"]
    assert idempotency_index["column_names"] == ["idempotency_key"]
    assert idempotency_index["unique"]
    dialect_options = idempotency_index.get("dialect_options") or {}
    partial_where = dialect_options.get("sqlite_where")
    assert str(partial_where).replace(" ", "") == ("idempotency_keyISNOTNULLANDidempotency_key<>''")

    now = utc_now()
    with migrated_database.engine.begin() as connection:
        connection.execute(
            insert(project).values(
                id="project-job-index",
                name="任务索引测试项目",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(job).values(
                id="job-idempotency-key",
                project_id="project-job-index",
                job_type="test",
                idempotency_key="same-key",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(job).values(
                id="job-empty-key-1",
                project_id="project-job-index",
                job_type="test",
                idempotency_key="",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(job).values(
                id="job-empty-key-2",
                project_id="project-job-index",
                job_type="test",
                idempotency_key="",
                created_at=now,
                updated_at=now,
            )
        )

    with pytest.raises(IntegrityError):
        with migrated_database.engine.begin() as connection:
            connection.execute(
                insert(job).values(
                    id="job-duplicate-key",
                    project_id="project-job-index",
                    job_type="test",
                    idempotency_key="same-key",
                    created_at=now,
                    updated_at=now,
                )
            )


def test_database_initialize_and_repeated_migrate_schema_are_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SKIP_DATA_DELETION", "1")
    initialized_database = Database(f"sqlite:///{(tmp_path / 'initialize.db').as_posix()}")
    try:
        initialized_database.initialize(seed_mock_providers=False)
        first_initialize_snapshot = _schema_snapshot(initialized_database)
        initialized_database.initialize(seed_mock_providers=False)
        second_initialize_snapshot = _schema_snapshot(initialized_database)

        assert first_initialize_snapshot == second_initialize_snapshot
        assert set(first_initialize_snapshot) == set(metadata.tables)
        with initialized_database.engine.connect() as connection:
            assert connection.scalar(text("SELECT COUNT(*) FROM api_provider")) == 0
    finally:
        initialized_database.dispose()

    migrated_database = Database(f"sqlite:///{(tmp_path / 'repeat-migration.db').as_posix()}")
    try:
        migrate_schema(migrated_database)
        first_migration_snapshot = _schema_snapshot(migrated_database)
        migrate_schema(migrated_database)
        second_migration_snapshot = _schema_snapshot(migrated_database)

        assert first_migration_snapshot == second_migration_snapshot
        with migrated_database.engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "20260818_0017_dynamic_audio_current"
            )
    finally:
        migrated_database.dispose()


def test_workflow_rows_enforce_foreign_keys_unique_attempt_and_cascade(
    migrated_database: Database,
) -> None:
    now = utc_now()
    with migrated_database.engine.connect() as connection:
        assert connection.scalar(text("PRAGMA foreign_keys")) == 1

    with migrated_database.engine.begin() as connection:
        connection.execute(
            insert(project).values(
                id="project-workflow",
                name="工作流测试项目",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(chapter).values(
                id="chapter-workflow",
                project_id="project-workflow",
                chapter_no=1,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(workflow_run).values(
                id="run-workflow",
                project_id="project-workflow",
                chapter_id="chapter-workflow",
                source_kind="full_text",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(workflow_stage).values(
                id="stage-workflow",
                workflow_run_id="run-workflow",
                stage_key="extract",
                ordinal=1,
                attempt=1,
                created_at=now,
                updated_at=now,
            )
        )

    with migrated_database.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(project)) == 1
        assert connection.scalar(select(func.count()).select_from(chapter)) == 1
        assert connection.scalar(select(func.count()).select_from(workflow_run)) == 1
        assert connection.scalar(select(func.count()).select_from(workflow_stage)) == 1

    with pytest.raises(IntegrityError):
        with migrated_database.engine.begin() as connection:
            connection.execute(
                insert(workflow_stage).values(
                    id="stage-workflow-duplicate",
                    workflow_run_id="run-workflow",
                    stage_key="extract",
                    ordinal=1,
                    attempt=1,
                    created_at=now,
                    updated_at=now,
                )
            )

    with migrated_database.engine.begin() as connection:
        connection.execute(workflow_run.delete().where(workflow_run.c.id == "run-workflow"))

    with migrated_database.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(workflow_run)) == 0
        assert connection.scalar(select(func.count()).select_from(workflow_stage)) == 0


def _create_minimal_0003_database(database_path: Path) -> tuple[str, str, str]:
    legacy_row = ("legacy-project", "旧项目", "legacy-deleted")
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE alembic_version (
                version_num VARCHAR(32) NOT NULL PRIMARY KEY
            );
            INSERT INTO alembic_version (version_num) VALUES ('20260730_0003');
            CREATE TABLE project (
                id TEXT NOT NULL PRIMARY KEY,
                name TEXT NOT NULL,
                deleted_at TEXT
            );
            """
        )
        connection.execute(
            "INSERT INTO project (id, name, deleted_at) VALUES (?, ?, ?)",
            legacy_row,
        )
    return legacy_row


def test_migrate_schema_from_minimal_0003_database_preserves_project_data_and_adds_0015(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SKIP_DATA_DELETION", "1")
    database_path = tmp_path / "legacy-0003.db"
    legacy_row = _create_minimal_0003_database(database_path)
    database = Database(f"sqlite:///{database_path.as_posix()}")
    try:
        migrate_schema(database)

        inspector = inspect(database.engine)
        assert {"workflow_run", "workflow_stage"} <= set(inspector.get_table_names())
        assert {
            "id",
            "project_id",
            "chapter_id",
            "status",
            "source_kind",
        } <= {column["name"] for column in inspector.get_columns("workflow_run")}
        assert {
            "id",
            "workflow_run_id",
            "stage_key",
            "attempt",
            "status",
        } <= {column["name"] for column in inspector.get_columns("workflow_stage")}
        assert {
            "workflow_run_id",
            "input_signature",
            "preflight_json",
            "provenance_json",
        } <= {column["name"] for column in inspector.get_columns("chapter_composition")}

        with database.engine.connect() as connection:
            preserved_row = connection.execute(
                text("SELECT id, name, deleted_at FROM project WHERE id = :project_id"),
                {"project_id": legacy_row[0]},
            ).one()
            assert tuple(preserved_row) == legacy_row
            assert (
                connection.scalar(
                    text("SELECT storage_name FROM project WHERE id = :project_id"),
                    {"project_id": legacy_row[0]},
                )
                == legacy_row[1]
            )
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "20260818_0017_dynamic_audio_current"
            )
    finally:
        database.dispose()


def test_fresh_migration_creates_0017_dynamic_audio_current_index(
    migrated_database: Database,
) -> None:
    inspector = inspect(migrated_database.engine)
    indexes = {item["name"]: item for item in inspector.get_indexes("shot_audio")}

    assert "ux_shot_audio_current" not in indexes
    current_index = indexes["ux_shot_audio_current_line"]
    assert current_index["column_names"] == ["shot_id", "dialogue_line_id"]
    assert current_index["unique"]
    dialect_options = current_index.get("dialect_options") or {}
    partial_where = dialect_options.get("sqlite_where")
    assert str(partial_where).replace(" ", "") == "is_current=1"

    now = utc_now()
    with migrated_database.engine.begin() as connection:
        connection.execute(
            insert(api_provider).values(
                id="tts-migration-provider",
                kind="tts",
                name="迁移 TTS Provider",
                adapter="mock",
                created_at=now,
                updated_at=now,
            )
        )
        assert connection.scalar(
            select(api_provider.c.kind).where(api_provider.c.id == "tts-migration-provider")
        ) == "tts"


def test_0017_downgrade_collapses_multi_dialogue_current_and_keeps_null_legacy_row(
    migrated_database: Database,
) -> None:
    now = utc_now()
    with migrated_database.engine.begin() as connection:
        connection.execute(
            insert(project).values(
                id="project-audio-downgrade",
                name="音频降级测试项目",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(chapter).values(
                id="chapter-audio-downgrade",
                project_id="project-audio-downgrade",
                chapter_no=1,
                title="降级章节",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(shot).values(
                id="shot-audio-downgrade",
                project_id="project-audio-downgrade",
                chapter_id="chapter-audio-downgrade",
                shot_no=1,
                shot_order=1,
                source_type="novel",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(shot_audio),
            [
                {
                    "id": "audio-legacy-null-current",
                    "shot_id": "shot-audio-downgrade",
                    "dialogue_line_id": None,
                    "dialogue_text": "legacy",
                    "version_no": 1,
                    "status": "completed",
                    "is_current": 1,
                    "source": "generated",
                    "created_at": now,
                },
                {
                    "id": "audio-line-one-current",
                    "shot_id": "shot-audio-downgrade",
                    "dialogue_line_id": "line-1",
                    "dialogue_text": "line one",
                    "version_no": 2,
                    "status": "completed",
                    "is_current": 1,
                    "source": "generated",
                    "created_at": now,
                },
                {
                    "id": "audio-line-two-current",
                    "shot_id": "shot-audio-downgrade",
                    "dialogue_line_id": "line-2",
                    "dialogue_text": "line two",
                    "version_no": 3,
                    "status": "completed",
                    "is_current": 1,
                    "source": "generated",
                    "created_at": now,
                },
            ],
        )

    alembic_config = _alembic_config()
    with migrated_database.engine.begin() as connection:
        alembic_config.attributes["connection"] = connection
        command.downgrade(
            alembic_config,
            "20260816_0016_dynamic_job_idempotency",
        )

    inspector = inspect(migrated_database.engine)
    indexes = {item["name"] for item in inspector.get_indexes("shot_audio")}
    with migrated_database.engine.connect() as connection:
        current_rows = connection.execute(
            select(shot_audio.c.id, shot_audio.c.dialogue_line_id).where(
                shot_audio.c.shot_id == "shot-audio-downgrade",
                shot_audio.c.is_current == 1,
            )
        ).all()
    assert "ux_shot_audio_current" in indexes
    assert [(row.id, row.dialogue_line_id) for row in current_rows] == [
        ("audio-legacy-null-current", None)
    ]

    with pytest.raises(IntegrityError):
        with migrated_database.engine.begin() as connection:
            connection.execute(
                insert(shot_audio).values(
                    id="audio-downgrade-new-current",
                    shot_id="shot-audio-downgrade",
                    dialogue_line_id="line-3",
                    dialogue_text="line three",
                    version_no=4,
                    status="completed",
                    is_current=1,
                    source="generated",
                    created_at=now,
                )
            )


def test_provider_config_accepts_tts_kind() -> None:
    provider = ProviderConfig.model_validate(
        {
            "id": "tts-provider",
            "kind": "tts",
            "adapter": "edge_tts",
        }
    )

    assert provider.kind == "tts"


def test_0017_upgrade_cleans_stale_sqlite_batch_table_and_can_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SKIP_DATA_DELETION", "1")
    database = Database(f"sqlite:///{(tmp_path / 'stale-batch.db').as_posix()}")
    try:
        config = _alembic_config()
        with database.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "20260816_0016_dynamic_job_idempotency")
            connection.execute(
                text(
                    "CREATE TABLE provider_reference ("
                    "id TEXT PRIMARY KEY, "
                    "provider_id TEXT NOT NULL REFERENCES api_provider(id)"
                    ")"
                )
            )
            connection.execute(
                insert(api_provider).values(
                    id="provider-before-retry",
                    kind="image",
                    name="迁移重试引用 Provider",
                    adapter="mock",
                    created_at=utc_now(),
                    updated_at=utc_now(),
                )
            )
            connection.execute(
                text(
                    "INSERT INTO provider_reference (id, provider_id) "
                    "VALUES ('reference-before-retry', 'provider-before-retry')"
                )
            )
            connection.execute(
                text(
                    "CREATE TABLE _alembic_tmp_api_provider "
                    "(id TEXT NOT NULL PRIMARY KEY)"
                )
            )

        migrate_schema(database)

        with database.engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "20260818_0017_dynamic_audio_current"
            )
            assert connection.scalar(
                text(
                    "SELECT COUNT(*) FROM sqlite_master "
                    "WHERE type = 'table' AND name = '_alembic_tmp_api_provider'"
                )
            ) == 0
            # 断言 FK 引用在 api_provider 重建后仍指向原 provider：
            # id 是 reference-before-retry，provider_id 才是 provider-before-retry。
            assert connection.scalar(
                text(
                    "SELECT provider_id FROM provider_reference "
                    "WHERE id = 'reference-before-retry'"
                )
            ) == "provider-before-retry"
    finally:
        database.dispose()


def test_audio_index_migration_preserves_rows(
    migrated_database: Database,
) -> None:
    """0017 索引升级（ux_shot_audio_current → ux_shot_audio_current_line）
    必须保留全部 shot_audio 行，且升级后同 (shot_id, dialogue_line_id) 至多
    1 行 is_current=1；总 current 行数不应超过总 shot 数。
    """
    now = utc_now()
    with migrated_database.engine.begin() as connection:
        connection.execute(
            insert(project).values(
                id="project-audio-index",
                name="音频索引保留测试项目",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(chapter).values(
                id="chapter-audio-index",
                project_id="project-audio-index",
                chapter_no=1,
                title="索引保留章节",
                created_at=now,
                updated_at=now,
            )
        )
        for shot_id, shot_no in (
            ("shot-audio-index-a", 1),
            ("shot-audio-index-b", 2),
        ):
            connection.execute(
                insert(shot).values(
                    id=shot_id,
                    project_id="project-audio-index",
                    chapter_id="chapter-audio-index",
                    shot_no=shot_no,
                    shot_order=shot_no,
                    source_type="novel",
                    created_at=now,
                    updated_at=now,
                )
            )

    # 先回退到 0016 schema（仍带 legacy 索引 ux_shot_audio_current）。
    alembic_config = _alembic_config()
    with migrated_database.engine.begin() as connection:
        alembic_config.attributes["connection"] = connection
        command.downgrade(alembic_config, "20260816_0016_dynamic_job_idempotency")

    # 0017 upgrade 内的 _verify_referenced_rows 会扫所有 shot_audio 行（即便
    # shot_audio 没有 tts_provider_id 列，SQLite 对未知列的 WHERE 仍会判定为
    # 命中）。为避免触发其 "providers=0 but references>0" 防御性检查，
    # 提前插入一个占位 api_provider。
    with migrated_database.engine.begin() as connection:
        connection.execute(
            insert(api_provider).values(
                id="provider-audio-index-dummy",
                kind="text",
                name="音频索引保留测试占位",
                adapter="mock",
                created_at=now,
                updated_at=now,
            )
        )

    # 模拟「旧 schema（无 line-aware 唯一索引）」场景：手动 DROP legacy 索引，
    # 留下无唯一约束的 shot_audio 表，从而能自由写入多 current 行。
    with migrated_database.engine.begin() as connection:
        connection.execute(text("DROP INDEX IF EXISTS ux_shot_audio_current"))

    # 在旧 schema 下插入 3 行：2 行同 shot 但不同 dialogue_line_id 且 is_current=1，
    # 1 行不同 shot 且 is_current=0。
    with migrated_database.engine.begin() as connection:
        connection.execute(
            insert(shot_audio),
            [
                {
                    "id": "audio-idx-line-1",
                    "shot_id": "shot-audio-index-a",
                    "dialogue_line_id": "line-1",
                    "dialogue_text": "first line",
                    "version_no": 1,
                    "status": "completed",
                    "is_current": 1,
                    "source": "generated",
                    "created_at": now,
                },
                {
                    "id": "audio-idx-line-2",
                    "shot_id": "shot-audio-index-a",
                    "dialogue_line_id": "line-2",
                    "dialogue_text": "second line",
                    "version_no": 2,
                    "status": "completed",
                    "is_current": 1,
                    "source": "generated",
                    "created_at": now,
                },
                {
                    "id": "audio-idx-other-shot",
                    "shot_id": "shot-audio-index-b",
                    "dialogue_line_id": "line-1",
                    "dialogue_text": "other shot line",
                    "version_no": 1,
                    "status": "completed",
                    "is_current": 0,
                    "source": "generated",
                    "created_at": now,
                },
            ],
        )

    # 升级到 0017：新唯一索引 (shot_id, dialogue_line_id) WHERE is_current=1。
    with migrated_database.engine.begin() as connection:
        alembic_config.attributes["connection"] = connection
        command.upgrade(alembic_config, "head")

    with migrated_database.engine.connect() as connection:
        rows = connection.execute(
            select(
                shot_audio.c.id,
                shot_audio.c.shot_id,
                shot_audio.c.dialogue_line_id,
                shot_audio.c.is_current,
            )
            .where(shot_audio.c.shot_id.in_(["shot-audio-index-a", "shot-audio-index-b"]))
            .order_by(shot_audio.c.id)
        ).all()

    # 1) 升级后 3 行都还在。
    assert {row.id for row in rows} == {
        "audio-idx-line-1",
        "audio-idx-line-2",
        "audio-idx-other-shot",
    }

    # 2) 同 (shot_id, dialogue_line_id) 至多 1 行 is_current=1。
    keys: set[tuple[str, str]] = set()
    for row in rows:
        line_id = row.dialogue_line_id if row.dialogue_line_id is not None else ""
        if row.is_current == 1:
            assert (row.shot_id, line_id) not in keys
            keys.add((row.shot_id, line_id))

    # 3) 总 current 数 ≤ 总 shot 数。
    current_count = sum(1 for row in rows if row.is_current == 1)
    distinct_shots = {row.shot_id for row in rows}
    assert current_count <= len(distinct_shots)
