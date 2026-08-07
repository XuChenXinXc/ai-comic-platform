from __future__ import annotations

import asyncio
import base64
import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import insert

from app.db import Database, utc_now
from app.jobs.recovery import recover_jobs
from app.jobs.repository import JobRepository, JobStateError
from app.jobs.runner import JobRunner
from app.models.tables import project
from app.schemas import JobCreate, ProviderPollResult
from app.services.file_service import file_service
from app.services.job_service import JobService
from app.services.media_service import MediaService


def _repository(tmp_path: Path) -> tuple[Database, JobRepository]:
    database = Database(f"sqlite:///{(tmp_path / 'jobs.db').as_posix()}")
    database.create_all()
    now = utc_now()
    with database.engine.begin() as connection:
        connection.execute(
            insert(project).values(
                id="project-1",
                name="测试项目",
                created_at=now,
                updated_at=now,
            )
        )
    return database, JobRepository(database)


def _create(repository: JobRepository, job_type: str = "analyze_novel") -> dict[str, Any]:
    return repository.create_job(
        JobCreate(
            project_id="project-1",
            job_type=job_type,
            provider_kind="video" if "video" in job_type else "text",
            input_data={"value": 1},
        )
    )


def _wait_for_status(
    repository: JobRepository,
    job_id: str,
    expected: str,
    timeout: float = 5,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = repository.require(job_id)
        if record["status"] == expected:
            return record
        time.sleep(0.01)
    pytest.fail(f"任务未进入预期状态：{expected}")


def test_job_runner_with_sync_handler_persists_completed_result(tmp_path: Path) -> None:
    database, repository = _repository(tmp_path)
    runner = JobRunner(repository, {"text": 1, "image": 1, "video": 1})
    handled = threading.Event()

    def handler(record: dict[str, Any]) -> dict[str, Any]:
        handled.set()
        return {"received": record["input_data"]["value"]}

    runner.register_handler("analyze_novel", handler)
    record = _create(repository)

    assert runner.enqueue(record["id"]) is True
    assert handled.wait(timeout=2) is True
    completed = _wait_for_status(repository, record["id"], "completed")

    assert completed["result_data"] == {"received": 1}
    assert completed["progress_current"] == 1
    runner.shutdown()
    database.dispose()


def test_job_runner_with_async_handler_persists_completed_result(tmp_path: Path) -> None:
    database, repository = _repository(tmp_path)
    runner = JobRunner(repository, {"text": 1, "image": 1, "video": 1})

    async def handler(_record: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(0)
        return {"mode": "async"}

    runner.register_handler("analyze_novel", handler)
    record = _create(repository)
    runner.enqueue(record["id"])

    completed = _wait_for_status(repository, record["id"], "completed")

    assert completed["result_data"] == {"mode": "async"}
    runner.shutdown()
    database.dispose()


def test_job_runner_image_batch_with_failed_item_completes_other_items(
    tmp_path: Path,
) -> None:
    database, repository = _repository(tmp_path)
    runner = JobRunner(repository, {"text": 1, "image": 1, "video": 1})
    service = JobService(database, repository=repository, runner=runner)

    def handler(record: dict[str, Any]) -> dict[str, Any]:
        if record["input_data"]["result"] == "failed":
            raise RuntimeError("图片生成失败")
        return {"result": "completed"}

    runner.register_handler("generate_asset_image", handler)
    batch = service.create_batch(
        [
            {
                "project_id": "project-1",
                "job_type": "generate_asset_image",
                "provider_kind": "image",
                "input_data": {"result": "failed"},
            },
            {
                "project_id": "project-1",
                "job_type": "generate_asset_image",
                "provider_kind": "image",
                "input_data": {"result": "completed"},
            },
        ]
    )

    failed = _wait_for_status(repository, batch["jobs"][0]["id"], "failed")
    completed = _wait_for_status(repository, batch["jobs"][1]["id"], "completed")

    assert failed["batch_id"] == batch["batch_id"]
    assert completed["batch_id"] == batch["batch_id"]
    assert completed["result_data"] == {"result": "completed"}
    runner.shutdown()
    database.dispose()


def test_job_runner_with_signed_url_error_persists_redacted_error(tmp_path: Path) -> None:
    database, repository = _repository(tmp_path)
    runner = JobRunner(repository, {"text": 1, "image": 1, "video": 1})

    def handler(_record: dict[str, Any]) -> None:
        raise RuntimeError("Provider 下载失败：https://cdn.invalid/video.mp4?token=secret-token")

    runner.register_handler("generate_video", handler)
    record = _create(repository, "generate_video")
    runner.enqueue(record["id"])

    failed = _wait_for_status(repository, record["id"], "failed")

    assert "secret-token" not in failed["error"]
    assert "https://cdn.invalid" not in failed["error"]
    runner.shutdown()
    database.dispose()


def test_recover_jobs_with_each_non_terminal_state_applies_safe_decision(
    tmp_path: Path,
) -> None:
    database, repository = _repository(tmp_path)
    queued = _create(repository)
    running = _create(repository)
    waiting = _create(repository)
    downloading = _create(repository, "generate_video")
    repository.update(running["id"], status="running")
    repository.set_provider_task(waiting["id"], "provider-task-1")
    repository.set_provider_task(
        downloading["id"],
        "provider-task-2",
        status="downloading",
        result_data={"result_url": "https://example.invalid/video.mp4"},
    )

    recovered = set(recover_jobs(repository))

    assert recovered == {queued["id"], waiting["id"], downloading["id"]}
    assert repository.require(running["id"])["status"] == "interrupted"
    assert repository.require(waiting["id"])["status"] == "queued"
    assert repository.require(downloading["id"])["status"] == "queued"
    database.dispose()


def test_recover_downloading_job_with_nested_local_file_requeues_original_job(
    tmp_path: Path,
) -> None:
    database, repository = _repository(tmp_path)
    record = _create(repository, "generate_video")
    repository.set_provider_task(
        record["id"],
        "provider-task-with-staged-result",
        status="downloading",
        result_data={
            "status": "completed",
            "data": {"local_file": str(tmp_path / "provider-result.bin")},
        },
    )

    recovered = recover_jobs(repository)

    assert recovered == [record["id"]]
    assert repository.require(record["id"])["status"] != "failed"
    database.dispose()


def test_job_retry_after_video_download_failure_keeps_original_provider_task(
    tmp_path: Path,
) -> None:
    database, repository = _repository(tmp_path)
    record = _create(repository, "generate_video")
    repository.set_provider_task(
        record["id"],
        "paid-provider-task",
        status="downloading",
        result_data={"result_url": "https://example.invalid/video.mp4"},
    )
    repository.fail(record["id"], "下载失败")

    retried = repository.retry(record["id"])

    assert retried["status"] == "downloading"
    assert retried["provider_task_id"] == "paid-provider-task"
    assert retried["retry_count"] == 1
    database.dispose()


def test_job_retry_with_nested_local_file_resumes_download_without_polling(
    tmp_path: Path,
) -> None:
    database, repository = _repository(tmp_path)
    record = _create(repository, "generate_video")
    repository.set_provider_task(
        record["id"],
        "paid-provider-task",
        status="downloading",
        result_data={
            "status": "completed",
            "data": {"local_file": str(tmp_path / "provider-result.bin")},
        },
    )
    repository.fail(record["id"], "下载失败")

    retried = repository.retry(record["id"])

    assert retried["status"] == "downloading"
    assert retried["provider_task_id"] == "paid-provider-task"
    database.dispose()


def test_two_runners_with_same_waiting_job_executes_handler_once(tmp_path: Path) -> None:
    database, repository = _repository(tmp_path)
    record = _create(repository, "generate_video")
    repository.set_provider_task(record["id"], "provider-task", status="waiting_provider")
    first_started = threading.Event()
    second_started = threading.Event()
    release = threading.Event()
    call_count = 0
    lock = threading.Lock()

    def handler(_record: dict[str, Any]) -> dict[str, Any]:
        nonlocal call_count
        with lock:
            call_count += 1
            if call_count == 1:
                first_started.set()
            else:
                second_started.set()
        release.wait(timeout=2)
        return {"ok": True}

    first_runner = JobRunner(repository, {"text": 1, "image": 1, "video": 1})
    second_runner = JobRunner(repository, {"text": 1, "image": 1, "video": 1})
    first_runner.register_handler("generate_video", handler)
    second_runner.register_handler("generate_video", handler)

    try:
        assert first_runner.enqueue(record["id"]) is True
        assert first_started.wait(timeout=1) is True
        assert second_runner.enqueue(record["id"]) is True
        assert second_started.wait(timeout=0.25) is False
    finally:
        release.set()
        first_runner.shutdown()
        second_runner.shutdown()
        database.dispose()

    assert call_count == 1


def test_job_cancel_after_started_rejects_transition(tmp_path: Path) -> None:
    database, repository = _repository(tmp_path)
    record = _create(repository)
    repository.update(record["id"], status="running")

    with pytest.raises(JobStateError):
        repository.cancel(record["id"])
    database.dispose()


def test_job_service_get_with_provider_secret_result_hides_sensitive_diagnostics(
    tmp_path: Path,
) -> None:
    database, repository = _repository(tmp_path)
    record = _create(repository, "generate_video")
    repository.save_result(
        record["id"],
        {
            "result_url": "https://cdn.invalid/video.mp4?signed=secret-token",
            "content_base64": "secret-base64",
            "data": {"raw": {"authorization": "Bearer secret-token"}},
        },
    )
    runner = JobRunner(repository, {"text": 1, "image": 1, "video": 1})
    service = JobService(database, repository=repository, runner=runner)

    public_record = service.get(record["id"])

    assert "secret-token" not in json.dumps(public_record)
    assert public_record["result_json"]["result_url"] == "[已隐藏]"
    runner.shutdown()
    database.dispose()


def test_media_save_provider_result_with_base64_stages_file_outside_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, repository = _repository(tmp_path)
    record = _create(repository, "generate_image")
    monkeypatch.setattr(file_service, "workspace_dir", tmp_path.resolve())
    monkeypatch.setattr(file_service, "projects_dir", (tmp_path / "projects").resolve())
    file_service.projects_dir.mkdir(parents=True, exist_ok=True)
    service = MediaService(database)
    encoded = base64.b64encode(b"provider-binary-result").decode("ascii")

    stored = service._save_provider_result(
        record["id"],
        ProviderPollResult(status="completed", content_base64=encoded),
    )
    persisted = repository.require(record["id"])

    assert encoded not in persisted["result_json"]
    assert stored.content_base64 == ""
    assert Path(stored.data["local_file"]).read_bytes() == b"provider-binary-result"
    service.jobs.runner.shutdown()
    database.dispose()
