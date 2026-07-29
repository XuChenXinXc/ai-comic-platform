import base64
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import insert, select

from app.api.schemas import ShotCreate
from app.db import Database
from app.jobs.repository import JobRepository
from app.jobs.runner import JobRunner
from app.models.tables import asset, asset_image, novel_analysis, screenplay, video_version
from app.services.asset_service import AssetService
from app.services.errors import ServiceError
from app.services.file_service import file_service
from app.services.job_service import JobService
from app.services.media_service import MediaService
from app.services.project_service import ProjectService
from app.services.shot_service import ShotService
from app.services.utils import utc_now

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
_PROVIDER_RESOLVE_ERROR = (
    "Provider 配置失效：https://provider.invalid/task?token=secret-token\n"
    "Authorization: Bearer top-secret"
)


@pytest.fixture
def job_service(tmp_path: Path) -> JobService:
    database = Database(f"sqlite:///{(tmp_path / 'jobs.db').as_posix()}")
    database.initialize(seed_mock_providers=True)
    project = ProjectService(database).create({"name": "任务测试", "source_text": ""})
    repository = JobRepository(database)
    runner = JobRunner(repository, {"text": 1, "image": 1, "video": 1})
    service = JobService(database, repository=repository, runner=runner)
    service.project_id = project["id"]  # type: ignore[attr-defined]
    yield service
    runner.shutdown()
    database.dispose()


@pytest.fixture
def media_job_services(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[dict[str, Any]]:
    database = Database(f"sqlite:///{(tmp_path / 'media-jobs.db').as_posix()}")
    database.initialize(seed_mock_providers=True)
    monkeypatch.setattr(file_service, "workspace_dir", tmp_path.resolve())
    monkeypatch.setattr(file_service, "projects_dir", (tmp_path / "projects").resolve())
    file_service.projects_dir.mkdir(parents=True, exist_ok=True)
    repository = JobRepository(database)
    runner = JobRunner(repository, {"text": 1, "image": 1, "video": 1})
    jobs = JobService(database, repository=repository, runner=runner)
    project = ProjectService(database).create({"name": "媒体任务测试", "source_text": ""})
    yield {
        "database": database,
        "repository": repository,
        "runner": runner,
        "jobs": jobs,
        "media": MediaService(database, jobs),
        "project": project,
    }
    runner.shutdown()
    database.dispose()


def _wait_for_job_status(
    repository: JobRepository,
    job_id: str,
    expected_status: str,
) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        record = repository.require(job_id)
        if record["status"] == expected_status:
            return record
        time.sleep(0.01)
    pytest.fail(f"任务未进入预期状态：{expected_status}")


def _fail_second_provider_resolve(
    media: MediaService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_resolve = media.providers.resolve
    resolve_count = 0

    def resolve(*args: Any, **kwargs: Any) -> Any:
        nonlocal resolve_count
        resolve_count += 1
        if resolve_count == 1:
            return original_resolve(*args, **kwargs)
        raise RuntimeError(_PROVIDER_RESOLVE_ERROR)

    monkeypatch.setattr(media.providers, "resolve", resolve)


def test_image_job_with_provider_resolve_failure_marks_version_failed(
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = media_job_services["database"]
    repository = media_job_services["repository"]
    runner = media_job_services["runner"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    current_asset = AssetService(database, jobs).create(
        project["id"],
        {"asset_type": "character", "name": "主角", "prompt": "主角立绘"},
    )
    runner.register_handler("generate_asset_image", media.handle_asset_image)
    _fail_second_provider_resolve(media, monkeypatch)

    queued = media.queue_asset_image(current_asset["id"], {})
    failed_job = _wait_for_job_status(repository, queued["id"], "failed")

    with database.engine.connect() as connection:
        target = connection.execute(
            select(asset_image.c.status, asset_image.c.error).where(
                asset_image.c.id == queued["target_id"]
            )
        ).one()
    assert failed_job["status"] == "failed"
    assert target.status == "failed"
    assert "secret-token" not in target.error
    assert "top-secret" not in target.error
    assert "[已隐藏" in target.error


def test_video_job_with_provider_resolve_failure_marks_version_failed(
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = media_job_services["database"]
    repository = media_job_services["repository"]
    runner = media_job_services["runner"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    current_shot = ShotService(database, jobs).create(
        project["id"],
        ShotCreate(
            source_ref="manual-provider-failure-test",
            action="主角向前走",
            image_prompt="主角站在走廊",
            video_prompt="主角向前走",
        ).model_dump(),
    )
    media.upload_keyframe(
        current_shot["id"],
        "frame.png",
        "image/png",
        _PNG,
    )
    runner.register_handler("generate_video", media.handle_video)
    _fail_second_provider_resolve(media, monkeypatch)

    queued = media.queue_video(current_shot["id"], {})
    failed_job = _wait_for_job_status(repository, queued["id"], "failed")

    with database.engine.connect() as connection:
        target = connection.execute(
            select(video_version.c.status, video_version.c.error).where(
                video_version.c.id == queued["target_id"]
            )
        ).one()
    assert failed_job["status"] == "failed"
    assert target.status == "failed"
    assert "secret-token" not in target.error
    assert "top-secret" not in target.error
    assert "[已隐藏" in target.error


def test_cancel_queued_job_returns_cancelled_and_second_cancel_conflicts(
    job_service: JobService,
) -> None:
    record = job_service.repository.create_job(
        project_id=job_service.project_id,  # type: ignore[attr-defined]
        job_type="never_run",
    )
    cancelled = job_service.cancel(record["id"])
    assert cancelled["status"] == "cancelled"
    with pytest.raises(ServiceError) as exc_info:
        job_service.cancel(record["id"])
    assert exc_info.value.status_code == 409


def test_cancel_when_target_sync_fails_rolls_back_job_status(
    job_service: JobService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = job_service.repository.create_job(
        project_id=job_service.project_id,  # type: ignore[attr-defined]
        job_type="never_run",
    )

    def fail_target_sync(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("同步目标失败")

    monkeypatch.setattr(job_service, "_sync_cancelled_target", fail_target_sync)

    with pytest.raises(RuntimeError, match="同步目标失败"):
        job_service.cancel(record["id"])

    persisted = job_service.repository.require(record["id"])
    assert persisted["status"] == "queued"


def test_cancel_queued_media_job_marks_target_version_failed(
    job_service: JobService,
) -> None:
    now = utc_now()
    asset_id = "asset-cancel"
    image_id = "asset-image-cancel"
    with job_service.repository.engine.begin() as connection:
        connection.execute(
            insert(asset).values(
                id=asset_id,
                project_id=job_service.project_id,  # type: ignore[attr-defined]
                asset_type="character",
                name="待取消媒体",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(asset_image).values(
                id=image_id,
                asset_id=asset_id,
                version_no=1,
                status="queued",
                created_at=now,
            )
        )
    record = job_service.repository.create_job(
        project_id=job_service.project_id,  # type: ignore[attr-defined]
        job_type="generate_asset_image",
        target_type="asset_image",
        target_id=image_id,
        provider_kind="image",
    )

    job_service.cancel(record["id"])

    with job_service.repository.engine.connect() as connection:
        target = connection.execute(
            select(asset_image.c.status, asset_image.c.error).where(asset_image.c.id == image_id)
        ).one()
    assert target.status == "failed"
    assert target.error == "关联任务已取消"


@pytest.mark.parametrize(
    ("table", "job_type", "target_type", "target_suffix"),
    [
        (novel_analysis, "analyze_novel", "analysis", ""),
        (screenplay, "generate_screenplay", "screenplay", ""),
        (screenplay, "generate_screenplay_scene", "screenplay_scene", ":1"),
    ],
)
def test_cancel_queued_writing_job_marks_generating_target_failed(
    job_service: JobService,
    table: object,
    job_type: str,
    target_type: str,
    target_suffix: str,
) -> None:
    project_id = job_service.project_id  # type: ignore[attr-defined]
    with job_service.repository.engine.begin() as connection:
        connection.execute(
            insert(table).values(
                project_id=project_id,
                status="generating",
                updated_at=utc_now(),
            )
        )
    record = job_service.repository.create_job(
        project_id=project_id,
        job_type=job_type,
        target_type=target_type,
        target_id=f"{project_id}{target_suffix}",
        provider_kind="text",
    )

    job_service.cancel(record["id"])

    with job_service.repository.engine.connect() as connection:
        target = connection.execute(
            select(table.c.status, table.c.error).where(table.c.project_id == project_id)
        ).one()
    assert target.status == "failed"
    assert target.error == "关联任务已取消"


def test_retry_failed_job_requeues_same_id_and_increments_retry_count(
    job_service: JobService,
) -> None:
    record = job_service.repository.create_job(
        project_id=job_service.project_id,  # type: ignore[attr-defined]
        job_type="retry_me",
    )
    job_service.repository.fail(record["id"], "首次失败")
    retried = job_service.retry(record["id"])
    assert retried["id"] == record["id"]
    assert retried["retry_count"] == 1
    assert retried["status"] in {"queued", "failed"}
