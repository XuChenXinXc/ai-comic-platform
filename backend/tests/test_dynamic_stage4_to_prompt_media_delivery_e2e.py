from __future__ import annotations

import asyncio
import base64
import io
import json
import threading
import zipfile
from types import SimpleNamespace

import pytest
from media_test_helpers import _PNG
from sqlalchemy import insert, update
from test_dynamic_workflow_stage5_barrier import (
    _confirm_stage,
    _prepare_stage4,
    _prompt_jobs,
    stage5_context,  # noqa: F401
)

from app.dynamic_pipeline.audio_job_service import DynamicAudioJobService
from app.dynamic_pipeline.delivery import DynamicDeliveryJobService, DynamicDeliveryService
from app.dynamic_pipeline.media_queue_service import DynamicMediaQueueService
from app.dynamic_pipeline.prompt_stage_service import DynamicPromptStageService
from app.dynamic_pipeline.schemas import StageConfirmRequest
from app.jobs.dynamic_bridge import DynamicJobBridge
from app.jobs.repository import JobRepository
from app.jobs.runner import JobRunner
from app.models.tables import api_provider, job, workflow_run, workflow_stage
from app.schemas import ProviderSubmission
from app.services.composition_service import CompositionService
from app.services.dynamic_workflow_service import DynamicWorkflowService
from app.services.job_service import JobService
from app.services.media_service import MediaService
from app.services.provider_service import ProviderService
from app.services.task_handlers import register_handlers
from app.services.utils import json_dumps, json_loads, utc_now


@pytest.fixture
def gate3_stage5_context(request):
    return request.getfixturevalue("stage5_context")


def test_dynamic_stage4_to_prompt_media_delivery_e2e(gate3_stage5_context, monkeypatch) -> None:
    context = gate3_stage5_context
    setup = _prepare_stage4(context)
    with context.database.engine.begin() as connection:
        storyboard_without_external_references = {
            **setup.storyboard,
            "shots": [
                {**shot, "reference_bindings": []} for shot in setup.storyboard["shots"]
            ],
        }
        connection.execute(
            update(workflow_run)
            .where(workflow_run.c.id == setup.run_id)
            .values(
                config_snapshot_json=json_dumps(
                    {
                        "image_provider_id": "mock-image",
                        "image_provider_model": "mock-image",
                        "video_provider_id": "mock-video",
                        "video_provider_model": "mock-video",
                        "generate_audio": False,
                        "generate_subtitle": False,
                        "generate_bgm": False,
                        "generate_composition": False,
                    }
                )
            )
        )
        connection.execute(
            update(workflow_stage)
            .where(
                workflow_stage.c.workflow_run_id == setup.run_id,
                workflow_stage.c.stage_key == "4-storyboard",
            )
            .values(
                normalized_output_json=json_dumps(storyboard_without_external_references),
                draft_output_json=json_dumps(storyboard_without_external_references),
            )
        )

    confirmed = _confirm_stage(
        context,
        setup.run_id,
        "4-storyboard",
        setup.stage4_saved["run"]["revision"],
    )
    prompt_jobs = _prompt_jobs(context, setup.run_id)
    assert {item["workflow_stage_key"] for item in prompt_jobs} == {
        "5a-image-prompts",
        "5b-video-prompts",
    }
    context.enqueued.clear()
    context.enqueue_seen.clear()
    for prompt_job in prompt_jobs:
        stage = context.service.get_stage(setup.run_id, prompt_job["workflow_stage_key"])
        assert prompt_job["input_signature"] == stage["input_signature"]

    prompt_service = DynamicPromptStageService(context.database)
    repository = JobRepository(context.database)
    from app.dynamic_pipeline import materializer

    payloads = []
    original_signature = materializer.calculate_prompt_stage_input_signature

    def capture_signature(payload):
        payloads.append(payload)
        return original_signature(payload)

    monkeypatch.setattr(materializer, "calculate_prompt_stage_input_signature", capture_signature)
    for prompt_job in prompt_jobs:
        with context.database.engine.connect() as connection:
            run_record = prompt_service._run(connection, setup.run_id)
            stage_record = prompt_service._stage_by_id(
                connection,
                prompt_job["workflow_stage_id"],
                setup.run_id,
            )
        assert run_record is not None
        assert stage_record is not None
        with context.database.engine.connect() as connection:
            workflow_signature, _ = context.service._prompt_stage_input_signature(
                connection,
                prompt_job["workflow_stage_key"],
                run_record,
                context.service._stage_from_connection(
                    connection,
                    setup.run_id,
                    "4-storyboard",
                ),
            )
            actual_signature = prompt_service._current_prompt_input_signature(
                connection,
                run=run_record,
                stage=stage_record,
                job_record=repository.require(prompt_job["id"]),
            )
        assert payloads[-2]["reference_plan_hashes"] == payloads[-1]["reference_plan_hashes"]
        assert payloads[-2]["shots"] == payloads[-1]["shots"]
        assert payloads[-2] == payloads[-1]
        assert workflow_signature == actual_signature
        assert actual_signature == stage_record["input_signature"]
        result = asyncio.run(prompt_service.execute_job(repository.require(prompt_job["id"])))
        assert result.get("status") in {"ready", "warning"}, result

    image_confirmed = _confirm_stage(
        context,
        setup.run_id,
        "5a-image-prompts",
        context.service.get_run(setup.run_id)["revision"],
    )
    video_confirmed = _confirm_stage(
        context,
        setup.run_id,
        "5b-video-prompts",
        image_confirmed["run"]["revision"],
    )

    with context.database.engine.connect() as connection:
        media_jobs = [
            dict(row._mapping)
            for row in connection.execute(
                job.select().where(
                    job.c.workflow_run_id == setup.run_id,
                    job.c.job_type.in_(("generate_keyframe", "generate_video")),
                )
            ).all()
        ]
        delivery_stage = connection.execute(
            workflow_stage.select().where(
                workflow_stage.c.workflow_run_id == setup.run_id,
                workflow_stage.c.stage_key == "6-delivery",
            )
        ).one()
        delivery_job = connection.execute(
            job.select().where(
                job.c.workflow_run_id == setup.run_id,
                job.c.workflow_stage_id == delivery_stage.id,
            )
        ).one()

    assert confirmed["run"]["current_stage"] == "5a-image-prompts"
    assert video_confirmed["run"]["current_stage"] == "6-delivery"
    assert delivery_job.status == "queued"
    assert delivery_stage.status == "queued"
    dependencies = json_loads(delivery_job.depends_on_json, [])
    assert set(dependencies) == {str(item["id"]) for item in media_jobs}
    assert delivery_job.id not in context.enqueued

    asyncio.run(
        _run_delayed_media_and_delivery(
            context,
            repository,
            media_jobs,
            delivery_stage,
            delivery_job,
            monkeypatch,
        )
    )


async def _run_delayed_media_and_delivery(
    context,
    repository: JobRepository,
    media_jobs: list[dict[str, object]],
    delivery_stage,
    delivery_job,
    monkeypatch,
) -> None:
    from app.services import media_service as media_module

    class DelayedImageProvider:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def submit(self, **_kwargs):
            self.started.set()
            await self.release.wait()
            return ProviderSubmission(
                status="completed",
                result={"content_base64": base64.b64encode(_PNG).decode("ascii")},
            )

    class RecordingVideoProvider:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def prepare_input_image(self, _path):
            return {"value": "keyframe"}

        async def submit(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(status="completed", task_id="", result={})

    image_provider = DelayedImageProvider()
    video_provider = RecordingVideoProvider()
    runner_calls: list[str] = []

    class RecordingRunner:
        def enqueue(self, job_id: str) -> bool:
            runner_calls.append(job_id)
            return True

    runner = RecordingRunner()
    jobs = JobService(
        context.database,
        repository=repository,
        runner=runner,  # type: ignore[arg-type]
    )
    media = MediaService(context.database, jobs)
    monkeypatch.setattr(media_module, "file_service", context.service.storage)
    monkeypatch.setattr(
        media.providers,
        "resolve",
        lambda *, kind, **_kwargs: (
            image_provider if kind == "image" else video_provider,
            {
                "id": "mock-image" if kind == "image" else "mock-video",
                "model": "mock-image" if kind == "image" else "mock-video",
                "params": {},
                "timeout_sec": 60,
            },
        ),
    )

    async def download_result(_provider, _result, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_PNG if target.suffix == ".png" else b"video-data")

    monkeypatch.setattr(media, "_download_provider_result", download_result)
    monkeypatch.setattr(media, "_verify_video", lambda _path: 6.0)
    monkeypatch.setattr(
        media,
        "_extract_last_frame",
        lambda _source, target: target.write_bytes(b"end-frame"),
    )

    media_by_type = {str(item["job_type"]): item for item in media_jobs}
    image_job = repository.require(str(media_by_type["generate_keyframe"]["id"]))
    video_job = repository.require(str(media_by_type["generate_video"]["id"]))
    keyframe_reads: list[str] = []
    video_started = False
    original_get_version = media._get_version

    def record_keyframe_read(kind, version_id):
        if kind == "keyframe" and video_started:
            keyframe_reads.append(str(version_id))
        return original_get_version(kind, version_id)

    monkeypatch.setattr(media, "_get_version", record_keyframe_read)
    wait_started = asyncio.Event()
    original_wait = media._wait_for_job_dependencies

    async def wait_for_dependencies(record):
        if str(record.get("id")) == str(video_job["id"]):
            wait_started.set()
        await original_wait(record)

    monkeypatch.setattr(media, "_wait_for_job_dependencies", wait_for_dependencies)
    image_task = asyncio.create_task(media.handle_keyframe(image_job))
    await image_provider.started.wait()
    video_started = True
    video_task = asyncio.create_task(media.handle_video(video_job))
    await wait_started.wait()
    await asyncio.sleep(0)
    assert video_provider.calls == []
    assert keyframe_reads == []

    image_provider.release.set()
    await image_task
    await video_task
    assert video_provider.calls
    assert repository.require(image_job["id"])["status"] == "completed"
    assert repository.require(video_job["id"])["status"] == "completed"
    assert str(delivery_job.id) in runner_calls

    delivery = DynamicDeliveryJobService(
        context.database,
        repository=repository,
        file_service=context.service.storage,
    )
    assert delivery._is_current_delivery_job(str(delivery_job.id), str(delivery_stage.id))
    started_delivery: list[str] = []
    original_start = delivery._start_delivery

    def record_delivery_start(run_id, stage_id, input_signature):
        started_delivery.append(str(stage_id))
        return original_start(run_id, stage_id, input_signature)

    monkeypatch.setattr(delivery, "_start_delivery", record_delivery_start)
    final_result = await delivery.execute_job(repository.require(str(delivery_job.id)))
    assert final_result["status"] == "completed", final_result
    assert started_delivery == [str(delivery_stage.id)]


def test_dynamic_stage4_to_delivery_real_runner_emits_binary_zip(
    gate3_stage5_context,
    monkeypatch,
) -> None:
    """真实 Runner 驱动全链路；Provider 与 FFmpeg 均为进程内 mock。"""
    context = gate3_stage5_context
    setup = _prepare_stage4(context)
    now = utc_now()
    with context.database.engine.begin() as connection:
        connection.execute(
            insert(api_provider).values(
                id="mock-tts-gate3",
                kind="tts",
                name="Gate 3 Mock TTS",
                adapter="mock",
                model="mock-tts-gate3",
                params_json="{}",
                timeout_sec=60,
                max_retries=0,
                enabled=1,
                is_default=0,
                created_at=now,
                updated_at=now,
            )
        )
        storyboard = {
            **setup.storyboard,
            "shots": [
                {**shot, "reference_bindings": []} for shot in setup.storyboard["shots"]
            ],
        }
        connection.execute(
            update(workflow_run)
            .where(workflow_run.c.id == setup.run_id)
            .values(
                config_snapshot_json=json_dumps(
                    {
                        "image_provider_id": "mock-image",
                        "image_provider_model": "mock-image",
                        "video_provider_id": "mock-video",
                        "video_provider_model": "mock-video",
                        "audio_provider_id": "mock-tts-gate3",
                        "audio_provider_model": "mock-tts-gate3",
                        "generate_audio": True,
                        "generate_subtitle": True,
                        "generate_composition": True,
                        "generate_bgm": False,
                    }
                )
            )
        )
        connection.execute(
            update(workflow_stage)
            .where(
                workflow_stage.c.workflow_run_id == setup.run_id,
                workflow_stage.c.stage_key == "4-storyboard",
            )
            .values(
                normalized_output_json=json_dumps(storyboard),
                draft_output_json=json_dumps(storyboard),
            )
        )

    repository = JobRepository(context.database)
    runner = JobRunner(repository, {"text": 1, "image": 1, "video": 1})
    real_workflow = DynamicWorkflowService(
        context.database,
        storage=context.service.storage,
        job_bridge=DynamicJobBridge(repository, enqueue=runner.enqueue),
        media_queue_service=DynamicMediaQueueService(
            context.database,
            job_repository=repository,
            enqueue=runner.enqueue,
        ),
    )

    from app.providers.mock import MockVideoProvider

    video_started = threading.Event()
    release_video = threading.Event()

    async def delayed_video_submit(self, **_kwargs):
        video_started.set()
        await asyncio.to_thread(release_video.wait)
        return ProviderSubmission(
            status="completed",
            result={"content_base64": base64.b64encode(b"mock-mp4").decode("ascii")},
        )

    monkeypatch.setattr(MockVideoProvider, "submit", delayed_video_submit)
    monkeypatch.setattr(MediaService, "_verify_video", staticmethod(lambda _path: 1.0))
    monkeypatch.setattr(
        DynamicAudioJobService,
        "_get_audio_duration",
        staticmethod(lambda _path: 1.0),
    )

    class FakeTTSProvider:
        async def synthesize(self, *, text, voice_id, params):
            return b"mock-audio"

        def supports_voice(self, _voice_id):
            return True

    original_provider_resolve = ProviderService.resolve
    monkeypatch.setattr(
        ProviderService,
        "resolve",
        lambda self, *, kind, provider_id=None: (
            FakeTTSProvider(),
            {"id": provider_id or "mock-tts-gate3", "model": "mock-tts-gate3", "params": {}},
        )
        if kind == "tts"
        else original_provider_resolve(self, kind=kind, provider_id=provider_id),
    )

    def fake_concat(self, _shots_data, _project_path, temp_path, **_kwargs):
        target = temp_path / "concat.mp4"
        target.write_bytes(b"mock-concat-mp4")
        return target

    monkeypatch.setattr(CompositionService, "_concat_videos", fake_concat)
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
        "_add_bgm",
        lambda self, video, *_args, **_kwargs: video,
    )
    monkeypatch.setattr(
        CompositionService,
        "_finalize_output",
        lambda self, video, output, _resolution: output.write_bytes(video.read_bytes() + b"-final"),
    )
    monkeypatch.setattr(CompositionService, "_get_video_duration", staticmethod(lambda _path: 1.0))
    monkeypatch.setattr(
        MediaService,
        "_extract_last_frame",
        staticmethod(lambda _source, _target: None),
    )
    from app.services import composition_service as composition_service_module
    from app.services import media_service as media_service_module

    monkeypatch.setattr(media_service_module, "file_service", context.service.storage)
    monkeypatch.setattr(composition_service_module, "file_service", context.service.storage)

    register_handlers(database=context.database, runner=runner)
    runner.start(recover=False)
    try:
        real_workflow.confirm_stage(
            setup.run_id,
            "4-storyboard",
            StageConfirmRequest(expected_revision=setup.stage4_saved["run"]["revision"]),
        )
        prompt_jobs = _prompt_jobs(context, setup.run_id)
        for prompt_job in prompt_jobs:
            runner.wait(prompt_job["id"], timeout=60)
        assert all(
            real_workflow.get_stage(setup.run_id, key)["status"] == "ready"
            for key in ("5a-image-prompts", "5b-video-prompts")
        )

        image_action = real_workflow.confirm_stage(
            setup.run_id,
            "5a-image-prompts",
            StageConfirmRequest(expected_revision=real_workflow.get_run(setup.run_id)["revision"]),
        )
        real_workflow.confirm_stage(
            setup.run_id,
            "5b-video-prompts",
            StageConfirmRequest(expected_revision=image_action["run"]["revision"]),
        )
        dynamic_jobs = repository.list_by_workflow(setup.run_id)
        image_job = next(item for item in dynamic_jobs if item["job_type"] == "generate_keyframe")
        video_job = next(item for item in dynamic_jobs if item["job_type"] == "generate_video")
        delivery_job = next(
            item
            for item in dynamic_jobs
            if item["workflow_stage_id"]
            == real_workflow.get_stage(setup.run_id, "6-delivery")["id"]
        )

        runner.wait(image_job["id"], timeout=60)
        if not video_started.wait(5):
            raise AssertionError(
                {
                    "image": repository.require(image_job["id"])["error"],
                    "video": repository.require(video_job["id"])["error"],
                    "delivery": repository.require(delivery_job["id"])["error"],
                }
            )
        assert repository.require(video_job["id"])["status"] in {
            "running",
            "waiting_provider",
            "downloading",
        }
        assert repository.require(delivery_job["id"])["status"] == "queued"
        assert not repository.list(workflow_run_id=setup.run_id, job_type="compose_chapter")

        release_video.set()
        runner.wait(video_job["id"], timeout=60)
        assert repository.require(image_job["id"])["status"] == "completed", repository.require(
            image_job["id"]
        )["error"]
        assert repository.require(video_job["id"])["status"] == "completed", repository.require(
            video_job["id"]
        )["error"]
        runner.enqueue(delivery_job["id"])
        runner.wait(delivery_job["id"], timeout=120)
        if repository.require(delivery_job["id"])["status"] != "completed":
            audio_errors = [
                item["error"]
                for item in repository.list_by_workflow(setup.run_id)
                if item["job_type"] == "generate_dialogue_audio"
            ]
            snapshot_debug = DynamicDeliveryService(
                context.database,
                file_service=context.service.storage,
            ).build_snapshot(setup.run_id)
            raise AssertionError(
                (
                    repository.require(delivery_job["id"])["error"],
                    audio_errors,
                    len(snapshot_debug["audios"]),
                    snapshot_debug["audios"],
                    len(snapshot_debug["subtitles"]),
                    repository.require(delivery_job["id"])["result_data"],
                )
            )
        assert repository.require(delivery_job["id"])["status"] == "completed", repository.require(
            delivery_job["id"]
        )["error"]
        composition_job = next(
            item
            for item in repository.list_by_workflow(setup.run_id)
            if item["job_type"] == "compose_chapter"
        )
        runner.wait(composition_job["id"], timeout=120)
        assert repository.require(composition_job["id"])["status"] == "completed"

        delivery_service = DynamicDeliveryService(
            context.database,
            file_service=context.service.storage,
        )
        zip_bytes, _filename, manifest = delivery_service.export(
            setup.run_id,
            strict=True,
            exported_at="2026-08-23T00:00:00Z",
        )
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            names = archive.namelist()
            manifest_json = json.loads(archive.read("metadata/manifest.json").decode("utf-8"))
        assert "README.md" in names
        assert "metadata/manifest.json" in names
        assert any(name.endswith(".png") for name in names)
        assert any(name.endswith(".mp4") for name in names)
        assert any(name.endswith(".mp3") for name in names)
        assert any(name.endswith(".srt") for name in names), names
        assert manifest["status"] == "success", {
            "missing": manifest.get("missing"),
            "failed": manifest.get("failed"),
            "skipped": manifest.get("skipped"),
            "preflight": manifest.get("preflight"),
        }
        assert manifest_json["entries"]
        assert all(
            item.get("sha256") and item.get("size", 0) > 0
            for item in manifest_json["entries"]
        )
        assert real_workflow.get_run(setup.run_id)["status"] == "confirmed"
    finally:
        runner.shutdown()
