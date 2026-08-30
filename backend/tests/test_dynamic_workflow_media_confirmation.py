from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, insert, select, update

from app.db import Database
from app.dynamic_pipeline.media_queue_service import DynamicMediaQueueService
from app.dynamic_pipeline.prompt_ir import (
    CompiledPromptBundle,
    ImagePromptBundle,
    TimelineSegment,
    VideoPromptBundle,
)
from app.dynamic_pipeline.prompt_stage import PromptStageOutput, stage_output_to_stage_json
from app.dynamic_pipeline.schemas import (
    ArtifactMeta,
    Diagnostic,
    StageConfirmRequest,
    StageGateResult,
)
from app.dynamic_pipeline.stage_catalog import STAGE_KEYS, get_stage_ordinal
from app.jobs.dynamic_bridge import DynamicJobBridge
from app.jobs.repository import JobRepository
from app.models.tables import (
    chapter,
    job,
    keyframe_version,
    project,
    screenplay,
    shot,
    video_version,
    workflow_run,
    workflow_stage,
)
from app.services.dynamic_workflow_service import DynamicWorkflowService
from app.services.errors import ServiceError
from app.services.utils import json_dumps, utc_now


@dataclass
class ConfirmationContext:
    database: Database
    service: DynamicWorkflowService
    queue: RecordingMediaQueue
    run_id: str
    project_id: str
    chapter_id: str
    stage_ids: dict[str, str]
    shot_ids: dict[str, str]
    enqueued_job_ids: list[str]


class RecordingMediaQueue:
    def __init__(self, delegate: DynamicMediaQueueService) -> None:
        self.delegate = delegate
        self.calls: list[dict[str, Any]] = []
        self.results: list[dict[str, Any]] = []
        self.error: Exception | None = None
        self.response: dict[str, Any] | None = None

    def queue_confirmed_prompt_stage(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(dict(kwargs))
        if self.error is not None:
            raise self.error
        if self.response is not None:
            result = dict(self.response)
        else:
            result = self.delegate.queue_confirmed_prompt_stage(**kwargs)
        self.results.append(result)
        return result


@pytest.fixture
def confirmation_context(tmp_path: Path) -> Iterator[ConfirmationContext]:
    database = Database(f"sqlite:///{(tmp_path / 'dynamic-workflow-media-confirm.db').as_posix()}")
    database.initialize(seed_mock_providers=True)
    now = utc_now()
    project_id = "media-confirm-project"
    chapter_id = "media-confirm-chapter"
    run_id = "media-confirm-run"
    stage_ids = {stage_key: f"media-confirm-stage-{stage_key}" for stage_key in STAGE_KEYS}
    shot_ids = {"shot-a": "media-confirm-shot-a", "shot-b": "media-confirm-shot-b"}

    with database.engine.begin() as connection:
        connection.execute(
            insert(project).values(
                id=project_id,
                name="动态媒体确认测试项目",
                is_free_create=1,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(chapter).values(
                id=chapter_id,
                project_id=project_id,
                chapter_no=1,
                title="第一章",
                source_text="测试源文",
                source_signature="media-confirm-source",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(workflow_run).values(
                id=run_id,
                project_id=project_id,
                chapter_id=chapter_id,
                kind="dynamic_comic_0_6",
                schema_version="1",
                status="ready",
                current_stage="5a-image-prompts",
                source_kind="synopsis",
                source_snapshot_json=json_dumps({"text": "测试源文"}),
                config_snapshot_json=json_dumps(
                    {
                        "target_duration_sec": 12,
                        "image_provider_id": "mock-image",
                        "image_provider_model": "config-image-model",
                    }
                ),
                asset_mapping_json=json_dumps([]),
                input_signature="media-confirm-run-input",
                revision=1,
                error="",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(workflow_stage),
            [
                {
                    "id": stage_ids[stage_key],
                    "workflow_run_id": run_id,
                    "stage_key": stage_key,
                    "ordinal": get_stage_ordinal(stage_key),
                    "status": (
                        "confirmed"
                        if stage_key == "4-storyboard"
                        else "ready"
                        if stage_key in {"5a-image-prompts", "5b-video-prompts"}
                        else "pending"
                    ),
                    "input_signature": f"{stage_key}-input",
                    "raw_output_json": "{}",
                    "normalized_output_json": "{}",
                    "draft_output_json": "{}",
                    "output_refs_json": "[]",
                    "gate_result_json": json_dumps(
                        StageGateResult(
                            status="ready",
                            passed=True,
                            can_confirm=True,
                        ).model_dump(mode="json")
                        if stage_key in {"5a-image-prompts", "5b-video-prompts"}
                        else {}
                    ),
                    "attempt": 1,
                    "error": "",
                    "created_at": now,
                    "updated_at": now,
                }
                for stage_key in STAGE_KEYS
            ],
        )
        connection.execute(
            insert(shot),
            [
                {
                    "id": shot_ids[external_key],
                    "project_id": project_id,
                    "chapter_id": chapter_id,
                    "shot_no": index,
                    "shot_order": index,
                    "source_type": "screenplay",
                    "external_key": external_key,
                    "status": "confirmed",
                    "created_at": now,
                    "updated_at": now,
                }
                for index, external_key in enumerate(("shot-a", "shot-b"), start=1)
            ],
        )

    delegate = DynamicMediaQueueService(
        database,
        job_repository=JobRepository(database),
        enqueue=lambda _job_id: True,
    )
    queue = RecordingMediaQueue(delegate)
    enqueued_job_ids: list[str] = []
    service = DynamicWorkflowService(
        database,
        media_queue_service=queue,
        job_bridge=DynamicJobBridge(
            JobRepository(database),
            enqueue=lambda job_id: enqueued_job_ids.append(job_id) or True,
        ),
    )
    try:
        yield ConfirmationContext(
            database=database,
            service=service,
            queue=queue,
            run_id=run_id,
            project_id=project_id,
            chapter_id=chapter_id,
            stage_ids=stage_ids,
            shot_ids=shot_ids,
            enqueued_job_ids=enqueued_job_ids,
        )
    finally:
        database.dispose()


def _prompt_stage_json(
    context: ConfirmationContext,
    stage_key: str,
    *,
    provider_id: str,
    provider_model: str,
    external_keys: tuple[str, ...] = ("shot-a", "shot-b"),
) -> dict[str, Any]:
    bundles: dict[str, CompiledPromptBundle] = {}
    for index, external_key in enumerate(external_keys, start=1):
        shot_id = context.shot_ids[external_key]
        provenance: dict[str, Any] = {
            "shot_id": shot_id,
            "external_key": external_key,
            "ordinal": index,
            "shot_no": index,
            "provider_id": provider_id,
            "provider_model": provider_model,
            "provider_capabilities": {
                "provider_id": provider_id,
                "model": provider_model,
                "supports_multiple_images": True,
                "max_reference_images": 2,
                "supports_end_frame": True,
            },
        }
        if stage_key == "5b-video-prompts" and index == 2:
            provenance["continuity"] = {"previous_end_frame_path": "frames/shot-a-end.png"}
        if stage_key == "5a-image-prompts":
            image = ImagePromptBundle(
                prompt=f"静止画提示词-{external_key}",
                negative_prompt="文字，水印",
                semantic_plan={"state": "standing"},
            )
            bundle = CompiledPromptBundle(
                status="ready",
                image=image,
                generated={"image": image},
                final={"image": image},
                provenance=provenance,
            )
        else:
            video = VideoPromptBundle(
                prompt=f"视频提示词-{external_key}",
                negative_prompt="文字，水印",
                edit_duration_sec=6,
                generation_duration_sec=6,
                timeline=[TimelineSegment(start=0, end=6, action="稳定移动")],
                end_state="动作结束",
                semantic_plan={
                    "continuity": {
                        "transition_type": "continuous" if index == 2 else "scene_change"
                    },
                    "action": "稳定移动",
                },
            )
            bundle = CompiledPromptBundle(
                status="ready",
                video=video,
                generated={"video": video},
                final={"video": video},
                provenance=provenance,
            )
        bundles[external_key] = bundle

    output = PromptStageOutput(
        stage_key=stage_key,  # type: ignore[arg-type]
        artifact_meta=ArtifactMeta(
            schema_version=f"dynamic-{stage_key}-test-v1",
            normalizer_version="dynamic-media-confirm-test-v1",
            input_signature=f"{stage_key}-input",
            content_signature=f"{stage_key}-content",
            source_attempt=1,
            provider_id=provider_id,
            provider_model=provider_model,
        ),
        bundles=bundles,
        target_duration_sec=12,
        status="ready",
        provenance={
            "workflow_run_id": context.run_id,
            "workflow_stage_id": context.stage_ids[stage_key],
            "input_signature": f"{stage_key}-input",
            "provider_id": provider_id,
            "provider_model": provider_model,
        },
    )
    return stage_output_to_stage_json(output)


def _write_prompt_stage(
    context: ConfirmationContext,
    stage_key: str,
    output: dict[str, Any],
    *,
    gate: StageGateResult | None = None,
) -> None:
    with context.database.engine.begin() as connection:
        connection.execute(
            update(workflow_stage)
            .where(workflow_stage.c.id == context.stage_ids[stage_key])
            .values(
                normalized_output_json=json_dumps(output),
                draft_output_json=json_dumps(output),
                gate_result_json=json_dumps(
                    (
                        gate or StageGateResult(status="ready", passed=True, can_confirm=True)
                    ).model_dump(mode="json")
                ),
            )
        )


def _count(database: Database, table: Any, *conditions: Any) -> int:
    statement = select(func.count()).select_from(table)
    if conditions:
        statement = statement.where(*conditions)
    with database.engine.connect() as connection:
        return int(connection.scalar(statement) or 0)


def _jobs(database: Database, run_id: str, job_type: str) -> list[dict[str, Any]]:
    with database.engine.connect() as connection:
        rows = connection.execute(
            select(job)
            .where(job.c.workflow_run_id == run_id, job.c.job_type == job_type)
            .order_by(job.c.created_at, job.c.id)
        ).all()
    return [dict(row._mapping) for row in rows]


def test_confirming_5a_and_5b_queues_media_with_provider_provenance_and_dependencies(
    confirmation_context: ConfirmationContext,
) -> None:
    context = confirmation_context
    _write_prompt_stage(
        context,
        "5a-image-prompts",
        _prompt_stage_json(
            context,
            "5a-image-prompts",
            provider_id="mock-image",
            provider_model="bundle-image-model",
        ),
    )
    _write_prompt_stage(
        context,
        "5b-video-prompts",
        _prompt_stage_json(
            context,
            "5b-video-prompts",
            provider_id="mock-video",
            provider_model="bundle-video-model",
        ),
    )

    image_action = context.service.confirm_stage(
        context.run_id,
        "5a-image-prompts",
        StageConfirmRequest(expected_revision=1),
    )

    assert context.queue.calls[0] == {
        "workflow_run_id": context.run_id,
        "workflow_stage_id": context.stage_ids["5a-image-prompts"],
        "expected_revision": 2,
        "provider_id": "mock-image",
        "provider_model": "config-image-model",
    }
    assert image_action["run"]["revision"] == 3
    assert image_action["run"]["current_stage"] == "5a-image-prompts"
    assert image_action["stages"][5]["status"] == "confirmed"
    assert context.queue.results[0]["revision"] == 3
    assert _count(context.database, keyframe_version) == 2
    image_jobs = _jobs(context.database, context.run_id, "generate_keyframe")
    assert len(image_jobs) == 2
    with context.database.engine.connect() as connection:
        image_versions = [
            dict(row._mapping)
            for row in connection.execute(
                select(keyframe_version).order_by(keyframe_version.c.version_no)
            ).all()
        ]
    assert {row["provider_id"] for row in image_versions} == {"mock-image"}
    assert {row["model"] for row in image_versions} == {"config-image-model"}
    assert {json.loads(row["provenance_json"])["workflow_stage_id"] for row in image_versions} == {
        context.stage_ids["5a-image-prompts"]
    }
    assert _count(context.database, screenplay) == 0
    assert _count(context.database, shot) == 2

    video_action = context.service.confirm_stage(
        context.run_id,
        "5b-video-prompts",
        StageConfirmRequest(expected_revision=image_action["run"]["revision"]),
    )

    assert context.queue.calls[1] == {
        "workflow_run_id": context.run_id,
        "workflow_stage_id": context.stage_ids["5b-video-prompts"],
        "expected_revision": 4,
        "provider_id": "mock-video",
        "provider_model": "bundle-video-model",
    }
    assert video_action["run"]["revision"] == 5
    assert video_action["run"]["current_stage"] == "6-delivery"
    video_jobs = _jobs(context.database, context.run_id, "generate_video")
    assert len(video_jobs) == 2
    image_jobs_by_external = {
        json.loads(item["input_json"])["external_key"]: item["id"] for item in image_jobs
    }
    video_jobs_by_external = {
        json.loads(item["input_json"])["external_key"]: item for item in video_jobs
    }
    assert json.loads(video_jobs_by_external["shot-a"]["depends_on_json"]) == [
        image_jobs_by_external["shot-a"]
    ]
    assert json.loads(video_jobs_by_external["shot-b"]["depends_on_json"]) == [
        image_jobs_by_external["shot-b"],
        video_jobs_by_external["shot-a"]["id"],
    ]
    assert _count(context.database, video_version) == 2
    assert _count(context.database, shot) == 2

    delivery_jobs = _jobs(context.database, context.run_id, "dynamic_stage_generate")
    assert len(delivery_jobs) == 1
    assert delivery_jobs[0]["workflow_stage_id"] == context.stage_ids["6-delivery"]
    assert delivery_jobs[0]["status"] == "queued"
    assert delivery_jobs[0]["input_signature"]
    delivery_dependencies = json.loads(delivery_jobs[0]["depends_on_json"])
    assert set(delivery_dependencies) == {
        *(item["id"] for item in image_jobs),
        *(item["id"] for item in video_jobs),
    }
    assert context.enqueued_job_ids == []
    with context.database.engine.connect() as connection:
        assert connection.scalar(
            select(workflow_stage.c.status).where(
                workflow_stage.c.id == context.stage_ids["6-delivery"]
            )
        ) == "queued"

    before_jobs = _count(context.database, job)
    with pytest.raises(ServiceError) as repeated:
        context.service.confirm_stage(
            context.run_id,
            "5b-video-prompts",
            StageConfirmRequest(expected_revision=video_action["run"]["revision"]),
        )
    assert repeated.value.status_code == 409
    assert len(context.queue.calls) == 2
    assert _count(context.database, job) == before_jobs


def test_prompt_queue_failure_keeps_confirmed_stage_and_records_recovery_warning(
    confirmation_context: ConfirmationContext,
) -> None:
    context = confirmation_context
    context.queue.error = RuntimeError("queue unavailable")
    _write_prompt_stage(
        context,
        "5a-image-prompts",
        _prompt_stage_json(
            context,
            "5a-image-prompts",
            provider_id="mock-image",
            provider_model="bundle-image-model",
            external_keys=("shot-a",),
        ),
    )

    action = context.service.confirm_stage(
        context.run_id,
        "5a-image-prompts",
        StageConfirmRequest(expected_revision=1),
    )

    assert len(context.queue.calls) == 1
    assert action["run"]["revision"] == 2
    stage = context.service.get_stage(context.run_id, "5a-image-prompts")
    assert stage["status"] == "confirmed"
    assert stage["confirmed_revision"] == 2
    assert "媒体队列未完成" in stage["error"]
    assert any(
        item["capture_mode"] == "dynamic_media_queue_warning" for item in stage["output_refs"]
    )
    assert _count(context.database, keyframe_version) == 0
    assert _count(context.database, job) == 0


def test_blocked_prompt_queue_result_keeps_confirmed_stage_without_media_jobs(
    confirmation_context: ConfirmationContext,
) -> None:
    context = confirmation_context
    context.queue.response = {"status": "blocked", "jobs": [], "versions": []}
    _write_prompt_stage(
        context,
        "5a-image-prompts",
        _prompt_stage_json(
            context,
            "5a-image-prompts",
            provider_id="mock-image",
            provider_model="bundle-image-model",
            external_keys=("shot-a",),
        ),
    )

    action = context.service.confirm_stage(
        context.run_id,
        "5a-image-prompts",
        StageConfirmRequest(expected_revision=1),
    )

    assert action["run"]["revision"] == 2
    assert context.service.get_stage(context.run_id, "5a-image-prompts")["status"] == "confirmed"
    assert (
        "媒体队列未完成" in context.service.get_stage(context.run_id, "5a-image-prompts")["error"]
    )
    assert _count(context.database, keyframe_version) == 0
    assert _count(context.database, job) == 0


@pytest.mark.parametrize(
    ("stage_status", "expected_revision"),
    [("blocked", 1), ("pending", 1), ("ready", 99)],
)
def test_blocked_unconfirmed_or_old_revision_does_not_call_media_queue(
    confirmation_context: ConfirmationContext,
    stage_status: str,
    expected_revision: int,
) -> None:
    context = confirmation_context
    with context.database.engine.begin() as connection:
        connection.execute(
            update(workflow_stage)
            .where(workflow_stage.c.id == context.stage_ids["5a-image-prompts"])
            .values(status=stage_status)
        )

    with pytest.raises(ServiceError) as error:
        context.service.confirm_stage(
            context.run_id,
            "5a-image-prompts",
            StageConfirmRequest(expected_revision=expected_revision),
        )

    assert error.value.status_code == 409
    assert context.queue.calls == []
    assert context.service.get_run(context.run_id)["revision"] == 1
    assert _count(context.database, keyframe_version) == 0
    assert _count(context.database, job) == 0


def test_warning_gate_is_confirmable_and_default_constructor_has_real_media_runner(
    confirmation_context: ConfirmationContext,
) -> None:
    context = confirmation_context
    no_queue_service = DynamicWorkflowService(context.database)
    assert isinstance(no_queue_service.media_queue_service, DynamicMediaQueueService)
    assert callable(no_queue_service.media_queue_service.enqueue)
    assert no_queue_service.media_queue_service.enqueue == no_queue_service.job_bridge.enqueue
    _write_prompt_stage(
        context,
        "5a-image-prompts",
        _prompt_stage_json(
            context,
            "5a-image-prompts",
            provider_id="mock-image",
            provider_model="bundle-image-model",
            external_keys=("shot-a",),
        ),
        gate=StageGateResult(
            status="warning",
            passed=True,
            can_confirm=True,
            diagnostics=[Diagnostic(level="warning", code="soft_warning", message="可继续")],
        ),
    )

    action = context.service.confirm_stage(
        context.run_id,
        "5a-image-prompts",
        StageConfirmRequest(expected_revision=1),
    )

    assert len(context.queue.calls) == 1
    assert action["stages"][5]["status"] == "confirmed"
    assert _count(context.database, keyframe_version) == 1
