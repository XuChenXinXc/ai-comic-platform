from __future__ import annotations

import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import func, insert, inspect, select, text
from sqlalchemy.exc import IntegrityError

from app.config import BACKEND_DIR, PROJECT_ROOT, Settings
from app.db import Database, init_db, migrate_schema, utc_now
from app.models.tables import ALL_TABLES, api_provider, job, project
from app.schemas import NovelAnalysisOutput, ReferenceBinding


def _alembic_config() -> Config:
    config = Config()
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.set_main_option("prepend_sys_path", str(BACKEND_DIR))
    config.set_main_option("path_separator", "os")
    return config


def _insert_project(database: Database, project_id: str = "project-1") -> None:
    now = utc_now()
    with database.engine.begin() as connection:
        connection.execute(
            insert(project).values(
                id=project_id,
                name="测试项目",
                created_at=now,
                updated_at=now,
            )
        )


def test_settings_with_relative_workspace_resolves_from_project_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_WORKSPACE", "relative-workspace")
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)

    settings = Settings(_env_file=None)

    assert settings.workspace_dir == (PROJECT_ROOT / "relative-workspace").resolve()
    assert settings.database_url == (
        f"sqlite:///{(PROJECT_ROOT / 'relative-workspace' / 'platform.db').as_posix()}"
    )


def test_settings_with_relative_projects_dir_resolves_from_project_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_PROJECTS_DIR", "comic-projects")

    settings = Settings(_env_file=None)

    assert settings.projects_dir == (PROJECT_ROOT / "comic-projects").resolve()


def test_settings_without_projects_dir_uses_workspace_projects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_WORKSPACE", "relative-workspace")
    monkeypatch.delenv("APP_PROJECTS_DIR", raising=False)

    settings = Settings(_env_file=None)

    assert settings.projects_dir == (PROJECT_ROOT / "relative-workspace" / "projects").resolve()


def test_settings_with_empty_projects_dir_uses_workspace_projects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_WORKSPACE", "relative-workspace")
    monkeypatch.setenv("APP_PROJECTS_DIR", "")

    settings = Settings(_env_file=None)

    assert settings.projects_dir == (PROJECT_ROOT / "relative-workspace" / "projects").resolve()


@pytest.mark.parametrize(
    "projects_dir",
    [".", "C:/", "backend", "backend/app", "frontend", ".git"],
)
def test_settings_with_unsafe_projects_dir_rejects_value(
    monkeypatch: pytest.MonkeyPatch,
    projects_dir: str,
) -> None:
    monkeypatch.setenv("APP_PROJECTS_DIR", projects_dir)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.skipif(os.name != "nt", reason="仅适用于 Windows 路径限制")
def test_settings_with_projects_dir_too_deep_rejects_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_PROJECTS_DIR", str(PROJECT_ROOT / ("深" * 140)))

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_database_initialize_creates_ten_tables_and_three_mock_providers(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'platform.db').as_posix()}")
    database.initialize()
    database.initialize()

    assert set(inspect(database.engine).get_table_names()) == {table.name for table in ALL_TABLES}
    with database.engine.connect() as connection:
        assert connection.scalar(text("PRAGMA journal_mode")) == "wal"
        assert connection.scalar(text("PRAGMA foreign_keys")) == 1
        assert connection.scalar(text("PRAGMA busy_timeout")) == 5000
        assert connection.scalar(select(func.count()).select_from(api_provider)) == 3
        defaults = connection.execute(
            select(api_provider.c.kind).where(api_provider.c.is_default == 1)
        ).scalars()
        assert set(defaults) == {"text", "image", "video"}
    database.dispose()


def test_init_db_with_fresh_database_migrates_to_head_and_seeds_mock_providers(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'startup.db').as_posix()}")

    init_db(database=database)

    assert set(inspect(database.engine).get_table_names()) == {
        "alembic_version",
        *(table.name for table in ALL_TABLES),
    }
    shot_columns = {column["name"] for column in inspect(database.engine).get_columns("shot")}
    assert {"image_prompt_override", "video_prompt_override"} <= shot_columns
    asset_columns = {column["name"] for column in inspect(database.engine).get_columns("asset")}
    assert "prompt_override" in asset_columns
    with database.engine.connect() as connection:
        # 与 alembic 脚本目录的 head 对比，避免每加一条迁移都要改硬编码版本号
        script_directory = ScriptDirectory.from_config(_alembic_config())
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            script_directory.get_current_head()
        )
        assert connection.scalar(select(func.count()).select_from(api_provider)) == 3
    database.dispose()


def test_migrate_schema_from_legacy_revision_0003_adds_project_storage_name(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy-0003.db"
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
            INSERT INTO project (id, name) VALUES ('project-1', '旧项目');
            """
        )
    database = Database(f"sqlite:///{database_path.as_posix()}")

    migrate_schema(database)

    with sqlite3.connect(database_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(project)")}
        storage_name = connection.execute(
            "SELECT storage_name FROM project WHERE id = 'project-1'"
        ).fetchone()[0]
    assert "storage_name" in columns
    assert storage_name == "旧项目"
    database.dispose()


def test_database_with_invalid_foreign_key_rejects_insert(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'platform.db').as_posix()}")
    database.create_all()
    now = utc_now()

    with pytest.raises(IntegrityError):
        with database.engine.begin() as connection:
            connection.execute(
                insert(job).values(
                    id="job-invalid",
                    project_id="missing-project",
                    job_type="analyze_novel",
                    created_at=now,
                    updated_at=now,
                )
            )
    database.dispose()


def test_database_with_concurrent_short_transactions_writes_all_rows(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'platform.db').as_posix()}")
    database.create_all()
    _insert_project(database)

    def write_job(index: int) -> None:
        now = utc_now()
        with database.engine.begin() as connection:
            connection.execute(
                insert(job).values(
                    id=f"job-{index}",
                    project_id="project-1",
                    job_type="analyze_novel",
                    created_at=now,
                    updated_at=now,
                )
            )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(write_job, range(40)))

    with database.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(job)) == 40
    database.dispose()


def test_novel_analysis_output_with_missing_required_field_rejects_value() -> None:
    with pytest.raises(ValidationError):
        NovelAnalysisOutput.model_validate({"summary": "只有摘要"})


def test_reference_binding_with_unknown_role_rejects_value() -> None:
    adapter = TypeAdapter(ReferenceBinding)

    with pytest.raises(ValidationError):
        adapter.validate_python({"role": "unknown", "asset_id": "asset-1"})


def test_alembic_initial_migration_upgrades_to_ten_tables(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "migration.db"
    database = Database(f"sqlite:///{database_path.as_posix()}")

    migrate_schema(database)

    with sqlite3.connect(database_path) as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'alembic_%'"
            )
        }
    assert names == {table.name for table in ALL_TABLES}

    alembic_config = Config()
    alembic_config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    alembic_config.set_main_option("prepend_sys_path", str(BACKEND_DIR))
    alembic_config.set_main_option("path_separator", "os")
    with database.engine.begin() as connection:
        alembic_config.attributes["connection"] = connection
        command.downgrade(alembic_config, "base")
    with sqlite3.connect(database_path) as connection:
        remaining = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'alembic_%'"
            )
        }
    assert remaining == set()
    database.dispose()
