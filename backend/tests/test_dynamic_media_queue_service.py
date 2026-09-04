from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import func, insert, select, update

from app.db import Database, migrate_schema, utc_now
from app.dynamic_pipeline.media_queue_service import (
    DynamicMediaQueueService,
    MediaQueueError,
)
from app.dynamic_pipeline.prompt_ir import (
    CompiledPromptBundle,
    ImagePromptBundle,
    TimelineSegment,
    VideoPromptBundle,
)
from app.dynamic_pipeline.prompt_stage import PromptStageOutput, stage_output_to_stage_json
from app.dynamic_pipeline.schemas import ArtifactMeta, Diagnostic, ReferenceSlot
from app.jobs.repository import JobRepository
from app.models.tables import (
    asset,
    chapter,
    job,
    keyframe_version,
    project,
    shot,
    video_version,
    workflow_run,
    workflow_stage,
)
from app.services.job_service import JobService
from app.services.media_service import MediaService
from app.services.utils import json_dumps


@dataclass
class QueueContext:
    database: Database
    service: DynamicMediaQueueService
    run_id: str
    project_id: str
    chapter_id: str
    stage_5a_id: str
    stage_5b_id: str
    shot_ids: dict[str, str]
    enqueued: list[str]


def _artifact(signature: str, provider_id: str, provider_model: str) -> ArtifactMeta:
    return ArtifactMeta(
        schema_version="dynamic-prompt-stage-test-v1",
        normalizer_version="dynamic-prompt-test-v1",
        input_signature=signature,
        content_signature=f"content:{signature}",
        source_attempt=1,
        provider_id=provider_id,
        provider_model=provider_model,
    )


def _reference_plan() -> dict[str, Any]:
    return {
        "slots": [
            ReferenceSlot(
                index=1,
                role="hero",
                purpose="clean_identity",
                relative_path="references/hero.png",
            ).model_dump(mode="json")
        ],
        "actual_local_files": ["references/hero.png"],
        "actual_urls": [],
        "text_mapping": {
            "@Image1": {
                "index": 1,
                "source": "local",
                "local_file": "references/hero.png",
            }
        },
        "snapshot_hash": "reference-snapshot-1",
    }


def _bundle(
    *,
    stage_key: str,
    external_key: str,
    shot_no: int,
    prompt: str,
    transition: str = "scene_change",
    previous_end_frame_path: str | None = None,
    blocked: bool = False,
) -> CompiledPromptBundle:
    reference_plan = _reference_plan() if external_key.endswith("a") else None
    image = ImagePromptBundle(
        prompt=f"静止画：{prompt}",
        negative_prompt="文字，水印",
        reference_slots=(
            [ReferenceSlot.model_validate(item) for item in reference_plan["slots"]]
            if reference_plan
            else []
        ),
        semantic_plan={"state": "standing"},
    )
    video = VideoPromptBundle(
        prompt=f"视频：{prompt}",
        negative_prompt="文字，水印",
        edit_duration_sec=6,
        generation_duration_sec=6,
        timeline=[TimelineSegment(start=0, end=6, action="稳定移动")],
        end_state="动作结束并保持画面稳定",
        semantic_plan={
            "continuity": {"transition_type": transition},
            "action": "稳定移动",
        },
        reference_slots=(
            [ReferenceSlot.model_validate(item) for item in reference_plan["slots"]]
            if reference_plan
            else []
        ),
    )
    provenance: dict[str, Any] = {
        "shot_id": f"plan-shot-{external_key}",
        "external_key": external_key,
        "ordinal": shot_no,
        "shot_no": shot_no,
        "reference_plan": reference_plan,
        "style_version": "style-test-v1",
        "asset_versions": [{"asset_id": "asset-scene", "revision": 1}],
        "provider_capabilities": {
            "provider_id": "provider-image" if stage_key.startswith("5a") else "provider-video",
            "model": "image-model" if stage_key.startswith("5a") else "video-model",
            "supports_multiple_images": True,
            "max_reference_images": 3,
        },
    }
    if previous_end_frame_path:
        provenance["continuity"] = {"previous_end_frame_path": previous_end_frame_path}
    diagnostics = (
        [
            Diagnostic(
                level="blocking",
                code="test_blocked_target",
                message="测试目标被明确阻断",
                path=f"bundles.{external_key}",
            )
        ]
        if blocked
        else []
    )
    return CompiledPromptBundle(
        status="blocked" if blocked else "ready",
        image=image if stage_key.startswith("5a") else None,
        video=video if stage_key.startswith("5b") else None,
        diagnostics=diagnostics,
        provenance=provenance,
        generated={"image": image} if stage_key.startswith("5a") else {"video": video},
        final={"image": image} if stage_key.startswith("5a") else {"video": video},
    )


def _stage_output(
    *,
    run_id: str,
    stage_id: str,
    stage_key: str,
    signature: str,
    prompts: MappingLike,
    blocked: bool = False,
) -> dict[str, Any]:
    provider_id = "provider-image" if stage_key.startswith("5a") else "provider-video"
    provider_model = "image-model" if stage_key.startswith("5a") else "video-model"
    bundles: dict[str, CompiledPromptBundle] = {}
    for key, value in prompts.items():
        if isinstance(value, MappingLikeValue):
            bundles[key] = _bundle(
                stage_key=stage_key,
                external_key=value.external_key,
                shot_no=value.shot_no,
                prompt=value.prompt,
                transition=value.transition,
                previous_end_frame_path=value.previous_end_frame_path,
                blocked=blocked or value.blocked,
            )
        else:
            bundles[key] = value
    output = PromptStageOutput(
        stage_key=stage_key,  # type: ignore[arg-type]
        artifact_meta=_artifact(signature, provider_id, provider_model),
        bundles=bundles,
        target_duration_sec=12,
        status="blocked" if blocked else "ready",
        diagnostics=(
            [
                Diagnostic(
                    level="blocking",
                    code="test_stage_blocked",
                    message="测试阶段被明确阻断",
                )
            ]
            if blocked
            else []
        ),
        provenance={
            "workflow_run_id": run_id,
            "workflow_stage_id": stage_id,
            "input_signature": signature,
            "shot_keys": list(bundles),
            "provider_id": provider_id,
            "provider_model": provider_model,
        },
    )
    return stage_output_to_stage_json(output)


@dataclass(frozen=True)
class MappingLikeValue:
    external_key: str
    shot_no: int
    prompt: str
    transition: str = "scene_change"
    previous_end_frame_path: str | None = None
    blocked: bool = False


class MappingLike(dict[str, MappingLikeValue]):
    pass


@pytest.fixture
def queue_context(tmp_path: Path) -> Iterator[QueueContext]:
    database = Database(f"sqlite:///{(tmp_path / 'dynamic-media-queue.db').as_posix()}")
    migrate_schema(database)
    database.seed_mock_providers()
    now = utc_now()
    project_id = "queue-project"
    chapter_id = "queue-chapter"
    run_id = "queue-run"
    stage_5a_id = "queue-stage-5a"
    stage_5b_id = "queue-stage-5b"
    shot_ids = {"shot-a": "queue-shot-a", "shot-b": "queue-shot-b"}
    # 项目目录持久化存储字段，供入队前校验上一镜末帧文件存在性。
    project_root = tmp_path / "queue-project"
    (project_root / "frames").mkdir(parents=True, exist_ok=True)
    (project_root / "frames" / "shot-a-end.png").write_bytes(b"fake-end-frame")
    with database.engine.begin() as connection:
        connection.execute(
            insert(project).values(
                id=project_id,
                name="动态媒体队列测试项目",
                storage_root=str(tmp_path),
                storage_name="queue-project",
                storage_layout="categorized",
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
                source_signature="chapter-source",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(asset).values(
                id="asset-scene",
                project_id=project_id,
                asset_type="scene",
                name="测试场景",
                profile_json=json_dumps({"layout": "测试布局"}),
                visual_anchor_json=json_dumps({"layout": "测试布局"}),
                status="confirmed",
                revision=1,
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
                config_snapshot_json=json_dumps({"target_duration_sec": 12}),
                asset_mapping_json=json_dumps([]),
                input_signature="run-input",
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
                    "id": stage_5a_id,
                    "workflow_run_id": run_id,
                    "stage_key": "5a-image-prompts",
                    "ordinal": 5,
                    "status": "confirmed",
                    "input_signature": "stage-5a-input",
                    "raw_output_json": "{}",
                    "normalized_output_json": "{}",
                    "draft_output_json": "{}",
                    "output_refs_json": "[]",
                    "gate_result_json": "{}",
                    "attempt": 1,
                    "error": "",
                    "created_at": now,
                    "updated_at": now,
                },
                {
                    "id": stage_5b_id,
                    "workflow_run_id": run_id,
                    "stage_key": "5b-video-prompts",
                    "ordinal": 5,
                    "status": "confirmed",
                    "input_signature": "stage-5b-input",
                    "raw_output_json": "{}",
                    "normalized_output_json": "{}",
                    "draft_output_json": "{}",
                    "output_refs_json": "[]",
                    "gate_result_json": "{}",
                    "attempt": 1,
                    "error": "",
                    "created_at": now,
                    "updated_at": now,
                },
            ],
        )
        connection.execute(
            insert(shot),
            [
                {
                    "id": shot_ids["shot-a"],
                    "project_id": project_id,
                    "chapter_id": chapter_id,
                    "shot_no": 1,
                    "shot_order": 1,
                    "source_type": "screenplay",
                    "external_key": "shot-a",
                    "scene_asset_id": "asset-scene",
                    "status": "confirmed",
                    "created_at": now,
                    "updated_at": now,
                },
                {
                    "id": shot_ids["shot-b"],
                    "project_id": project_id,
                    "chapter_id": chapter_id,
                    "shot_no": 2,
                    "shot_order": 2,
                    "source_type": "screenplay",
                    "external_key": "shot-b",
                    "scene_asset_id": "asset-scene",
                    "transition_from_prev": "continuous",
                    "status": "confirmed",
                    "created_at": now,
                    "updated_at": now,
                },
            ],
        )

    enqueued: list[str] = []

    def enqueue(job_id: str) -> bool:
        with database.engine.connect() as connection:
            assert connection.scalar(select(job.c.id).where(job.c.id == job_id)) == job_id
        enqueued.append(job_id)
        return True

    service = DynamicMediaQueueService(
        database,
        job_repository=JobRepository(database),
        enqueue=enqueue,
    )
    try:
        yield QueueContext(
            database=database,
            service=service,
            run_id=run_id,
            project_id=project_id,
            chapter_id=chapter_id,
            stage_5a_id=stage_5a_id,
            stage_5b_id=stage_5b_id,
            shot_ids=shot_ids,
            enqueued=enqueued,
        )
    finally:
        database.dispose()


def _write_stage(
    context: QueueContext,
    stage_id: str,
    output: dict[str, Any],
) -> None:
    with context.database.engine.begin() as connection:
        connection.execute(
            update(workflow_stage)
            .where(workflow_stage.c.id == stage_id)
            .values(
                normalized_output_json=json_dumps(output),
                draft_output_json=json_dumps(output),
            )
        )


def _set_draft(context: QueueContext, stage_id: str, output: dict[str, Any]) -> None:
    with context.database.engine.begin() as connection:
        connection.execute(
            update(workflow_stage)
            .where(workflow_stage.c.id == stage_id)
            .values(draft_output_json=json_dumps(output))
        )


def _run_revision(context: QueueContext) -> int:
    with context.database.engine.connect() as connection:
        revision = connection.scalar(
            select(workflow_run.c.revision).where(workflow_run.c.id == context.run_id)
        )
    assert revision is not None
    return int(revision)


def _stage_json(
    context: QueueContext,
    stage_id: str,
    stage_key: str,
    *,
    signature: str,
    prompts: MappingLike,
    blocked: bool = False,
) -> dict[str, Any]:
    return _stage_output(
        run_id=context.run_id,
        stage_id=stage_id,
        stage_key=stage_key,
        signature=signature,
        prompts=prompts,
        blocked=blocked,
    )


def test_queue_5a_creates_keyframe_version_and_job_after_commit(
    queue_context: QueueContext,
) -> None:
    context = queue_context
    output = _stage_json(
        context,
        context.stage_5a_id,
        "5a-image-prompts",
        signature="stage-5a-input",
        prompts=MappingLike({"bundle-a": MappingLikeValue("shot-a", 1, "主角站在测试场景中")}),
    )
    _write_stage(context, context.stage_5a_id, output)

    result = context.service.queue_confirmed_prompt_stage(
        workflow_run_id=context.run_id,
        workflow_stage_id=context.stage_5a_id,
        expected_revision=1,
        provider_id="mock-image",
        provider_model="image-model",
    )

    assert result["stage_key"] == "5a-image-prompts"
    assert result["status"] == "ready"
    assert len(result["targets"]) == 1
    assert len(result["versions"]) == 1
    assert len(result["jobs"]) == 1
    queued_job = result["jobs"][0]
    version = result["versions"][0]
    assert queued_job["job_type"] == "generate_keyframe"
    assert queued_job["target_type"] == "keyframe"
    assert queued_job["target_id"] == version["id"]
    assert queued_job["provider_kind"] == "image"
    assert version["id"].startswith("dynamic-keyframe:queue-run:queue-stage-5a:shot-a")
    assert version["status"] == "queued"
    assert version["source"] == "generated"
    assert version["prompt_snapshot"] == "静止画：主角站在测试场景中"
    assert version["negative_prompt_snapshot"] == "文字，水印"
    assert version["prompt_ir_json"]["prompt"] == version["prompt_snapshot"]
    assert version["provenance_json"]["workflow_run_id"] == context.run_id
    assert version["provenance_json"]["workflow_stage_id"] == context.stage_5a_id
    assert queued_job["input_data"]["plan"]["external_key"] == "shot-a"
    assert queued_job["input_data"]["reference"]["local"] == ["references/hero.png"]
    assert context.enqueued == [queued_job["id"]]
    assert result["revision"] == 2

    with context.database.engine.connect() as connection:
        stored_stage = connection.execute(
            select(workflow_stage).where(workflow_stage.c.id == context.stage_5a_id)
        ).one()
        stored_job = connection.execute(select(job).where(job.c.id == queued_job["id"])).one()
        stored_version = connection.execute(
            select(keyframe_version).where(keyframe_version.c.id == version["id"])
        ).one()
    assert stored_stage.status == "confirmed"
    assert json.loads(stored_stage.output_refs_json)[0]["capture_mode"] == "dynamic_media_queue"
    assert stored_job.status == "queued"
    assert stored_version.is_current == 0


def test_queue_5b_creates_video_jobs_with_keyframe_and_previous_video_dependencies(
    queue_context: QueueContext,
) -> None:
    context = queue_context
    _write_stage(
        context,
        context.stage_5a_id,
        _stage_json(
            context,
            context.stage_5a_id,
            "5a-image-prompts",
            signature="stage-5a-input",
            prompts=MappingLike(
                {
                    "bundle-b": MappingLikeValue("shot-b", 2, "第二镜静止画"),
                    "bundle-a": MappingLikeValue("shot-a", 1, "第一镜静止画"),
                }
            ),
        ),
    )
    first = context.service.queue_confirmed_prompt_stage(
        workflow_run_id=context.run_id,
        workflow_stage_id=context.stage_5a_id,
        expected_revision=1,
        provider_id="mock-image",
        provider_model="image-model",
    )
    assert first["revision"] == 2

    _write_stage(
        context,
        context.stage_5b_id,
        _stage_json(
            context,
            context.stage_5b_id,
            "5b-video-prompts",
            signature="stage-5b-input",
            prompts=MappingLike(
                {
                    "bundle-b": MappingLikeValue(
                        "shot-b",
                        2,
                        "第二镜视频",
                        transition="continuous",
                        previous_end_frame_path="frames/shot-a-end.png",
                    ),
                    "bundle-a": MappingLikeValue("shot-a", 1, "第一镜视频"),
                }
            ),
        ),
    )
    second = context.service.queue_confirmed_prompt_stage(
        workflow_run_id=context.run_id,
        workflow_stage_id=context.stage_5b_id,
        expected_revision=2,
        provider_id="mock-video",
        provider_model="video-model",
    )

    assert second["status"] == "ready"
    assert [item["external_key"] for item in second["targets"]] == ["shot-a", "shot-b"]
    assert len(second["jobs"]) == 2
    jobs_by_external = {item["input_data"]["external_key"]: item for item in second["jobs"]}
    first_video = jobs_by_external["shot-a"]
    second_video = jobs_by_external["shot-b"]
    keyframe_jobs = {item["input_data"]["external_key"]: item for item in first["jobs"]}
    assert first_video["job_type"] == "generate_video"
    assert first_video["target_type"] == "video"
    assert first_video["provider_kind"] == "video"
    assert first_video["depends_on"] == [keyframe_jobs["shot-a"]["id"]]
    assert second_video["depends_on"] == [
        keyframe_jobs["shot-b"]["id"],
        first_video["id"],
    ]
    assert all(item["status"] == "queued" for item in second["jobs"])
    assert all(item["status"] == "queued" for item in second["versions"])
    assert second["revision"] == 3

    with context.database.engine.connect() as connection:
        video_rows = connection.execute(
            select(video_version).where(
                video_version.c.shot_id.in_(list(context.shot_ids.values()))
            )
        ).all()
    assert len(video_rows) == 2
    assert {row.keyframe_version_id for row in video_rows} == {
        item["target_id"] for item in first["jobs"]
    }


def test_queue_5b_skips_end_frame_check_while_previous_video_is_generating(
    queue_context: QueueContext,
) -> None:
    context = queue_context
    _write_stage(
        context,
        context.stage_5a_id,
        _stage_json(
            context,
            context.stage_5a_id,
            "5a-image-prompts",
            signature="stage-5a-input",
            prompts=MappingLike(
                {
                    "bundle-b": MappingLikeValue("shot-b", 2, "第二镜静止画"),
                    "bundle-a": MappingLikeValue("shot-a", 1, "第一镜静止画"),
                }
            ),
        ),
    )
    first = context.service.queue_confirmed_prompt_stage(
        workflow_run_id=context.run_id,
        workflow_stage_id=context.stage_5a_id,
        expected_revision=1,
        provider_id="mock-image",
        provider_model="image-model",
    )
    assert first["revision"] == 2

    _write_stage(
        context,
        context.stage_5b_id,
        _stage_json(
            context,
            context.stage_5b_id,
            "5b-video-prompts",
            signature="stage-5b-input",
            prompts=MappingLike(
                {
                    "bundle-b": MappingLikeValue(
                        "shot-b",
                        2,
                        "第二镜视频",
                        transition="continuous",
                        # 上一镜视频尚在生成（queued），末帧将在生成后自动产生，
                        # 此时即使文件还不存在也不得误阻断。
                        previous_end_frame_path="frames/missing-until-generated.png",
                    ),
                    "bundle-a": MappingLikeValue("shot-a", 1, "第一镜视频"),
                }
            ),
        ),
    )
    result = context.service.queue_confirmed_prompt_stage(
        workflow_run_id=context.run_id,
        workflow_stage_id=context.stage_5b_id,
        expected_revision=2,
        provider_id="mock-video",
        provider_model="video-model",
    )

    assert result["status"] == "ready"
    assert not [
        item
        for item in result["diagnostics"]
        if item["code"] == "media_queue_previous_end_frame_unavailable"
    ]
    assert {item["input_data"]["external_key"] for item in result["jobs"]} == {"shot-a", "shot-b"}


def test_queue_5b_blocks_missing_previous_end_frame_after_previous_video_terminal(
    queue_context: QueueContext,
) -> None:
    context = queue_context
    _write_stage(
        context,
        context.stage_5a_id,
        _stage_json(
            context,
            context.stage_5a_id,
            "5a-image-prompts",
            signature="stage-5a-input",
            prompts=MappingLike(
                {
                    "bundle-b": MappingLikeValue("shot-b", 2, "第二镜静止画"),
                    "bundle-a": MappingLikeValue("shot-a", 1, "第一镜静止画"),
                }
            ),
        ),
    )
    first = context.service.queue_confirmed_prompt_stage(
        workflow_run_id=context.run_id,
        workflow_stage_id=context.stage_5a_id,
        expected_revision=1,
        provider_id="mock-image",
        provider_model="image-model",
    )
    assert first["revision"] == 2

    _write_stage(
        context,
        context.stage_5b_id,
        _stage_json(
            context,
            context.stage_5b_id,
            "5b-video-prompts",
            signature="stage-5b-input",
            prompts=MappingLike(
                {
                    "bundle-b": MappingLikeValue(
                        "shot-b",
                        2,
                        "第二镜视频",
                        transition="continuous",
                        previous_end_frame_path="frames/not-exist-end.png",
                    ),
                    "bundle-a": MappingLikeValue("shot-a", 1, "第一镜视频"),
                }
            ),
        ),
    )
    second = context.service.queue_confirmed_prompt_stage(
        workflow_run_id=context.run_id,
        workflow_stage_id=context.stage_5b_id,
        expected_revision=2,
        provider_id="mock-video",
        provider_model="video-model",
    )
    assert second["revision"] == 3

    # 模拟上一镜视频已经生成完成（终态），而其末帧文件从未落盘。
    with context.database.engine.begin() as connection:
        connection.execute(
            update(job)
            .where(
                job.c.workflow_run_id == context.run_id,
                job.c.job_type == "generate_video",
                job.c.input_json.like('%"external_key":"shot-a"%'),
            )
            .values(status="completed", finished_at=utc_now())
        )

    # 重新入队（幂等复用 shot-a 已完成的 Job）：上一镜已终态但末帧缺失，
    # 必须在入队前显式阻断 shot-b，错误信息写明哪个镜头缺末帧。
    result = context.service.queue_confirmed_prompt_stage(
        workflow_run_id=context.run_id,
        workflow_stage_id=context.stage_5b_id,
        expected_revision=3,
        provider_id="mock-video",
        provider_model="video-model",
    )

    blocked_diagnostics = [
        item
        for item in result["diagnostics"]
        if item["code"] == "media_queue_previous_end_frame_unavailable"
    ]
    assert blocked_diagnostics
    assert "shot-b" in blocked_diagnostics[0]["message"]
    assert blocked_diagnostics[0]["details"]["external_key"] == "shot-b"
    blocked_target = next(
        item for item in result["targets"] if item["external_key"] == "shot-b"
    )
    assert blocked_target["status"] == "blocked"
    # 缺末帧的 shot-b 不入队，shot-a 复用已完成的 Job。
    enqueued_external_keys = {
        item["input_data"]["external_key"] for item in result["jobs"]
    }
    assert enqueued_external_keys == {"shot-a"}
    with context.database.engine.connect() as connection:
        video_rows = connection.execute(
            select(video_version).where(
                video_version.c.shot_id == context.shot_ids["shot-b"]
            )
        ).all()
    assert len(video_rows) == 1  # 第一轮已创建，本轮不新增也不入队


def test_queue_5b_passes_end_frame_check_when_file_exists_after_terminal(
    queue_context: QueueContext,
) -> None:
    context = queue_context
    _write_stage(
        context,
        context.stage_5a_id,
        _stage_json(
            context,
            context.stage_5a_id,
            "5a-image-prompts",
            signature="stage-5a-input",
            prompts=MappingLike(
                {
                    "bundle-b": MappingLikeValue("shot-b", 2, "第二镜静止画"),
                    "bundle-a": MappingLikeValue("shot-a", 1, "第一镜静止画"),
                }
            ),
        ),
    )
    first = context.service.queue_confirmed_prompt_stage(
        workflow_run_id=context.run_id,
        workflow_stage_id=context.stage_5a_id,
        expected_revision=1,
        provider_id="mock-image",
        provider_model="image-model",
    )
    assert first["revision"] == 2

    # fixture 已创建 frames/shot-a-end.png；上一镜终态后末帧确实存在，
    # 检查必须通过而不是阻断。
    _write_stage(
        context,
        context.stage_5b_id,
        _stage_json(
            context,
            context.stage_5b_id,
            "5b-video-prompts",
            signature="stage-5b-input",
            prompts=MappingLike(
                {
                    "bundle-b": MappingLikeValue(
                        "shot-b",
                        2,
                        "第二镜视频",
                        transition="continuous",
                        previous_end_frame_path="frames/shot-a-end.png",
                    ),
                    "bundle-a": MappingLikeValue("shot-a", 1, "第一镜视频"),
                }
            ),
        ),
    )
    second = context.service.queue_confirmed_prompt_stage(
        workflow_run_id=context.run_id,
        workflow_stage_id=context.stage_5b_id,
        expected_revision=2,
        provider_id="mock-video",
        provider_model="video-model",
    )
    assert second["revision"] == 3

    with context.database.engine.begin() as connection:
        connection.execute(
            update(job)
            .where(
                job.c.workflow_run_id == context.run_id,
                job.c.job_type == "generate_video",
                job.c.input_json.like('%"external_key":"shot-a"%'),
            )
            .values(status="completed", finished_at=utc_now())
        )
    result = context.service.queue_confirmed_prompt_stage(
        workflow_run_id=context.run_id,
        workflow_stage_id=context.stage_5b_id,
        expected_revision=3,
        provider_id="mock-video",
        provider_model="video-model",
    )

    assert not [
        item
        for item in result["diagnostics"]
        if item["code"] == "media_queue_previous_end_frame_unavailable"
    ]
    assert {item["input_data"]["external_key"] for item in result["jobs"]} == {"shot-a", "shot-b"}


def test_queue_same_signature_is_idempotent_and_changed_signature_creates_new_version(
    queue_context: QueueContext,
) -> None:
    context = queue_context
    first_output = _stage_json(
        context,
        context.stage_5a_id,
        "5a-image-prompts",
        signature="stage-5a-input",
        prompts=MappingLike({"bundle-a": MappingLikeValue("shot-a", 1, "第一版提示词")}),
    )
    _write_stage(context, context.stage_5a_id, first_output)
    first = context.service.queue_confirmed_prompt_stage(
        workflow_run_id=context.run_id,
        workflow_stage_id=context.stage_5a_id,
        expected_revision=1,
        provider_id="mock-image",
        provider_model="image-model",
    )
    repeated = context.service.queue_confirmed_prompt_stage(
        workflow_run_id=context.run_id,
        workflow_stage_id=context.stage_5a_id,
        expected_revision=first["revision"],
        provider_id="mock-image",
        provider_model="image-model",
    )
    assert repeated["versions"][0]["id"] == first["versions"][0]["id"]
    assert repeated["jobs"][0]["id"] == first["jobs"][0]["id"]
    with context.database.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(keyframe_version)) == 1
        assert (
            connection.scalar(
                select(func.count()).select_from(job).where(job.c.job_type == "generate_keyframe")
            )
            == 1
        )

    changed_output = _stage_json(
        context,
        context.stage_5a_id,
        "5a-image-prompts",
        signature="stage-5a-input",
        prompts=MappingLike({"bundle-a": MappingLikeValue("shot-a", 1, "第二版提示词")}),
    )
    _set_draft(context, context.stage_5a_id, changed_output)
    changed = context.service.queue_confirmed_prompt_stage(
        workflow_run_id=context.run_id,
        workflow_stage_id=context.stage_5a_id,
        expected_revision=repeated["revision"],
        provider_id="mock-image",
        provider_model="image-model",
    )
    assert changed["versions"][0]["id"] != first["versions"][0]["id"]
    assert changed["jobs"][0]["id"] != first["jobs"][0]["id"]
    assert changed["versions"][0]["input_signature"] != first["versions"][0]["input_signature"]
    with context.database.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(keyframe_version)) == 2
        assert (
            connection.scalar(
                select(func.count()).select_from(job).where(job.c.job_type == "generate_keyframe")
            )
            == 2
        )


def test_blocked_target_does_not_create_media_version_or_job(queue_context: QueueContext) -> None:
    context = queue_context
    output = _stage_json(
        context,
        context.stage_5a_id,
        "5a-image-prompts",
        signature="stage-5a-input",
        prompts=MappingLike(
            {"bundle-a": MappingLikeValue("shot-a", 1, "阻断提示词", blocked=True)}
        ),
        blocked=True,
    )
    _write_stage(context, context.stage_5a_id, output)
    result = context.service.queue_confirmed_prompt_stage(
        workflow_run_id=context.run_id,
        workflow_stage_id=context.stage_5a_id,
        expected_revision=1,
        provider_id="mock-image",
        provider_model="image-model",
    )
    assert result["status"] == "blocked"
    assert result["jobs"] == []
    assert result["versions"] == []
    assert result["targets"][0]["status"] == "blocked"
    assert any(item["code"] == "test_blocked_target" for item in result["diagnostics"])
    with context.database.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(keyframe_version)) == 0
        assert (
            connection.scalar(
                select(func.count()).select_from(job).where(job.c.workflow_run_id == context.run_id)
            )
            == 0
        )


@pytest.mark.parametrize(
    ("stage_status", "expected_revision", "message_part"),
    [
        ("queued", 1, "尚未确认"),
        ("confirmed", 99, "revision 冲突"),
    ],
)
def test_unconfirmed_or_old_revision_returns_clear_error_without_writes(
    queue_context: QueueContext,
    stage_status: str,
    expected_revision: int,
    message_part: str,
) -> None:
    context = queue_context
    output = _stage_json(
        context,
        context.stage_5a_id,
        "5a-image-prompts",
        signature="stage-5a-input",
        prompts=MappingLike({"bundle-a": MappingLikeValue("shot-a", 1, "正常提示词")}),
    )
    _write_stage(context, context.stage_5a_id, output)
    with context.database.engine.begin() as connection:
        connection.execute(
            update(workflow_stage)
            .where(workflow_stage.c.id == context.stage_5a_id)
            .values(status=stage_status)
        )
    with pytest.raises(MediaQueueError, match=message_part):
        context.service.queue_confirmed_prompt_stage(
            workflow_run_id=context.run_id,
            workflow_stage_id=context.stage_5a_id,
            expected_revision=expected_revision,
            provider_id="mock-image",
            provider_model="image-model",
        )
    with context.database.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(keyframe_version)) == 0
        assert (
            connection.scalar(
                select(func.count()).select_from(job).where(job.c.workflow_run_id == context.run_id)
            )
            == 0
        )


def test_missing_or_duplicate_external_key_blocks_only_target_without_guessing_shot_no(
    queue_context: QueueContext,
) -> None:
    context = queue_context
    missing_output = _stage_json(
        context,
        context.stage_5a_id,
        "5a-image-prompts",
        signature="stage-5a-input",
        prompts=MappingLike(
            {"bundle-missing": MappingLikeValue("missing-external", 1, "找不到镜头")}
        ),
    )
    _write_stage(context, context.stage_5a_id, missing_output)
    missing = context.service.queue_confirmed_prompt_stage(
        workflow_run_id=context.run_id,
        workflow_stage_id=context.stage_5a_id,
        expected_revision=1,
        provider_id="mock-image",
        provider_model="image-model",
    )
    assert missing["jobs"] == []
    assert missing["versions"] == []
    assert any(
        item["code"] == "media_queue_shot_external_key_not_found" for item in missing["diagnostics"]
    )

    with context.database.engine.begin() as connection:
        connection.execute(
            insert(shot).values(
                id="queue-shot-a-duplicate",
                project_id=context.project_id,
                chapter_id=context.chapter_id,
                shot_no=99,
                shot_order=99,
                source_type="screenplay",
                external_key="shot-a",
                scene_asset_id="asset-scene",
                status="confirmed",
                created_at=utc_now(),
                updated_at=utc_now(),
            )
        )
        connection.execute(
            update(workflow_run).where(workflow_run.c.id == context.run_id).values(revision=1)
        )
    duplicate_output = _stage_json(
        context,
        context.stage_5a_id,
        "5a-image-prompts",
        signature="stage-5a-input",
        prompts=MappingLike({"bundle-a": MappingLikeValue("shot-a", 1, "重复映射")}),
    )
    _write_stage(context, context.stage_5a_id, duplicate_output)
    duplicate = context.service.queue_confirmed_prompt_stage(
        workflow_run_id=context.run_id,
        workflow_stage_id=context.stage_5a_id,
        expected_revision=1,
        provider_id="mock-image",
        provider_model="image-model",
    )
    assert duplicate["jobs"] == []
    assert duplicate["versions"] == []
    assert any(
        item["code"] == "media_queue_shot_external_key_duplicate"
        for item in duplicate["diagnostics"]
    )


def test_enqueue_failure_does_not_rollback_committed_queued_rows(
    queue_context: QueueContext,
) -> None:
    context = queue_context
    output = _stage_json(
        context,
        context.stage_5a_id,
        "5a-image-prompts",
        signature="stage-5a-input",
        prompts=MappingLike({"bundle-a": MappingLikeValue("shot-a", 1, "提交后可见")}),
    )
    _write_stage(context, context.stage_5a_id, output)
    callback_ids: list[str] = []

    def failing_enqueue(job_id: str) -> bool:
        callback_ids.append(job_id)
        raise RuntimeError("queue temporarily unavailable")

    failing_service = DynamicMediaQueueService(
        context.database,
        job_repository=JobRepository(context.database),
        enqueue=failing_enqueue,
    )
    result = failing_service.queue_confirmed_prompt_stage(
        workflow_run_id=context.run_id,
        workflow_stage_id=context.stage_5a_id,
        expected_revision=1,
        provider_id="mock-image",
        provider_model="image-model",
    )
    assert callback_ids == [result["jobs"][0]["id"]]
    assert any(item["code"] == "media_queue_enqueue_failed" for item in result["diagnostics"])
    with context.database.engine.connect() as connection:
        stored_job = connection.execute(select(job).where(job.c.id == callback_ids[0])).one()
        stored_version = connection.execute(
            select(keyframe_version).where(keyframe_version.c.id == result["versions"][0]["id"])
        ).one()
    assert stored_job.status == "queued"
    assert stored_version.status == "queued"


def test_legacy_media_service_keyframe_smoke_still_uses_legacy_job_path(
    queue_context: QueueContext,
) -> None:
    context = queue_context
    with context.database.engine.begin() as connection:
        connection.execute(
            update(project)
            .where(project.c.id == context.project_id)
            .values(default_image_provider_id="mock-image")
        )
        connection.execute(
            update(shot)
            .where(shot.c.id == context.shot_ids["shot-a"])
            .values(
                image_prompt="旧手工关键帧提示词",
                reference_bindings_json="[]",
                scene_asset_id=None,
            )
        )

    class RecordingRunner:
        def __init__(self) -> None:
            self.ids: list[str] = []

        def enqueue(self, job_id: str) -> bool:
            self.ids.append(job_id)
            return True

    runner = RecordingRunner()
    legacy_jobs = JobService(
        context.database,
        repository=JobRepository(context.database),
        runner=cast(Any, runner),
    )
    legacy_media = MediaService(context.database, legacy_jobs)
    record = legacy_media.queue_keyframe(
        context.shot_ids["shot-a"],
        {"provider_id": "mock-image"},
    )
    assert record["job_type"] == "generate_keyframe"
    assert record["workflow_run_id"] is None
    assert record["workflow_stage_id"] is None
    assert runner.ids == [record["id"]]
    with context.database.engine.connect() as connection:
        legacy_version = connection.execute(
            select(keyframe_version).where(keyframe_version.c.id == record["target_id"])
        ).one()
    assert legacy_version.source == "generated"
    assert (
        legacy_version.provenance_json is None or json.loads(legacy_version.provenance_json) == {}
    )
