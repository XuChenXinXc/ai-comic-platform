from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import insert, select, update

from app.db import Database, utc_now
from app.dynamic_pipeline.delivery import DynamicDeliveryJobService
from app.jobs.dynamic_recovery import recover_dynamic_jobs
from app.jobs.recovery import recover_jobs
from app.jobs.repository import JobRepository
from app.jobs.runner import JobRunner
from app.models.tables import (
    chapter,
    keyframe_version,
    project,
    shot,
    shot_audio,
    shot_subtitle,
    video_version,
    workflow_run,
    workflow_stage,
)
from app.services.errors import ServiceError
from app.services.file_service import file_service
from app.services.media_service import MediaService
from app.services.utils import json_dumps


@pytest.fixture
def dependency_context(tmp_path: Path) -> Iterator[tuple[Database, JobRepository]]:
    database = Database(f"sqlite:///{(tmp_path / 'runner-dependencies.db').as_posix()}")
    database.create_all()
    now = utc_now()
    with database.engine.begin() as connection:
        connection.execute(
            insert(project).values(
                id="dependency-project",
                name="Dependency test project",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(chapter).values(
                id="dependency-chapter",
                project_id="dependency-project",
                chapter_no=1,
                title="Dependency test chapter",
                created_at=now,
                updated_at=now,
            )
        )
    try:
        yield database, JobRepository(database)
    finally:
        database.dispose()


def _job(
    repository: JobRepository,
    *,
    job_id: str,
    depends_on: list[str] | None = None,
    workflow_run_id: str | None = None,
    workflow_stage_id: str | None = None,
    input_signature: str = "job-signature",
) -> dict[str, Any]:
    return repository.create_job(
        {
            "id": job_id,
            "project_id": "dependency-project",
            "chapter_id": "dependency-chapter",
            "workflow_run_id": workflow_run_id,
            "workflow_stage_id": workflow_stage_id,
            "job_type": "dependency_test",
            "provider_kind": "text",
            "input_signature": input_signature,
            "depends_on": depends_on or [],
        }
    )


def _wait_status(repository: JobRepository, job_id: str, status: str) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        record = repository.require(job_id)
        if record["status"] == status:
            return record
        time.sleep(0.01)
    pytest.fail(f"job {job_id} did not reach {status}")


def test_runner_does_not_claim_delayed_dependency_and_wakes_downstream_after_completion(
    dependency_context: tuple[Database, JobRepository],
) -> None:
    database, repository = dependency_context
    upstream = _job(repository, job_id="upstream")
    downstream = _job(repository, job_id="downstream", depends_on=[upstream["id"]])
    calls: list[str] = []
    runner = JobRunner(repository, {"text": 1, "image": 1, "video": 1})
    runner.register_handler("dependency_test", lambda record: calls.append(record["id"]) or {})
    try:
        assert runner.enqueue(downstream["id"]) is False
        assert repository.require(downstream["id"])["status"] == "queued"
        assert calls == []

        assert runner.enqueue(upstream["id"]) is True
        _wait_status(repository, upstream["id"], "completed")
        _wait_status(repository, downstream["id"], "completed")
    finally:
        runner.shutdown()

    assert calls == [upstream["id"], downstream["id"]]
    database.dispose()


def test_runner_fails_downstream_and_dynamic_workflow_when_dependency_failed(
    dependency_context: tuple[Database, JobRepository],
) -> None:
    database, repository = dependency_context
    now = utc_now()
    with database.engine.begin() as connection:
        connection.execute(
            insert(workflow_run).values(
                id="dependency-run",
                project_id="dependency-project",
                chapter_id="dependency-chapter",
                source_kind="full_text",
                status="running",
                current_stage="0-source",
                revision=1,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(workflow_stage).values(
                id="dependency-stage",
                workflow_run_id="dependency-run",
                stage_key="0-source",
                ordinal=0,
                status="queued",
                input_signature="stage-signature",
                created_at=now,
                updated_at=now,
            )
        )
    upstream = _job(repository, job_id="failed-upstream")
    repository.fail(upstream["id"], "provider failure")
    downstream = _job(
        repository,
        job_id="failed-downstream",
        depends_on=[upstream["id"]],
        workflow_run_id="dependency-run",
        workflow_stage_id="dependency-stage",
        input_signature="stage-signature",
    )
    calls: list[str] = []
    runner = JobRunner(repository, {"text": 1, "image": 1, "video": 1})
    runner.register_handler("dependency_test", lambda record: calls.append(record["id"]) or {})
    try:
        assert runner.enqueue(downstream["id"]) is False
    finally:
        runner.shutdown()

    assert repository.require(downstream["id"])["status"] == "failed"
    assert calls == []
    with database.engine.connect() as connection:
        state = connection.execute(
            select(workflow_run.c.status, workflow_stage.c.status)
            .select_from(workflow_run.join(workflow_stage))
            .where(workflow_run.c.id == "dependency-run")
        ).one()
    assert tuple(state) == ("partial_failed", "partial_failed")
    database.dispose()


def test_recovery_only_enqueues_dependency_ready_legacy_job(
    dependency_context: tuple[Database, JobRepository],
) -> None:
    database, repository = dependency_context
    upstream = _job(repository, job_id="recovery-upstream")
    downstream = _job(repository, job_id="recovery-downstream", depends_on=[upstream["id"]])

    assert recover_jobs(repository) == [upstream["id"]]
    assert repository.require(downstream["id"])["status"] == "queued"
    database.dispose()


def test_dynamic_recovery_does_not_reconcile_stage_before_prerequisite_confirmation(
    dependency_context: tuple[Database, JobRepository],
) -> None:
    database, repository = dependency_context
    now = utc_now()
    with database.engine.begin() as connection:
        connection.execute(
            insert(workflow_run).values(
                id="recovery-run",
                project_id="dependency-project",
                chapter_id="dependency-chapter",
                source_kind="full_text",
                status="queued",
                current_stage="1-character-bios",
                revision=1,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(workflow_stage),
            [
                {
                    "id": "recovery-source",
                    "workflow_run_id": "recovery-run",
                    "stage_key": "0-source",
                    "ordinal": 0,
                    "status": "queued",
                    "input_signature": "source-signature",
                    "created_at": now,
                    "updated_at": now,
                },
                {
                    "id": "recovery-bios",
                    "workflow_run_id": "recovery-run",
                    "stage_key": "1-character-bios",
                    "ordinal": 1,
                    "status": "queued",
                    "input_signature": "bios-signature",
                    "created_at": now,
                    "updated_at": now,
                },
            ],
        )

    report = recover_dynamic_jobs(repository)

    assert len(report.enqueue_ids) == 1
    assert repository.require(report.enqueue_ids[0])["workflow_stage_id"] == "recovery-source"
    assert repository.list_by_workflow("recovery-run")[0]["workflow_stage_id"] == "recovery-source"
    database.dispose()


def test_dynamic_media_activation_rejects_changed_stage_signature_without_replacing_current(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'media-activation.db').as_posix()}")
    database.create_all()
    monkeypatch.setattr(file_service, "workspace_dir", (tmp_path / "workspace").resolve())
    monkeypatch.setattr(file_service, "projects_dir", (tmp_path / "projects").resolve())
    file_service.projects_dir.mkdir(parents=True, exist_ok=True)
    repository = JobRepository(database)
    media = MediaService(database)
    now = utc_now()
    with database.engine.begin() as connection:
        connection.execute(
            insert(project).values(
                id="media-project",
                name="Media test project",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(chapter).values(
                id="media-chapter",
                project_id="media-project",
                chapter_no=1,
                title="Media chapter",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(shot).values(
                id="media-shot",
                project_id="media-project",
                chapter_id="media-chapter",
                shot_no=1,
                shot_order=1,
                source_type="screenplay",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(workflow_run).values(
                id="media-run",
                project_id="media-project",
                chapter_id="media-chapter",
                source_kind="full_text",
                status="running",
                current_stage="5a-image-prompts",
                revision=1,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(workflow_stage).values(
                id="media-stage",
                workflow_run_id="media-run",
                stage_key="5a-image-prompts",
                ordinal=5,
                status="confirmed",
                input_signature="stage-v1",
                attempt=1,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(keyframe_version).values(
                id="media-old",
                shot_id="media-shot",
                version_no=1,
                file_path="temp/old.png",
                prompt_snapshot="old",
                status="completed",
                is_current=True,
                input_signature="old-signature",
                source="generated",
                provenance_json=json_dumps(
                    {
                        "workflow_run_id": "media-run",
                        "workflow_stage_id": "media-stage",
                        "workflow_stage_attempt": 1,
                    }
                ),
                created_at=now,
            )
        )
    old_path = file_service.resolve_project_path("media-project", "temp/old.png")
    old_path.parent.mkdir(parents=True, exist_ok=True)
    old_path.write_bytes(b"old")
    new_path = file_service.resolve_project_path("media-project", "temp/new.png")
    new_path.write_bytes(b"new")
    target_job = repository.create_job(
        {
            "id": "media-job",
            "project_id": "media-project",
            "chapter_id": "media-chapter",
            "workflow_run_id": "media-run",
            "workflow_stage_id": "media-stage",
            "job_type": "generate_keyframe",
            "target_type": "keyframe",
            "target_id": "media-new",
            "provider_kind": "image",
            "input_signature": "new-signature",
            "input_data": {
                "workflow_run_id": "media-run",
                "workflow_stage_id": "media-stage",
                "workflow_stage_input_signature": "stage-v1",
                "workflow_stage_attempt": 1,
            },
        }
    )
    with database.engine.begin() as connection:
        connection.execute(
            insert(keyframe_version).values(
                id="media-new",
                shot_id="media-shot",
                version_no=2,
                file_path="temp/new.png",
                prompt_snapshot="new",
                status="running",
                is_current=False,
                input_signature="new-signature",
                source="generated",
                provenance_json=json_dumps(
                    {
                        "workflow_run_id": "media-run",
                        "workflow_stage_id": "media-stage",
                        "workflow_stage_attempt": 1,
                        "upstream_input_signature": "stage-v1",
                    }
                ),
                created_at=now,
            )
        )
    original_guard = media._dynamic_activation_guard

    def racing_guard(
        connection: Any,
        job_record: dict[str, Any],
        input_data: dict[str, Any],
        version: dict[str, Any],
    ) -> dict[str, Any]:
        connection.execute(
            update(workflow_stage)
            .where(workflow_stage.c.id == "media-stage")
            .values(input_signature="stage-v2")
        )
        return original_guard(connection, job_record, input_data, version)

    monkeypatch.setattr(media, "_dynamic_activation_guard", racing_guard)
    with pytest.raises(ServiceError):
        media._activate_media_version(
            "keyframe",
            "media-new",
            source_relative="temp/new.png",
            complete=True,
            job_record=target_job,
        )

    with database.engine.connect() as connection:
        state = connection.execute(
            select(keyframe_version.c.id, keyframe_version.c.is_current)
            .where(keyframe_version.c.shot_id == "media-shot")
            .order_by(keyframe_version.c.version_no)
        ).all()
    assert [(row.id, row.is_current) for row in state] == [("media-old", 1), ("media-new", 0)]
    assert old_path.is_file()
    assert new_path.is_file()
    database.dispose()


def _insert_delivery_context(database: Database, *, latest_attempt: bool = False) -> None:
    now = utc_now()
    with database.engine.begin() as connection:
        connection.execute(
            insert(project).values(
                id="subtitle-project",
                name="Subtitle test project",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(chapter).values(
                id="subtitle-chapter",
                project_id="subtitle-project",
                chapter_no=1,
                title="Subtitle chapter",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(shot).values(
                id="subtitle-shot",
                project_id="subtitle-project",
                chapter_id="subtitle-chapter",
                shot_no=1,
                shot_order=1,
                source_type="screenplay",
                duration_sec=6,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(workflow_run).values(
                id="subtitle-run",
                project_id="subtitle-project",
                chapter_id="subtitle-chapter",
                source_kind="full_text",
                status="running",
                current_stage="6-delivery",
                revision=1,
                created_at=now,
                updated_at=now,
            )
        )
        stages = [
            {
                "id": "subtitle-stage-old",
                "workflow_run_id": "subtitle-run",
                "stage_key": "6-delivery",
                "ordinal": 6,
                "status": "stale" if latest_attempt else "running",
                "input_signature": "delivery-old",
                "attempt": 1,
                "created_at": now,
                "updated_at": now,
            }
        ]
        if latest_attempt:
            stages.append(
                {
                    "id": "subtitle-stage-new",
                    "workflow_run_id": "subtitle-run",
                    "stage_key": "6-delivery",
                    "ordinal": 6,
                    "status": "running",
                    "input_signature": "delivery-new",
                    "attempt": 2,
                    "created_at": now,
                    "updated_at": now,
                }
            )
        connection.execute(insert(workflow_stage), stages)


def test_subtitle_job_keeps_queued_when_audio_dependency_is_not_completed(
    dependency_context: tuple[Database, JobRepository],
) -> None:
    database, repository = dependency_context
    now = utc_now()
    with database.engine.begin() as connection:
        connection.execute(
            insert(workflow_run).values(
                id="subtitle-pending-run",
                project_id="dependency-project",
                chapter_id="dependency-chapter",
                source_kind="full_text",
                status="running",
                current_stage="6-delivery",
                revision=1,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(workflow_stage).values(
                id="subtitle-pending-stage",
                workflow_run_id="subtitle-pending-run",
                stage_key="6-delivery",
                ordinal=6,
                status="running",
                input_signature="delivery-signature",
                created_at=now,
                updated_at=now,
            )
        )
    audio_dependency = _job(repository, job_id="subtitle-audio-pending")
    subtitle_job = repository.create_job(
        {
            "id": "subtitle-pending",
            "project_id": "dependency-project",
            "chapter_id": "dependency-chapter",
            "workflow_run_id": "subtitle-pending-run",
            "workflow_stage_id": "subtitle-pending-stage",
            "job_type": "generate_dynamic_subtitle",
            "target_type": "shot_subtitle",
            "provider_kind": "text",
            "input_signature": "subtitle-signature",
            "depends_on": [audio_dependency["id"]],
            "input_data": {
                "input_signature": "subtitle-signature",
                "upstream_input_signature": "delivery-signature",
                "shots": {},
            },
        }
    )
    service = DynamicDeliveryJobService(database, repository=repository)

    result = asyncio.run(service.handle_subtitle_job(subtitle_job))

    assert result["status"] == "queued"
    assert repository.require(subtitle_job["id"])["status"] == "queued"
    with database.engine.connect() as connection:
        assert connection.scalar(select(shot_subtitle.c.id)) is None
    database.dispose()


def test_late_subtitle_attempt_cannot_replace_new_timeline(
    dependency_context: tuple[Database, JobRepository],
) -> None:
    database, repository = dependency_context
    _insert_delivery_context(database, latest_attempt=True)
    now = utc_now()
    with database.engine.begin() as connection:
        connection.execute(
            insert(shot_audio).values(
                id="subtitle-audio",
                shot_id="subtitle-shot",
                dialogue_line_id="line-1",
                line_order=1,
                dialogue_text="new timeline",
                version_no=1,
                duration_sec=1,
                status="completed",
                is_current=True,
                input_signature="audio-new",
                created_at=now,
            )
        )
        connection.execute(
            insert(video_version).values(
                id="subtitle-video",
                shot_id="subtitle-shot",
                version_no=1,
                actual_duration_sec=6,
                status="completed",
                is_current=True,
                input_signature="video-new",
                source="generated",
                created_at=now,
            )
        )
        connection.execute(
            insert(shot_subtitle).values(
                id="new-subtitle",
                shot_id="subtitle-shot",
                dialogue_line_id="line-1",
                audio_id="subtitle-audio",
                text="new timeline",
                start_time=0,
                end_time=1,
                provenance_json=json_dumps(
                    {
                        "workflow_run_id": "subtitle-run",
                        "workflow_stage_id": "subtitle-stage-new",
                        "input_signature": "new-subtitle-signature",
                    }
                ),
                created_at=now,
            )
        )
    late_job = repository.create_job(
        {
            "id": "subtitle-late",
            "project_id": "subtitle-project",
            "chapter_id": "subtitle-chapter",
            "workflow_run_id": "subtitle-run",
            "workflow_stage_id": "subtitle-stage-old",
            "job_type": "generate_dynamic_subtitle",
            "target_type": "shot_subtitle",
            "provider_kind": "text",
            "input_signature": "old-subtitle-signature",
            "input_data": {
                "input_signature": "old-subtitle-signature",
                "upstream_input_signature": "delivery-old",
                "shots": {
                    "shot": {
                        "shot_id": "subtitle-shot",
                        "duration_sec": 6,
                        "dialogues": [
                            {
                                "dialogue_line_id": "line-1",
                                "text": "late timeline",
                            }
                        ],
                    }
                },
            },
        }
    )
    service = DynamicDeliveryJobService(database, repository=repository)

    result = asyncio.run(service.handle_subtitle_job(late_job))

    assert result["status"] == "interrupted"
    assert repository.require(late_job["id"])["status"] == "interrupted"
    with database.engine.connect() as connection:
        row = connection.execute(
            select(shot_subtitle.c.text, shot_subtitle.c.provenance_json)
            .where(shot_subtitle.c.id == "new-subtitle")
        ).one()
    assert row.text == "new timeline"
    assert row.provenance_json == json_dumps(
        {
            "workflow_run_id": "subtitle-run",
            "workflow_stage_id": "subtitle-stage-new",
            "input_signature": "new-subtitle-signature",
        }
    )
    database.dispose()
