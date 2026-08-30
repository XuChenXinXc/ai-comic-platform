from __future__ import annotations

import asyncio
import copy
import hashlib
import io
import json
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest
from media_test_helpers import _PNG
from sqlalchemy import insert, select, update

from app.db import Database, migrate_schema, utc_now
from app.dynamic_pipeline.composition_preflight import CompositionPreflightResult
from app.dynamic_pipeline.delivery import (
    DeliveryExecutionError,
    DeliveryServiceError,
    DynamicDeliveryJobService,
    DynamicDeliveryService,
)
from app.dynamic_pipeline.stage_catalog import STAGE_KEYS
from app.jobs.dynamic_bridge import DynamicJobBridge, build_dynamic_idempotency_key
from app.jobs.repository import JobRepository
from app.models.tables import (
    api_provider,
    asset,
    chapter,
    chapter_composition,
    job,
    keyframe_version,
    project,
    shot,
    shot_audio,
    shot_subtitle,
    video_version,
    workflow_run,
    workflow_stage,
)
from app.services.composition_service import CompositionService
from app.services.dynamic_workflow_service import DynamicWorkflowService
from app.services.file_service import FileService
from app.services.job_service import JobService


@pytest.fixture
def delivery_context(tmp_path: Path):
    database = Database(f"sqlite:///{(tmp_path / 'delivery.db').as_posix()}")
    migrate_schema(database)
    now = utc_now()
    run_id = "run/partial delivery"
    project_id = "project-delivery"
    chapter_id = "chapter-delivery"
    with database.engine.begin() as connection:
        connection.execute(
            insert(project).values(
                id=project_id,
                name="交付测试项目",
                storage_root=str(tmp_path / "projects"),
                storage_name="交付测试项目",
                storage_layout="categorized",
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
                source_text="测试正文",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(workflow_run).values(
                id=run_id,
                project_id=project_id,
                chapter_id=chapter_id,
                source_kind="full_text",
                status="confirmed",
                current_stage="6-delivery",
                source_snapshot_json=json.dumps({"text": "测试正文"}),
                config_snapshot_json=json.dumps({}),
                asset_mapping_json=json.dumps([]),
                input_signature="run-signature",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(workflow_stage),
            [
                {
                    "id": f"{run_id}-stage-{index}",
                    "workflow_run_id": run_id,
                    "stage_key": stage_key,
                    "ordinal": index,
                    "status": "confirmed",
                    "input_signature": f"{stage_key}-signature",
                    "raw_output_json": json.dumps(
                        {
                            "api_key": "provider-api-secret",
                            "cookie": "session-cookie-secret",
                        }
                    ),
                    "normalized_output_json": json.dumps({"stage": stage_key}),
                    "draft_output_json": json.dumps({"stage": stage_key}),
                    "output_refs_json": "[]",
                    "gate_result_json": json.dumps({"status": "ready", "passed": True}),
                    "attempt": 1,
                    "created_at": now,
                    "updated_at": now,
                }
                for index, stage_key in enumerate(STAGE_KEYS)
            ],
        )
        connection.execute(
            insert(shot).values(
                id="shot-delivery-1",
                project_id=project_id,
                chapter_id=chapter_id,
                shot_no=1,
                shot_order=1,
                source_type="novel",
                source_ref="source-1",
                status="confirmed",
                created_at=now,
                updated_at=now,
            )
        )

    file_service = FileService(tmp_path, database=database)
    service = DynamicDeliveryService(database, file_service=file_service)
    try:
        yield database, service, file_service, run_id, project_id, chapter_id
    finally:
        database.dispose()


def test_build_snapshot_reads_json_media_shape_without_creating_resources(
    delivery_context,
) -> None:
    database, service, file_service, run_id, project_id, chapter_id = delivery_context
    project_path = file_service.project_path(project_id)
    before_counts = {
        table_name: database.engine.connect().execute(select(table)).fetchall()
        for table_name, table in (
            ("workflow_run", workflow_run),
            ("workflow_stage", workflow_stage),
            ("shot", shot),
        )
    }

    snapshot = service.build_snapshot(run_id)

    assert snapshot["workflow_run_id"] == run_id
    assert snapshot["project_id"] == project_id
    assert snapshot["chapter_id"] == chapter_id
    assert isinstance(snapshot["run"]["source_snapshot"], dict)
    assert isinstance(snapshot["stages"], list)
    assert len(snapshot["stages"]) == len(STAGE_KEYS)
    assert snapshot["shots"][0]["keyframe"] == []
    assert snapshot["shots"][0]["video"] == []
    assert snapshot["shots"][0]["audio"] == []
    assert snapshot["shots"][0]["subtitle"] == []
    assert snapshot["media"]["keyframes"] == []
    assert json.dumps(snapshot, ensure_ascii=False)
    snapshot_text = json.dumps(snapshot, ensure_ascii=False)
    assert "provider-api-secret" not in snapshot_text
    assert "session-cookie-secret" not in snapshot_text
    assert not project_path.exists()

    for table_name, table in (
        ("workflow_run", workflow_run),
        ("workflow_stage", workflow_stage),
        ("shot", shot),
    ):
        with database.engine.connect() as connection:
            after = connection.execute(select(table)).fetchall()
        assert len(after) == len(before_counts[table_name])


def test_build_snapshot_keeps_same_shot_media_isolated_between_dynamic_runs(
    delivery_context,
) -> None:
    database, service, _file_service, run_id, project_id, chapter_id = delivery_context
    now = utc_now()
    other_run_id = "run-other-delivery"
    other_stage_id = f"{other_run_id}-stage-5a-image-prompts"
    current_stage_id = f"{run_id}-stage-5"
    shot_id = "shot-delivery-1"
    with database.engine.begin() as connection:
        connection.execute(
            insert(workflow_run).values(
                id=other_run_id,
                project_id=project_id,
                chapter_id=chapter_id,
                source_kind="full_text",
                status="confirmed",
                current_stage="6-delivery",
                source_snapshot_json=json.dumps({"text": "other"}),
                config_snapshot_json=json.dumps({}),
                asset_mapping_json=json.dumps([]),
                input_signature="other-run-signature",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(workflow_stage).values(
                id=other_stage_id,
                workflow_run_id=other_run_id,
                stage_key="5a-image-prompts",
                ordinal=5,
                status="confirmed",
                input_signature="other-stage-signature",
                raw_output_json="{}",
                normalized_output_json="{}",
                draft_output_json="{}",
                output_refs_json="[]",
                gate_result_json=json.dumps({"status": "ready", "passed": True}),
                attempt=1,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(keyframe_version),
            [
                {
                    "id": "keyframe-dynamic-run-one",
                    "shot_id": shot_id,
                    "version_no": 1,
                    "status": "completed",
                    "is_current": 0,
                    "source": "generated",
                    "input_signature": "run-one-media-signature",
                    "provenance_json": json.dumps(
                        {
                            "workflow_run_id": run_id,
                            "workflow_stage_id": current_stage_id,
                            "upstream_input_signature": "5a-image-prompts-signature",
                        }
                    ),
                    "created_at": now,
                },
                {
                    "id": "keyframe-dynamic-run-two",
                    "shot_id": shot_id,
                    "version_no": 2,
                    "status": "completed",
                    "is_current": 1,
                    "source": "generated",
                    "input_signature": "run-two-media-signature",
                    "provenance_json": json.dumps(
                        {
                            "workflow_run_id": other_run_id,
                            "workflow_stage_id": other_stage_id,
                            "upstream_input_signature": "other-stage-signature",
                        }
                    ),
                    "created_at": now,
                },
                {
                    "id": "keyframe-legacy-version",
                    "shot_id": shot_id,
                    "version_no": 3,
                    "status": "completed",
                    "is_current": 0,
                    "source": "uploaded",
                    "input_signature": "legacy-signature",
                    "provenance_json": "{}",
                    "created_at": now,
                },
            ],
        )

    first = service.build_snapshot(run_id)
    second = service.build_snapshot(other_run_id)

    assert [item["id"] for item in first["keyframes"]] == ["keyframe-dynamic-run-one"]
    assert [item["id"] for item in second["keyframes"]] == ["keyframe-dynamic-run-two"]
    assert [item["id"] for item in first["versions"] if item["kind"] == "keyframe"] == [
        "keyframe-dynamic-run-one"
    ]


def test_preflight_missing_media_is_blocked_without_ffmpeg_or_mutation(
    delivery_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _database, service, _file_service, run_id, _project_id, _chapter_id = delivery_context
    calls: list[object] = []

    from app.services import composition_service

    monkeypatch.setattr(
        composition_service.subprocess,
        "run",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    original_build_snapshot = service.build_snapshot
    snapshot = original_build_snapshot(run_id)
    before = copy.deepcopy(snapshot)
    monkeypatch.setattr(service, "build_snapshot", lambda requested_run_id: snapshot)

    result = service.preflight(run_id)

    assert result.status == "blocked"
    assert result.missing
    assert result.estimated_jobs == 0
    assert calls == []
    assert snapshot == before


def test_export_strict_missing_contains_preflight_and_missing(delivery_context) -> None:
    _database, service, _file_service, run_id, _project_id, _chapter_id = delivery_context

    with pytest.raises(DeliveryServiceError, match=r"preflight.*missing"):
        service.export(run_id, exported_at="2026-08-18T00:00:00Z")


def test_partial_export_has_fixed_safe_filename_zip_metadata_and_redaction(
    delivery_context,
) -> None:
    _database, service, _file_service, run_id, _project_id, _chapter_id = delivery_context

    zip_bytes, filename, manifest = service.export(
        run_id,
        allow_partial=True,
        exported_at="2026-08-18T00:00:00Z",
    )

    assert filename == "dynamic-delivery-runpartialdelivery.zip"
    assert "/" not in filename
    assert "\\" not in filename
    assert manifest["export_mode"] == "partial"
    assert manifest["status"] == "partial"
    assert manifest["missing"]

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        names = archive.namelist()
        assert names[:8] == [
            "00_拆解.md",
            "01_人物小传.md",
            "02_节奏爽点.md",
            "03_详细剧本.md",
            "04_标准分镜.md",
            "05_图片提示词.md",
            "06_视频提示词.md",
            "README.md",
        ]
        assert "metadata/manifest.json" in names
        metadata = archive.read("metadata/manifest.json").decode("utf-8")
        assert "2026-08-18T00:00:00Z" in metadata
        assert "provider-api-secret" not in metadata
        assert "session-cookie-secret" not in metadata
        assert "<redacted>" in metadata


def test_invalid_run_and_project_relationship_raise_delivery_error(delivery_context) -> None:
    _database, service, _file_service, run_id, _project_id, _chapter_id = delivery_context

    with pytest.raises(DeliveryServiceError, match="workflow run"):
        service.build_snapshot("missing-run")
    with pytest.raises(DeliveryServiceError, match="run/project"):
        service.build_snapshot(run_id, project_id="other-project")


def test_checklist_is_read_only_and_returns_preflight(delivery_context) -> None:
    database, service, _file_service, run_id, _project_id, _chapter_id = delivery_context
    before = service.build_snapshot(run_id)

    checklist = service.get_checklist(run_id)

    before_by_key = {item["key"]: item for item in before["checklist"]}
    after_by_key = {item["key"]: item for item in checklist["checklist"]}
    assert set(after_by_key) == set(before_by_key)
    assert after_by_key["audio"]["status"] == "missing"
    assert after_by_key["composition"]["status"] == "planned"
    assert checklist["preflight"]["status"] == "blocked"
    assert checklist["preflight"]["missing"]
    with database.engine.connect() as connection:
        assert connection.execute(select(workflow_stage.c.stage_key)).all()


def test_checklist_references_reads_actual_asset_mapping_completion(delivery_context) -> None:
    database, service, _file_service, run_id, project_id, _chapter_id = delivery_context
    now = utc_now()
    with database.engine.begin() as connection:
        connection.execute(
            insert(asset).values(
                id="asset-reference-1",
                project_id=project_id,
                asset_type="character",
                name="参考角色",
                status="confirmed",
                revision=2,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            update(workflow_run)
            .where(workflow_run.c.id == run_id)
            .values(
                asset_mapping_json=json.dumps(
                    [
                        {
                            "workflow_asset_id": "workflow-character-1",
                            "asset_id": "asset-reference-1",
                            "usage": "character",
                            "locked_revision": 2,
                            "reuse_mode": "approved_existing",
                        }
                    ]
                )
            )
        )

    checklist = service.build_snapshot(run_id)["checklist"]
    references = next(item for item in checklist if item["key"] == "references")

    assert references["status"] == "completed"
    assert references["completed"] == 1
    assert references["total"] == 1


class _DeliveryRunner:
    def __init__(self, *, result: bool = True) -> None:
        self.enqueued: list[str] = []
        self.result = result

    def enqueue(self, job_id: str) -> bool:
        self.enqueued.append(job_id)
        return self.result


class _CompletedAudioService:
    def __init__(self, database: Database, repository: JobRepository, storage: FileService) -> None:
        self.database = database
        self.repository = repository
        self.storage = storage

    async def handle_job(self, record: dict[str, Any]) -> dict[str, Any]:
        current = self.repository.require(record["id"])
        if current["status"] == "completed":
            return current["result_data"]
        input_data = record["input_data"]
        audio_id = f"audio:{record['id']}"
        file_path = f"分镜配音/{audio_id.replace(':', '-')}.mp3"
        self.storage.write_bytes_atomic(record["project_id"], file_path, b"audio")
        now = utc_now()
        with self.database.engine.begin() as connection:
            connection.execute(
                insert(shot_audio).values(
                    id=audio_id,
                    shot_id=input_data["shot_id"],
                    dialogue_line_id=input_data["dialogue_line_id"],
                    line_order=input_data["line_order"],
                    dialogue_text=input_data["text"],
                    source_text_snapshot=input_data["source_text_snapshot"],
                    speaker_name=input_data["speaker"],
                    voice_id=input_data["voice_id"],
                    file_path=file_path,
                    duration_sec=1.0,
                    version_no=1,
                    provider_id=record["provider_id"],
                    model=input_data["provider_model"],
                    status="completed",
                    is_current=1,
                    source="generated",
                    input_signature=record["input_signature"],
                    provenance_json=json.dumps(
                        {
                            "phase": "6a-audio",
                            "workflow_run_id": record["workflow_run_id"],
                            "workflow_stage_id": record["workflow_stage_id"],
                            "input_signature": record["input_signature"],
                            "upstream_input_signature": input_data.get(
                                "upstream_input_signature", ""
                            ),
                        }
                    ),
                    error="",
                    created_at=now,
                )
            )
        result = {
            "status": "completed",
            "audio_id": audio_id,
            "shot_id": input_data["shot_id"],
            "dialogue_line_id": input_data["dialogue_line_id"],
            "file_path": file_path,
            "duration_sec": 1.0,
        }
        self.repository.complete(record["id"], result)
        return result


def test_dynamic_delivery_executes_audio_subtitle_and_queues_composition(
    delivery_context,
) -> None:
    database, _service, file_service, run_id, project_id, chapter_id = delivery_context
    now = utc_now()
    stage6_id = f"{run_id}-stage-7"
    shot_id = "shot-delivery-1"
    project_root = file_service.project_path(project_id)
    project_root.mkdir(parents=True, exist_ok=True)
    for relative_path in ("keyframes/shot.png", "videos/shot.mp4"):
        target = project_root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"media")
    with database.engine.begin() as connection:
        connection.execute(
            insert(api_provider).values(
                id="tts-delivery",
                kind="text",
                name="测试 TTS",
                adapter="mock",
                model="tts-model",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            update(workflow_run)
            .where(workflow_run.c.id == run_id)
            .values(
                status="queued",
                config_snapshot_json=json.dumps(
                    {
                        "generate_audio": True,
                        "audio_provider_id": "tts-delivery",
                        "generate_subtitle": True,
                        "generate_bgm": False,
                        "generate_composition": True,
                    }
                ),
            )
        )
        connection.execute(
            update(workflow_stage)
            .where(workflow_stage.c.id == stage6_id)
            .values(status="queued", input_signature="run-signature")
        )
        connection.execute(
            update(shot)
            .where(shot.c.id == shot_id)
            .values(
                external_key="shot-delivery-1",
                dialogue_json=json.dumps(
                    [{"dialogue_line_id": "line-1", "speaker": "主角", "text": "你好。"}]
                ),
                dialogue_cues_json=json.dumps(
                    [{"dialogue_line_id": "line-1", "speaker": "主角", "text": "你好。"}]
                ),
            )
        )
        connection.execute(
            insert(keyframe_version).values(
                id="keyframe-delivery-1",
                shot_id=shot_id,
                version_no=1,
                file_path="keyframes/shot.png",
                status="completed",
                is_current=1,
                source="generated",
                input_signature="run-signature",
                created_at=now,
            )
        )
        connection.execute(
            insert(video_version).values(
                id="video-delivery-1",
                shot_id=shot_id,
                version_no=1,
                file_path="videos/shot.mp4",
                actual_duration_sec=1.0,
                status="completed",
                is_current=1,
                source="generated",
                input_signature="run-signature",
                created_at=now,
            )
        )

    repository = JobRepository(database)
    top_job = repository.create_job(
        {
            "project_id": project_id,
            "chapter_id": chapter_id,
            "workflow_run_id": run_id,
            "workflow_stage_id": stage6_id,
            "job_type": "dynamic_stage_generate",
            "target_type": "workflow_stage",
            "target_id": stage6_id,
            "provider_kind": "text",
            "input_data": {"stage_key": "6-delivery"},
            "input_signature": "run-signature",
            "idempotency_key": "delivery-top-job",
        }
    )
    runner = _DeliveryRunner()
    jobs = JobService(database, repository=repository, runner=cast(Any, runner))
    composition = CompositionService(database, jobs)
    delivery = DynamicDeliveryJobService(
        database,
        repository=repository,
        audio_service=_CompletedAudioService(database, repository, file_service),
        composition_service=composition,
        file_service=file_service,
        enqueue=runner.enqueue,
    )

    result = asyncio.run(delivery.execute_job(top_job))

    assert result["status"] == "queued"
    assert {item["kind"] for item in result["subtasks"]} == {
        "audio",
        "subtitle",
        "bgm",
        "composition",
    }
    assert result["subtasks"][0]["status"] == "completed"
    assert result["subtasks"][-1]["status"] == "queued"
    assert repository.require(top_job["id"])["status"] == "completed"
    with database.engine.connect() as connection:
        assert connection.scalar(select(shot_subtitle.c.id))
        assert connection.scalar(select(chapter_composition.c.id))
        child_types = set(
            connection.scalars(
                select(job.c.job_type).where(
                    job.c.workflow_run_id == run_id,
                    job.c.id != top_job["id"],
                )
            ).all()
        )
        stage_status = connection.scalar(
            select(workflow_stage.c.status).where(workflow_stage.c.id == stage6_id)
        )
    assert {
        "generate_dialogue_audio",
        "generate_dynamic_subtitle",
        "compose_chapter",
    } <= child_types
    assert stage_status == "queued"
    composition_job = next(
        item
        for item in repository.list_by_workflow(run_id)
        if item["job_type"] == "compose_chapter"
    )
    composition_input = composition_job["input_data"]
    assert composition_job["id"] in runner.enqueued
    assert composition_job["workflow_run_id"] == run_id
    assert composition_job["workflow_stage_id"] == stage6_id
    assert composition_job["input_signature"] == "run-signature"
    assert composition_input["workflow_run_id"] == run_id
    assert composition_input["workflow_stage_id"] == stage6_id
    assert composition_input["workflow_stage_attempt"] == 1
    assert composition_input["workflow_run_revision"] == 1
    assert composition_input["input_signature"] == "run-signature"
    assert composition_job["depends_on"]
    assert all(
        repository.require(dependency_id)["status"] == "completed"
        for dependency_id in composition_job["depends_on"]
    )


def test_resume_after_audio_failure_retries_children_and_exports_current_attempt(
    delivery_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, service, file_service, run_id, project_id, chapter_id = delivery_context
    now = utc_now()
    stage6_id = f"{run_id}-stage-7"
    stage5a_id = f"{run_id}-stage-5"
    stage5b_id = f"{run_id}-stage-6"
    project_root = file_service.project_path(project_id)
    project_root.mkdir(parents=True, exist_ok=True)
    for relative_path, data in (
        ("keyframes/resume.png", _PNG),
        ("videos/resume.mp4", b"resume-video"),
    ):
        target = project_root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def media_provenance(stage_id: str, upstream_signature: str) -> dict[str, Any]:
        return {
            "workflow_run_id": run_id,
            "workflow_stage_id": stage_id,
            "workflow_stage_attempt": 1,
            "workflow_run_revision": 1,
            "input_signature": "media-own-signature",
            "upstream_input_signature": upstream_signature,
        }

    with database.engine.begin() as connection:
        connection.execute(
            update(workflow_run)
            .where(workflow_run.c.id == run_id)
            .values(
                status="queued",
                current_stage="6-delivery",
                config_snapshot_json=json.dumps(
                    {
                        "generate_audio": True,
                        "audio_provider_id": "tts-resume",
                        "generate_subtitle": True,
                        "generate_bgm": False,
                        "generate_composition": True,
                    }
                ),
            )
        )
        connection.execute(
            update(workflow_stage)
            .where(workflow_stage.c.id == stage6_id)
            .values(status="queued", input_signature="6-delivery-signature")
        )
        connection.execute(
            update(shot)
            .where(shot.c.id == "shot-delivery-1")
            .values(
                external_key="shot-delivery-1",
                dialogue_json=json.dumps(
                    [{"dialogue_line_id": "line-resume", "speaker": "主角", "text": "恢复"}]
                ),
                dialogue_cues_json=json.dumps(
                    [{"dialogue_line_id": "line-resume", "speaker": "主角", "text": "恢复"}]
                ),
            )
        )
        connection.execute(
            insert(api_provider).values(
                id="tts-resume",
                kind="tts",
                name="Resume TTS",
                adapter="mock",
                model="resume-model",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(keyframe_version).values(
                id="keyframe-resume",
                shot_id="shot-delivery-1",
                version_no=1,
                file_path="keyframes/resume.png",
                status="completed",
                is_current=1,
                source="generated",
                input_signature="media-own-signature",
                provenance_json=json.dumps(
                    media_provenance(stage5a_id, "5a-image-prompts-signature")
                ),
                created_at=now,
            )
        )
        connection.execute(
            insert(video_version).values(
                id="video-resume",
                shot_id="shot-delivery-1",
                version_no=1,
                file_path="videos/resume.mp4",
                actual_duration_sec=1.0,
                status="completed",
                is_current=1,
                source="generated",
                input_signature="media-own-signature",
                provenance_json=json.dumps(
                    media_provenance(stage5b_id, "5b-video-prompts-signature")
                ),
                created_at=now,
            )
        )

    repository = JobRepository(database)
    top_job = repository.create_job(
        {
            "project_id": project_id,
            "chapter_id": chapter_id,
            "workflow_run_id": run_id,
            "workflow_stage_id": stage6_id,
            "job_type": "dynamic_stage_generate",
            "target_type": "workflow_stage",
            "target_id": stage6_id,
            "provider_kind": "text",
            "input_data": {"stage_key": "6-delivery"},
            "input_signature": "6-delivery-signature",
            "idempotency_key": build_dynamic_idempotency_key(
                stage6_id,
                "workflow_stage",
                stage6_id,
                "6-delivery-signature",
                "dynamic_stage_generate",
            ),
        }
    )

    class FailingAudioService:
        async def handle_job(self, record: Mapping[str, Any]) -> dict[str, Any]:
            repository.fail(str(record["id"]), "首轮音频失败")
            return {"status": "failed", "job_id": record["id"], "error": "首轮音频失败"}

    enqueue_runner = _DeliveryRunner()
    composition = CompositionService(
        database,
        JobService(database, repository=repository, runner=cast(Any, enqueue_runner)),
    )
    delivery = DynamicDeliveryJobService(
        database,
        repository=repository,
        audio_service=FailingAudioService(),
        composition_service=composition,
        file_service=file_service,
        enqueue=enqueue_runner.enqueue,
    )
    first_result = asyncio.run(delivery.execute_job(top_job))
    assert first_result["status"] == "partial_failed"
    old_audio = next(
        item
        for item in repository.list_by_workflow(run_id)
        if item["job_type"] == "generate_dialogue_audio"
    )
    assert old_audio["status"] == "failed"
    with database.engine.connect() as connection:
        assert connection.scalar(
            select(workflow_stage.c.status).where(workflow_stage.c.id == stage6_id)
        ) == "partial_failed"
        assert connection.scalar(
            select(workflow_run.c.status).where(workflow_run.c.id == run_id)
        ) == "partial_failed"

    workflow = DynamicWorkflowService(
        database,
        storage=file_service,
        job_bridge=DynamicJobBridge(repository, enqueue=enqueue_runner.enqueue),
    )
    resumed = workflow.resume_run(run_id, expected_revision=2)
    assert resumed["run"]["status"] == "queued"
    jobs_after_resume = repository.list_by_workflow(run_id)
    new_audio = [
        item
        for item in jobs_after_resume
        if item["job_type"] == "generate_dialogue_audio" and item["id"] != old_audio["id"]
    ]
    assert len(new_audio) == 1
    assert new_audio[0]["status"] == "queued"
    assert new_audio[0]["input_data"]["delivery_generation"]
    assert new_audio[0]["input_signature"] != old_audio["input_signature"]
    resumed_top = repository.require(top_job["id"])
    assert resumed_top["status"] == "queued"
    assert new_audio[0]["id"] in resumed_top["depends_on"]
    assert old_audio["id"] not in resumed_top["depends_on"]

    completed_audio = _CompletedAudioService(database, repository, file_service)
    delivery.audio_service = completed_audio
    asyncio.run(delivery._execute_audio(new_audio[0]))
    assert repository.require(new_audio[0]["id"])["status"] == "completed"

    from app.services import composition_service as composition_module

    monkeypatch.setattr(composition_module, "file_service", file_service)
    monkeypatch.setattr(
        CompositionService,
        "_concat_videos",
        lambda self, _shots, _project, temp_path, **_kwargs: (
            temp_path.joinpath("concat.mp4").write_bytes(b"concat"),
            temp_path / "concat.mp4",
        )[1],
    )
    monkeypatch.setattr(
        CompositionService,
        "_add_audio",
        lambda self, video, *_args, **_kwargs: video,
    )
    monkeypatch.setattr(
        CompositionService,
        "_burn_subtitles",
        lambda self, video, *_args, **_kwargs: video,
    )
    monkeypatch.setattr(
        CompositionService,
        "_get_video_duration",
        staticmethod(lambda _path: 1.0),
    )
    monkeypatch.setattr(
        CompositionService,
        "_finalize_output",
        lambda self, video, output, _resolution: output.write_bytes(video.read_bytes()),
    )

    resumed_top_result = asyncio.run(delivery.execute_job(resumed_top))
    assert resumed_top_result["status"] == "queued", (
        resumed_top_result,
        resumed_top_result.get("preflight", {}).get("missing"),
    )
    new_subtitle = next(
        item
        for item in repository.list_by_workflow(run_id)
        if item["job_type"] == "generate_dynamic_subtitle"
        and item["id"] not in resumed_top["depends_on"]
    )
    assert new_subtitle["status"] == "completed"
    new_composition = next(
        item
        for item in repository.list_by_workflow(run_id)
        if item["job_type"] == "compose_chapter"
    )
    assert new_composition["status"] == "queued"
    asyncio.run(composition.handle_compose(repository.require(new_composition["id"])))
    assert repository.require(new_composition["id"])["status"] == "completed"

    zip_bytes, _filename, manifest = service.export(
        run_id,
        strict=True,
        exported_at="2026-08-23T00:00:00Z",
    )
    assert manifest["status"] == "success"
    resumed_audio_id = repository.require(new_audio[0]["id"])["result_data"]["audio_id"]
    assert all(
        item.get("source_id") != old_audio["id"]
        for item in manifest["entries"]
        if item.get("kind") == "audio"
    )
    assert any(
        item.get("source_id") == resumed_audio_id
        for item in manifest["entries"]
        if item.get("kind") == "audio"
    )
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        assert "metadata/manifest.json" in archive.namelist()


def test_composition_enqueue_failure_is_observable_and_fails_delivery_state(
    delivery_context,
) -> None:
    database, _service, file_service, run_id, project_id, chapter_id = delivery_context
    now = utc_now()
    stage6_id = f"{run_id}-stage-7"
    project_root = file_service.project_path(project_id)
    project_root.mkdir(parents=True, exist_ok=True)
    for relative_path in ("keyframes/queue-failure.png", "videos/queue-failure.mp4"):
        target = project_root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"media")
    with database.engine.begin() as connection:
        connection.execute(
            update(workflow_run)
            .where(workflow_run.c.id == run_id)
            .values(
                status="queued",
                config_snapshot_json=json.dumps(
                    {
                        "generate_audio": False,
                        "generate_subtitle": False,
                        "generate_bgm": False,
                        "generate_composition": True,
                        "allow_partial": True,
                    }
                ),
            )
        )
        connection.execute(
            update(workflow_stage)
            .where(workflow_stage.c.id == stage6_id)
            .values(status="queued", input_signature="run-signature")
        )
        connection.execute(
            insert(keyframe_version).values(
                id="keyframe-queue-failure",
                shot_id="shot-delivery-1",
                version_no=1,
                file_path="keyframes/queue-failure.png",
                status="completed",
                is_current=1,
                source="uploaded",
                input_signature="run-signature",
                created_at=now,
            )
        )
        connection.execute(
            insert(video_version).values(
                id="video-queue-failure",
                shot_id="shot-delivery-1",
                version_no=1,
                file_path="videos/queue-failure.mp4",
                actual_duration_sec=1.0,
                status="completed",
                is_current=1,
                source="uploaded",
                input_signature="run-signature",
                created_at=now,
            )
        )

    repository = JobRepository(database)
    runner = _DeliveryRunner(result=False)
    composition = CompositionService(
        database,
        JobService(database, repository=repository, runner=cast(Any, runner)),
    )
    top_job = repository.create_job(
        {
            "project_id": project_id,
            "chapter_id": chapter_id,
            "workflow_run_id": run_id,
            "workflow_stage_id": stage6_id,
            "job_type": "dynamic_stage_generate",
            "target_type": "workflow_stage",
            "target_id": stage6_id,
            "provider_kind": "text",
            "input_data": {"stage_key": "6-delivery"},
            "input_signature": "run-signature",
            "idempotency_key": "composition-enqueue-failure",
        }
    )
    delivery = DynamicDeliveryJobService(
        database,
        repository=repository,
        composition_service=composition,
        file_service=file_service,
        enqueue=runner.enqueue,
    )

    result = asyncio.run(delivery.execute_job(top_job))

    assert result["status"] in {"failed", "partial_failed"}
    assert "入队失败" in result["error"]
    composition_job = next(
        item
        for item in repository.list_by_workflow(run_id)
        if item["job_type"] == "compose_chapter"
    )
    assert composition_job["status"] == "queued"
    assert composition_job["input_data"]["workflow_stage_id"] == stage6_id
    assert composition_job["id"] in runner.enqueued
    with database.engine.connect() as connection:
        state = connection.execute(
            select(
                job.c.status,
                workflow_stage.c.status,
                workflow_run.c.status,
            )
            .select_from(
                job.join(workflow_stage, job.c.workflow_stage_id == workflow_stage.c.id).join(
                    workflow_run, job.c.workflow_run_id == workflow_run.c.id
                )
            )
            .where(job.c.id == top_job["id"])
        ).first()
    assert state is not None
    assert tuple(state) == ("failed", "failed", "failed")


@pytest.mark.parametrize("mutation", ["asset_revision", "reference_source"])
def test_queued_delivery_is_interrupted_when_completed_prompt_input_becomes_stale(
    delivery_context,
    mutation: str,
) -> None:
    database, _service, file_service, run_id, project_id, chapter_id = delivery_context
    now = utc_now()
    anchor_id = f"asset-delivery-anchor-{mutation}"
    reference_url = f"https://cdn.example.test/{mutation}-1.png"
    with database.engine.begin() as connection:
        connection.execute(
            insert(asset).values(
                id=anchor_id,
                project_id=project_id,
                asset_type="scene",
                name=f"交付场景-{mutation}",
                profile_json=json.dumps({"canonical_name": "交付场景"}),
                visual_anchor_json=json.dumps({}),
                status="confirmed",
                revision=1,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            update(shot)
            .where(shot.c.id == "shot-delivery-1")
            .values(
                external_key="shot-delivery-1",
                scene_asset_id=anchor_id,
                reference_bindings_json=json.dumps(
                    [
                        {
                            "index": 1,
                            "role": "scene",
                            "purpose": "layout_reference",
                            "asset_id": anchor_id,
                            "provider_url": reference_url,
                        }
                    ]
                ),
            )
        )
        stage4_content = {
            "shots": [
                {
                    "external_key": "shot-delivery-1",
                    "shot_no": 1,
                    "ordinal": 1,
                    "scene_asset_id": anchor_id,
                    "character_asset_ids": [],
                    "prop_asset_ids": [],
                    "reference_bindings": [
                        {
                            "index": 1,
                            "role": "scene",
                            "purpose": "layout_reference",
                            "asset_id": anchor_id,
                            "provider_url": reference_url,
                        }
                    ],
                }
            ]
        }
        connection.execute(
            update(workflow_stage)
            .where(
                workflow_stage.c.workflow_run_id == run_id,
                workflow_stage.c.stage_key == "4-storyboard",
            )
            .values(
                normalized_output_json=json.dumps(stage4_content),
                draft_output_json=json.dumps(stage4_content),
                status="confirmed",
            )
        )
        connection.execute(
            update(workflow_stage)
            .where(
                workflow_stage.c.workflow_run_id == run_id,
                workflow_stage.c.stage_key.in_(("5a-image-prompts", "5b-video-prompts")),
            )
            .values(
                status="confirmed",
                normalized_output_json=json.dumps(
                    {
                        "stage_key": "prompt",
                        "bundles": {"shot-delivery-1": {}},
                    }
                ),
                draft_output_json=json.dumps(
                    {
                        "stage_key": "prompt",
                        "bundles": {"shot-delivery-1": {}},
                    }
                ),
            )
        )

    workflow = DynamicWorkflowService(database, storage=file_service)
    with database.engine.begin() as connection:
        run_row = connection.execute(
            select(workflow_run).where(workflow_run.c.id == run_id)
        ).one()
        stage4 = workflow._stage_from_connection(connection, run_id, "4-storyboard")
        assert stage4 is not None
        prompt_stages: dict[str, dict[str, Any]] = {}
        for stage_key in ("5a-image-prompts", "5b-video-prompts"):
            signature, _ = workflow._prompt_stage_input_signature(
                connection,
                stage_key,
                dict(run_row._mapping),
                stage4,
            )
            connection.execute(
                update(workflow_stage)
                .where(
                    workflow_stage.c.workflow_run_id == run_id,
                    workflow_stage.c.stage_key == stage_key,
                )
                .values(
                    input_signature=signature,
                    output_refs_json=json.dumps(
                        [{"capture_mode": "compiled_prompt_stage"}]
                    ),
                )
            )
            prompt_stages[stage_key] = workflow._stage_from_connection(
                connection, run_id, stage_key
            ) or {}
        delivery_signature = workflow._delivery_stage_input_signature(
            dict(run_row._mapping),
            prompt_stages,
        )
        stage6_id = str(
            connection.scalar(
                select(workflow_stage.c.id).where(
                    workflow_stage.c.workflow_run_id == run_id,
                    workflow_stage.c.stage_key == "6-delivery",
                )
            )
        )
        connection.execute(
            update(workflow_stage)
            .where(workflow_stage.c.id == stage6_id)
            .values(status="queued", input_signature=delivery_signature)
        )

    repository = JobRepository(database)
    top_job = repository.create_job(
        {
            "project_id": project_id,
            "chapter_id": chapter_id,
            "workflow_run_id": run_id,
            "workflow_stage_id": stage6_id,
            "job_type": "dynamic_stage_generate",
            "target_type": "workflow_stage",
            "target_id": stage6_id,
            "provider_kind": "text",
            "input_data": {"stage_key": "6-delivery"},
            "input_signature": delivery_signature,
            "idempotency_key": f"delivery-stale-{mutation}",
        }
    )

    with database.engine.begin() as connection:
        if mutation == "asset_revision":
            connection.execute(
                update(asset).where(asset.c.id == anchor_id).values(revision=2)
            )
        else:
            connection.execute(
                update(shot)
                .where(shot.c.id == "shot-delivery-1")
                .values(
                    reference_bindings_json=json.dumps(
                        [
                            {
                                "index": 1,
                                "role": "scene",
                                "purpose": "layout_reference",
                                "asset_id": anchor_id,
                                "provider_url": f"{reference_url}-changed",
                            }
                        ]
                    )
                )
            )

    class NoMediaCall:
        async def handle_job(self, _record: Mapping[str, Any]) -> dict[str, Any]:
            raise AssertionError("stale delivery 不应开始媒体子任务")

    delivery = DynamicDeliveryJobService(
        database,
        repository=repository,
        audio_service=NoMediaCall(),
        composition_service=NoMediaCall(),
        file_service=file_service,
    )
    result = asyncio.run(delivery.execute_job(top_job))

    assert result["status"] == "interrupted", result
    assert repository.require(top_job["id"])["status"] == "interrupted"
    with database.engine.connect() as connection:
        statuses = connection.execute(
            select(workflow_stage.c.stage_key, workflow_stage.c.status).where(
                workflow_stage.c.workflow_run_id == run_id,
                workflow_stage.c.stage_key.in_(
                    ("5a-image-prompts", "5b-video-prompts", "6-delivery")
                ),
            )
        ).all()
        assert {tuple(item) for item in statuses} == {
            ("5a-image-prompts", "stale"),
            ("5b-video-prompts", "stale"),
            ("6-delivery", "stale"),
        }, result
        assert connection.scalar(select(keyframe_version.c.id)) is None
        assert connection.scalar(select(video_version.c.id)) is None


def test_old_delivery_attempt_cannot_overwrite_new_attempt(delivery_context, monkeypatch) -> None:
    database, _service, file_service, run_id, project_id, chapter_id = delivery_context
    old_stage_id = f"{run_id}-stage-7"
    new_stage_id = f"{run_id}-stage-7-attempt-2"
    now = utc_now()
    with database.engine.begin() as connection:
        connection.execute(
            insert(workflow_stage).values(
                id=new_stage_id,
                workflow_run_id=run_id,
                stage_key="6-delivery",
                ordinal=7,
                status="queued",
                input_signature="new-delivery-signature",
                raw_output_json="{}",
                normalized_output_json="{}",
                draft_output_json="{}",
                output_refs_json="[]",
                gate_result_json="{}",
                attempt=2,
                error="",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            update(workflow_stage)
            .where(workflow_stage.c.id == old_stage_id)
            .values(status="running", input_signature="old-delivery-signature")
        )

    repository = JobRepository(database)
    old_job = repository.create_job(
        {
            "project_id": project_id,
            "chapter_id": chapter_id,
            "workflow_run_id": run_id,
            "workflow_stage_id": old_stage_id,
            "job_type": "dynamic_stage_generate",
            "target_type": "workflow_stage",
            "target_id": old_stage_id,
            "provider_kind": "text",
            "input_data": {"stage_key": "6-delivery"},
            "input_signature": "old-delivery-signature",
            "idempotency_key": "old-delivery-attempt",
        }
    )
    delivery = DynamicDeliveryJobService(
        database,
        repository=repository,
        file_service=file_service,
    )

    async def fake_execute(_current_job, _run, _stage):
        return {"status": "completed", "subtasks": []}

    monkeypatch.setattr(delivery, "_execute_delivery", fake_execute)
    result = asyncio.run(delivery.execute_job(old_job))

    assert result["status"] == "interrupted"
    assert repository.require(old_job["id"])["status"] == "interrupted"
    with database.engine.connect() as connection:
        assert connection.scalar(
            select(workflow_stage.c.status).where(workflow_stage.c.id == new_stage_id)
        ) == "queued"


def test_strict_delivery_zip_contains_current_binary_media_and_manifest_digests(
    delivery_context,
) -> None:
    database, service, file_service, run_id, project_id, chapter_id = delivery_context
    now = utc_now()
    stage_ids = {
        stage_key: f"{run_id}-stage-{index}"
        for index, stage_key in enumerate(STAGE_KEYS)
    }
    media_payloads = {
        "keyframes/current.png": _PNG,
        "videos/current.mp4": b"\x00\x00ftypmp42-current-video",
        "audio/current.mp3": b"ID3-current-audio",
        "subtitle/current.srt": b"1\n00:00:00,000 --> 00:00:01,000\nhello\n",
        "composition/final.mp4": b"\x00\x00ftypmp42-composition",
    }
    project_root = file_service.project_path(project_id)
    project_root.mkdir(parents=True, exist_ok=True)
    for relative_path, data in media_payloads.items():
        target = project_root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def provenance(stage_key: str, input_signature: str) -> dict[str, Any]:
        return {
            "workflow_run_id": run_id,
            "workflow_stage_id": stage_ids[stage_key],
            "workflow_stage_attempt": 1,
            "workflow_run_revision": 1,
            "input_signature": input_signature,
            "upstream_input_signature": f"{stage_key}-signature",
        }

    with database.engine.begin() as connection:
        connection.execute(
            insert(keyframe_version).values(
                id="keyframe-current",
                shot_id="shot-delivery-1",
                version_no=1,
                file_path="keyframes/current.png",
                status="completed",
                is_current=1,
                source="generated",
                input_signature="keyframe-own-signature",
                provenance_json=json.dumps(
                    provenance("5a-image-prompts", "keyframe-own-signature")
                ),
                created_at=now,
            )
        )
        connection.execute(
            insert(video_version).values(
                id="video-current",
                shot_id="shot-delivery-1",
                version_no=1,
                file_path="videos/current.mp4",
                actual_duration_sec=1.0,
                status="completed",
                is_current=1,
                source="generated",
                input_signature="video-own-signature",
                provenance_json=json.dumps(
                    provenance("5b-video-prompts", "video-own-signature")
                ),
                created_at=now,
            )
        )
        connection.execute(
            insert(shot_audio).values(
                id="audio-current",
                shot_id="shot-delivery-1",
                dialogue_line_id="line-1",
                line_order=1,
                dialogue_text="hello",
                source_text_snapshot="hello",
                speaker_name="主角",
                voice_id="voice-1",
                file_path="audio/current.mp3",
                duration_sec=1.0,
                version_no=1,
                status="completed",
                is_current=1,
                source="generated",
                input_signature="audio-own-signature",
                provenance_json=json.dumps(provenance("6-delivery", "audio-own-signature")),
                error="",
                created_at=now,
            )
        )
        connection.execute(
            insert(shot_subtitle).values(
                id="subtitle-current",
                shot_id="shot-delivery-1",
                dialogue_line_id="line-1",
                audio_id="audio-current",
                text="hello",
                start_time=0.0,
                end_time=1.0,
                provenance_json=json.dumps(provenance("6-delivery", "subtitle-own-signature")),
                created_at=now,
            )
        )
        connection.execute(
            insert(chapter_composition).values(
                id="composition-current",
                chapter_id=chapter_id,
                workflow_run_id=run_id,
                input_signature="6-delivery-signature",
                file_path="composition/final.mp4",
                duration_sec=5.0,
                status="completed",
                error="",
                provenance_json=json.dumps(provenance("6-delivery", "6-delivery-signature")),
                created_at=now,
                updated_at=now,
            )
        )

    zip_bytes, _filename, manifest = service.export(
        run_id,
        strict=True,
        exported_at="2026-08-23T00:00:00Z",
    )
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        media_entries = {
            item["source_id"]: item
            for item in manifest["entries"]
            if item.get("kind") in {"keyframe", "video", "audio", "subtitle", "composition"}
        }
        assert set(media_entries) == {
            "keyframe-current",
            "video-current",
            "audio-current",
            f"subtitle:{run_id}",
            "composition-current",
        }
        for source_id, entry in media_entries.items():
            data = archive.read(entry["arcname"])
            expected = {
                "keyframe-current": media_payloads["keyframes/current.png"],
                "video-current": media_payloads["videos/current.mp4"],
                "audio-current": media_payloads["audio/current.mp3"],
                f"subtitle:{run_id}": (
                    b"1\n00:00:00,000 --> 00:00:01,000\nhello\n"
                ),
                "composition-current": media_payloads["composition/final.mp4"],
            }[source_id]
            assert data == expected
            assert entry["sha256"] == hashlib.sha256(data).hexdigest()
            assert entry["size"] == len(data)
            assert entry["source_signature"]
            assert entry["attempt"] == 1
            assert entry["revision"] == 1
        assert any(name.endswith(".png") for name in archive.namelist())
        assert sum(name.endswith(".mp4") for name in archive.namelist()) == 2
        assert any(name.endswith(".mp3") for name in archive.namelist())
        assert any(name.endswith(".srt") for name in archive.namelist())


def test_binary_delivery_rejects_old_attempt_and_path_traversal(delivery_context) -> None:
    _database, service, file_service, run_id, project_id, _chapter_id = delivery_context
    root = file_service.project_path(project_id)
    root.mkdir(parents=True, exist_ok=True)
    (root / "videos").mkdir(parents=True, exist_ok=True)
    (root / "videos" / "old.mp4").write_bytes(b"old")
    snapshot = service.build_snapshot(run_id)
    preflight = CompositionPreflightResult(
        workflow_run_id=run_id,
        project_id=project_id,
        chapter_id=snapshot["chapter_id"],
        strict=True,
        allow_partial=False,
        status="ready",
    )

    old_attempt = copy.deepcopy(snapshot)
    old_attempt["files"] = [
        {
            "kind": "video",
            "id": "old-video",
            "path": "videos/old.mp4",
            "status": "completed",
            "workflow_run_id": run_id,
            "workflow_stage_id": "old-video-stage-attempt",
            "input_signature": "old-video-signature",
            "upstream_input_signature": "5b-video-prompts-signature",
            "provenance": {
                "workflow_run_id": run_id,
                "workflow_stage_id": "old-video-stage-attempt",
                "workflow_stage_attempt": 1,
                "workflow_run_revision": 1,
                "upstream_input_signature": "5b-video-prompts-signature",
            },
        }
    ]
    with pytest.raises(DeliveryServiceError, match="stage attempt"):
        service._binary_media_entries(old_attempt, preflight)

    traversal = copy.deepcopy(snapshot)
    traversal["files"] = [
        {
            "kind": "video",
            "id": "traversal-video",
            "path": "../outside.mp4",
            "status": "completed",
            "workflow_run_id": run_id,
            "workflow_stage_id": f"{run_id}-stage-6",
            "input_signature": "video-own-signature",
            "upstream_input_signature": "5b-video-prompts-signature",
            "provenance": {
                "workflow_run_id": run_id,
                "workflow_stage_id": f"{run_id}-stage-6",
                "workflow_stage_attempt": 1,
                "workflow_run_revision": 1,
                "upstream_input_signature": "5b-video-prompts-signature",
            },
        }
    ]
    with pytest.raises(DeliveryServiceError, match="路径"):
        service._binary_media_entries(traversal, preflight)


def test_dynamic_composition_failure_updates_job_stage_and_run_atomically(delivery_context) -> None:
    database, _service, _file_service, run_id, _project_id, chapter_id = delivery_context
    repository = JobRepository(database)
    composition = CompositionService(
        database,
        JobService(database, repository=repository, runner=cast(Any, _DeliveryRunner())),
    )
    record = composition.compose_chapter(
        chapter_id,
        {"include_audio": False, "burn_subtitles": False},
        enqueue=False,
        workflow_context={
            "workflow_run_id": run_id,
            "workflow_stage_id": f"{run_id}-stage-7",
            "workflow_stage_attempt": 1,
            "workflow_run_revision": 1,
            "input_signature": "6-delivery-signature",
            "idempotency_key": "composition-failure-test",
        },
    )
    job_id = str(record["job_id"])

    with pytest.raises(RuntimeError, match="没有已生成的分镜视频"):
        asyncio.run(composition.handle_compose(repository.require(job_id)))

    with database.engine.connect() as connection:
        state = connection.execute(
            select(
                job.c.status,
                workflow_stage.c.status,
                workflow_run.c.status,
                chapter_composition.c.status,
            )
            .select_from(
                job.join(workflow_stage, job.c.workflow_stage_id == workflow_stage.c.id)
                .join(workflow_run, job.c.workflow_run_id == workflow_run.c.id)
                .join(chapter_composition, chapter_composition.c.job_id == job.c.id)
            )
            .where(job.c.id == job_id)
        ).first()
    assert state is not None
    assert tuple(state) == ("failed", "partial_failed", "partial_failed", "failed")


def test_stale_composition_does_not_block_new_delivery_attempt(delivery_context) -> None:
    database, _service, _file_service, run_id, project_id, chapter_id = delivery_context
    now = utc_now()
    old_stage_id = f"{run_id}-stage-7"
    new_stage_id = f"{run_id}-stage-7-attempt-2"
    with database.engine.begin() as connection:
        connection.execute(
            insert(workflow_stage).values(
                id=new_stage_id,
                workflow_run_id=run_id,
                stage_key="6-delivery",
                ordinal=7,
                status="queued",
                input_signature="new-delivery-signature",
                raw_output_json="{}",
                normalized_output_json="{}",
                draft_output_json="{}",
                output_refs_json="[]",
                gate_result_json="{}",
                attempt=2,
                error="",
                created_at=now,
                updated_at=now,
            )
        )
    repository = JobRepository(database)
    old_job = repository.create_job(
        {
            "project_id": project_id,
            "chapter_id": chapter_id,
            "workflow_run_id": run_id,
            "workflow_stage_id": old_stage_id,
            "job_type": "compose_chapter",
            "target_type": "chapter",
            "target_id": chapter_id,
            "provider_kind": "text",
            "input_data": {},
            "input_signature": "old-delivery-signature",
            "idempotency_key": "old-pending-composition",
        }
    )
    with database.engine.begin() as connection:
        connection.execute(
            insert(chapter_composition).values(
                id="old-pending-composition-record",
                chapter_id=chapter_id,
                job_id=old_job["id"],
                workflow_run_id=run_id,
                input_signature="old-delivery-signature",
                status="pending",
                error="",
                created_at=now,
                updated_at=now,
            )
        )

    composition = CompositionService(
        database,
        JobService(database, repository=repository, runner=cast(Any, _DeliveryRunner())),
    )
    new_record = composition.compose_chapter(
        chapter_id,
        {"include_audio": False, "burn_subtitles": False},
        enqueue=False,
        workflow_context={
            "workflow_run_id": run_id,
            "workflow_stage_id": new_stage_id,
            "workflow_stage_attempt": 2,
            "workflow_run_revision": 2,
            "input_signature": "new-delivery-signature",
            "idempotency_key": "new-pending-composition",
        },
    )

    assert new_record["id"] != "old-pending-composition-record"
    with database.engine.connect() as connection:
        states = connection.execute(
            select(chapter_composition.c.id, chapter_composition.c.status).where(
                chapter_composition.c.chapter_id == chapter_id
            )
        ).all()
    assert dict(states)["old-pending-composition-record"] == "failed"
    assert dict(states)[new_record["id"]] == "pending"


def test_same_stage_resume_replaces_old_composition_generation(delivery_context) -> None:
    database, _service, _file_service, run_id, project_id, chapter_id = delivery_context
    now = utc_now()
    stage_id = f"{run_id}-stage-7"
    signature = "6-delivery-signature"
    repository = JobRepository(database)
    old_job = repository.create_job(
        {
            "project_id": project_id,
            "chapter_id": chapter_id,
            "workflow_run_id": run_id,
            "workflow_stage_id": stage_id,
            "job_type": "compose_chapter",
            "target_type": "chapter",
            "target_id": chapter_id,
            "provider_kind": "text",
            "input_data": {
                "workflow_run_id": run_id,
                "workflow_stage_id": stage_id,
                "delivery_generation": "old-generation",
            },
            "input_signature": signature,
            "idempotency_key": "same-stage-old-composition",
        }
    )
    with database.engine.begin() as connection:
        connection.execute(
            insert(chapter_composition).values(
                id="same-stage-old-composition-record",
                chapter_id=chapter_id,
                job_id=old_job["id"],
                workflow_run_id=run_id,
                input_signature=signature,
                provenance_json=json.dumps({"delivery_generation": "old-generation"}),
                status="pending",
                error="",
                created_at=now,
                updated_at=now,
            )
        )

    composition = CompositionService(
        database,
        JobService(database, repository=repository, runner=cast(Any, _DeliveryRunner())),
    )
    new_record = composition.compose_chapter(
        chapter_id,
        {"include_audio": False, "burn_subtitles": False},
        enqueue=False,
        workflow_context={
            "workflow_run_id": run_id,
            "workflow_stage_id": stage_id,
            "workflow_stage_attempt": 1,
            "workflow_run_revision": 1,
            "input_signature": signature,
            "delivery_generation": "new-generation",
            "idempotency_key": "same-stage-new-composition",
            "provenance": {"delivery_generation": "new-generation"},
        },
    )

    with database.engine.connect() as connection:
        old_state = connection.execute(
            select(
                chapter_composition.c.status,
                job.c.status,
            )
            .select_from(chapter_composition.join(job, chapter_composition.c.job_id == job.c.id))
            .where(chapter_composition.c.id == "same-stage-old-composition-record")
        ).one()

    assert new_record["id"] != "same-stage-old-composition-record"
    assert tuple(old_state) == ("failed", "interrupted")


def test_dynamic_composition_rejects_media_snapshot_change(
    delivery_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, _service, file_service, run_id, project_id, chapter_id = delivery_context
    now = utc_now()
    stage_id = f"{run_id}-stage-7"
    video_path = "videos/snapshot.mp4"
    project_root = file_service.project_path(project_id)
    project_root.mkdir(parents=True, exist_ok=True)
    (project_root / "videos").mkdir(parents=True, exist_ok=True)
    (project_root / video_path).write_bytes(b"snapshot-video")
    with database.engine.begin() as connection:
        connection.execute(
            update(workflow_run)
            .where(workflow_run.c.id == run_id)
            .values(status="running")
        )
        connection.execute(
            update(workflow_stage)
            .where(workflow_stage.c.id == stage_id)
            .values(status="running", input_signature="6-delivery-signature")
        )
        connection.execute(
            insert(video_version).values(
                id="video-snapshot-v1",
                shot_id="shot-delivery-1",
                version_no=1,
                file_path=video_path,
                actual_duration_sec=1.0,
                status="completed",
                is_current=1,
                source="generated",
                input_signature="video-snapshot-signature",
                provenance_json=json.dumps(
                    {
                        "workflow_run_id": run_id,
                        "workflow_stage_id": "video-stage-1",
                        "workflow_stage_attempt": 1,
                        "workflow_run_revision": 1,
                        "input_signature": "video-snapshot-signature",
                        "upstream_input_signature": "5b-video-signature",
                    }
                ),
                created_at=now,
            )
        )
        connection.execute(
            insert(video_version).values(
                id="video-snapshot-v2",
                shot_id="shot-delivery-1",
                version_no=2,
                file_path=video_path,
                actual_duration_sec=1.0,
                status="completed",
                is_current=0,
                source="generated",
                input_signature="video-snapshot-new-signature",
                provenance_json=json.dumps(
                    {
                        "workflow_run_id": run_id,
                        "workflow_stage_id": "video-stage-1",
                        "workflow_stage_attempt": 1,
                        "workflow_run_revision": 2,
                        "input_signature": "video-snapshot-new-signature",
                        "upstream_input_signature": "5b-video-signature",
                    }
                ),
                created_at=now,
            )
        )
        connection.execute(
            update(video_version)
            .where(video_version.c.id == "video-snapshot-v1")
            .values(is_current=0)
        )
        connection.execute(
            update(video_version)
            .where(video_version.c.id == "video-snapshot-v2")
            .values(is_current=1)
        )

    from app.services import composition_service as composition_module

    monkeypatch.setattr(composition_module, "file_service", file_service)
    repository = JobRepository(database)
    composition = CompositionService(
        database,
        JobService(database, repository=repository, runner=cast(Any, _DeliveryRunner())),
    )
    record = composition.compose_chapter(
        chapter_id,
        {"include_audio": False, "burn_subtitles": False},
        enqueue=False,
        workflow_context={
            "workflow_run_id": run_id,
            "workflow_stage_id": stage_id,
            "workflow_stage_attempt": 1,
            "workflow_run_revision": 1,
            "input_signature": "6-delivery-signature",
            "media_snapshot": {
                "version": "dynamic-composition-media-snapshot-v1",
                "workflow_run_id": run_id,
                "workflow_stage_id": stage_id,
                "workflow_stage_attempt": 1,
                "workflow_run_revision": 1,
                "input_signature": "6-delivery-signature",
                "shots": [
                    {
                        "shot_id": "shot-delivery-1",
                        "video_version_id": "video-snapshot-v1",
                        "audio_version_id": None,
                        "video": {
                            "version_id": "video-snapshot-v1",
                            "file_path": video_path,
                            "input_signature": "video-snapshot-signature",
                            "attempt": 1,
                            "revision": 1,
                            "workflow_run_id": run_id,
                            "workflow_stage_id": "video-stage-1",
                        },
                        "audio": None,
                    }
                ],
            },
            "idempotency_key": "snapshot-change-composition",
        },
    )

    with pytest.raises(RuntimeError, match="current version"):
        asyncio.run(composition.handle_compose(repository.require(record["job_id"])))

    with database.engine.connect() as connection:
        state = connection.execute(
            select(
                job.c.status,
                workflow_stage.c.status,
                workflow_run.c.status,
                chapter_composition.c.status,
            )
            .select_from(
                job.join(workflow_stage, job.c.workflow_stage_id == workflow_stage.c.id)
                .join(workflow_run, job.c.workflow_run_id == workflow_run.c.id)
                .join(chapter_composition, chapter_composition.c.job_id == job.c.id)
            )
            .where(job.c.id == record["job_id"])
        ).first()
    assert state is not None
    assert tuple(state) == ("failed", "partial_failed", "partial_failed", "failed")


def test_delivery_state_race_does_not_complete_job_after_stage_becomes_stale(
    delivery_context,
) -> None:
    database, _service, _file_service, run_id, project_id, chapter_id = delivery_context
    stage_id = f"{run_id}-stage-7"
    repository = JobRepository(database)
    delivery = DynamicDeliveryJobService(database, repository=repository)
    current_job = repository.create_job(
        {
            "project_id": project_id,
            "chapter_id": chapter_id,
            "workflow_run_id": run_id,
            "workflow_stage_id": stage_id,
            "job_type": "dynamic_stage_generate",
            "target_type": "workflow_stage",
            "target_id": stage_id,
            "provider_kind": "text",
            "input_data": {"stage_key": "6-delivery"},
            "input_signature": "6-delivery-signature",
            "idempotency_key": "delivery-race-test",
        }
    )
    with database.engine.begin() as connection:
        connection.execute(
            update(workflow_stage).where(workflow_stage.c.id == stage_id).values(status="stale")
        )

    with pytest.raises(DeliveryExecutionError, match="当前 attempt"):
        delivery._save_delivery_state(
            job_id=current_job["id"],
            run_id=run_id,
            stage_id=stage_id,
            job_status="completed",
            status="completed",
            result={"status": "completed"},
            expected_run_revision=1,
            expected_input_signature="6-delivery-signature",
        )

    with database.engine.connect() as connection:
        state = connection.execute(
            select(job.c.status, workflow_stage.c.status, workflow_run.c.revision)
            .select_from(
                job.join(workflow_stage, job.c.workflow_stage_id == workflow_stage.c.id).join(
                    workflow_run, job.c.workflow_run_id == workflow_run.c.id
                )
            )
            .where(job.c.id == current_job["id"])
        ).first()
    assert state is not None
    assert tuple(state) == ("queued", "stale", 1)
