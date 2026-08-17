from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import func, insert, inspect, select, text
from sqlalchemy.exc import IntegrityError

from alembic.config import Config
from alembic.script import ScriptDirectory
from app.config import BACKEND_DIR
from app.db import Database, migrate_schema, utc_now
from app.models.tables import chapter, job, metadata, project, workflow_run, workflow_stage

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


def test_migration_script_has_single_0016_head_and_expected_revision() -> None:
    script_directory = ScriptDirectory.from_config(_alembic_config())

    assert script_directory.get_heads() == ["20260816_0016_dynamic_job_idempotency"]
    revision = script_directory.get_revision("20260816_0016_dynamic_job_idempotency")
    assert revision is not None
    assert revision.revision == "20260816_0016_dynamic_job_idempotency"
    assert revision.down_revision == "20260816_0015"


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
                "20260816_0016_dynamic_job_idempotency"
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
                "20260816_0016_dynamic_job_idempotency"
            )
    finally:
        database.dispose()
