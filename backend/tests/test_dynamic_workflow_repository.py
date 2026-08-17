from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import insert, update

from app.db import Database, utc_now
from app.dynamic_pipeline.stage_catalog import STAGE_KEYS
from app.dynamic_pipeline.state_machine import RevisionConflict
from app.models.tables import chapter, project, workflow_run, workflow_stage
from app.services.dynamic_workflow_repository import DynamicWorkflowRepository


@pytest.fixture
def repository(tmp_path: Path):
    database = Database(f"sqlite:///{(tmp_path / 'workflow.db').as_posix()}")
    database.initialize(seed_mock_providers=False)
    now = utc_now()
    with database.engine.begin() as connection:
        connection.execute(
            insert(project).values(
                id="project-repository",
                name="仓储测试项目",
                is_free_create=1,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(chapter).values(
                id="chapter-repository",
                project_id="project-repository",
                chapter_no=1,
                title="仓储测试章节",
                created_at=now,
                updated_at=now,
            )
        )
    try:
        yield database, DynamicWorkflowRepository(database)
    finally:
        database.dispose()


def _create_run(database: Database, repository: DynamicWorkflowRepository, run_id: str = "run-1"):
    stages = [
        {"id": f"{run_id}-stage-{index}", "stage_key": stage_key}
        for index, stage_key in enumerate(STAGE_KEYS)
    ]
    with database.engine.begin() as connection:
        repository.create_run_with_stages(
            connection,
            {
                "id": run_id,
                "project_id": "project-repository",
                "chapter_id": "chapter-repository",
                "source_kind": "full_text",
                "source_snapshot": {"text": "原文"},
                "config_snapshot": {"target_duration_sec": 30},
                "asset_mapping": [{"workflow_asset_id": "asset-1", "usage": "character"}],
                "input_signature": "signature-1",
            },
            stages,
        )
    return stages


def test_create_and_query_run_decodes_json_and_joins_titles(repository) -> None:
    database, workflow_repository = repository
    _create_run(database, workflow_repository)

    run = workflow_repository.get_run("run-1")
    assert run is not None
    assert run["title"] == "仓储测试项目"
    assert run["chapter_title"] == "仓储测试章节"
    assert run["project_id"] == "project-repository"
    assert run["chapter_id"] == "chapter-repository"
    assert run["current_stage"] == "0-source"
    assert run["source_snapshot"] == {"text": "原文"}
    assert run["config_snapshot"] == {"target_duration_sec": 30}
    assert run["asset_mapping"] == [{"workflow_asset_id": "asset-1", "usage": "character"}]

    combined = workflow_repository.get_run_with_stages("run-1")
    assert combined is not None
    assert combined["run"]["id"] == "run-1"
    assert len(combined["stages"]) == len(STAGE_KEYS)


def test_stage_list_and_json_fields_are_decoded(repository) -> None:
    database, workflow_repository = repository
    stages = _create_run(database, workflow_repository)
    with database.engine.begin() as connection:
        connection.execute(
            update(workflow_stage)
            .where(workflow_stage.c.id == stages[0]["id"])
            .values(
                raw_output_json='{"raw":true}',
                normalized_output_json='{"normalized":true}',
                draft_output_json='{"draft":true}',
                output_refs_json='[{"path":"output.json"}]',
                gate_result_json='{"status":"ready","passed":true}',
            )
        )

    stage = workflow_repository.get_stage("run-1", "0-source")
    assert stage is not None
    assert stage["raw_output"] == {"raw": True}
    assert stage["normalized_output"] == {"normalized": True}
    assert stage["draft_output"] == {"draft": True}
    assert stage["output_refs"] == [{"path": "output.json"}]
    assert stage["gate_result"] == {"status": "ready", "passed": True}
    assert [item["stage_key"] for item in workflow_repository.list_stages("run-1")] == list(
        STAGE_KEYS
    )


def test_find_active_run_excludes_failed_and_cancelled_and_returns_latest_active(
    repository,
) -> None:
    database, workflow_repository = repository
    _create_run(database, workflow_repository, "run-active-old")
    with database.engine.begin() as connection:
        connection.execute(
            update(workflow_run)
            .where(workflow_run.c.id == "run-active-old")
            .values(status="failed", created_at="2026-01-01T00:00:00+00:00")
        )
    _create_run(database, workflow_repository, "run-cancelled")
    with database.engine.begin() as connection:
        connection.execute(
            update(workflow_run)
            .where(workflow_run.c.id == "run-cancelled")
            .values(status="cancelled", created_at="2026-01-03T00:00:00+00:00")
        )
    _create_run(database, workflow_repository, "run-active-new")
    with database.engine.begin() as connection:
        connection.execute(
            update(workflow_run)
            .where(workflow_run.c.id == "run-active-new")
            .values(created_at="2026-01-02T00:00:00+00:00")
        )

    active = workflow_repository.find_active_run("chapter-repository")
    assert active is not None
    assert active["id"] == "run-active-new"


def test_run_and_stage_writes_require_expected_revision(repository) -> None:
    database, workflow_repository = repository
    _create_run(database, workflow_repository)

    updated = workflow_repository.update_run_status(
        "run-1",
        expected_revision=1,
        expected_statuses=["pending"],
        target_status="queued",
    )
    assert updated["status"] == "queued"
    assert updated["revision"] == 2

    with pytest.raises(RevisionConflict) as error:
        workflow_repository.update_run_status(
            "run-1",
            expected_revision=1,
            expected_statuses=["queued"],
            target_status="running",
        )
    assert error.value.expected_revision == 1
    assert error.value.actual_revision == 2


def test_save_draft_and_update_stage_status_bump_run_revision(repository) -> None:
    database, workflow_repository = repository
    _create_run(database, workflow_repository)

    draft = workflow_repository.save_stage_draft(
        "run-1",
        "0-source",
        expected_run_revision=1,
        draft_output={"summary": "已编辑"},
        gate_result={"status": "ready", "passed": True, "can_confirm": True},
    )
    assert draft["draft_output"] == {"summary": "已编辑"}
    assert draft["gate_result"]["status"] == "ready"
    assert workflow_repository.get_run("run-1")["revision"] == 2

    updated = workflow_repository.update_stage_status(
        run_id="run-1",
        stage_key="0-source",
        expected_run_revision=2,
        target_status="running",
        error="",
    )
    assert updated["status"] == "running"
    assert workflow_repository.get_run("run-1")["revision"] == 3

    with pytest.raises(RevisionConflict):
        workflow_repository.save_stage_draft(
            "run-1",
            "0-source",
            expected_run_revision=2,
            draft_output={"summary": "过期"},
        )


def test_mark_descendants_stale_updates_only_descendants(repository) -> None:
    database, workflow_repository = repository
    _create_run(database, workflow_repository)
    with database.engine.begin() as connection:
        connection.execute(
            update(workflow_stage)
            .where(
                workflow_stage.c.workflow_run_id == "run-1",
                workflow_stage.c.stage_key.in_(
                    ["4-storyboard", "5a-image-prompts", "5b-video-prompts", "6-delivery"]
                ),
            )
            .values(status="confirmed")
        )
        changed = workflow_repository.mark_descendants_stale(connection, "run-1", "4-storyboard")

    assert changed == ["5a-image-prompts", "5b-video-prompts", "6-delivery"]
    assert workflow_repository.get_stage("run-1", "4-storyboard")["status"] == "confirmed"
    assert all(
        workflow_repository.get_stage("run-1", stage_key)["status"] == "stale"
        for stage_key in ("5a-image-prompts", "5b-video-prompts", "6-delivery")
    )
