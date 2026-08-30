from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from media_test_helpers import _PNG

from app.db import Database
from app.jobs.repository import JobRepository
from app.jobs.runner import JobRunner
from app.services import secret_service
from app.services.asset_service import AssetService
from app.services.chapter_service import ChapterService
from app.services.file_service import file_service
from app.services.job_service import JobService
from app.services.media_service import MediaService
from app.services.project_service import ProjectService


class _InMemorySecretBackend:
    def encrypt(self, plaintext: str) -> str:
        return f"fake:{plaintext}"

    def decrypt(self, ciphertext: str) -> str:
        if not ciphertext.startswith("fake:"):
            raise secret_service.SecretServiceError("fake 后端无法解密")
        return ciphertext.removeprefix("fake:")


@pytest.fixture(autouse=True)
def _use_in_memory_secret_backend_on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    """macOS 测试环境不写真实 Keychain，避免 CI/沙箱无法访问钥匙串。"""
    if sys.platform == "darwin":
        monkeypatch.setattr(secret_service, "_backend", _InMemorySecretBackend())


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
    project = ProjectService(database).create({"name": "媒体任务测试"})
    chapter = ChapterService(database).create(project["id"], {"title": "第一章"})
    media = MediaService(database, jobs)
    scene = AssetService(database, jobs).create(
        project["id"],
        {"asset_type": "scene", "name": "默认场景", "prompt": "默认场景设定"},
    )
    scene_image = media.upload_asset_image(scene["id"], "scene.png", "image/png", _PNG)
    try:
        yield {
            "database": database,
            "repository": repository,
            "runner": runner,
            "jobs": jobs,
            "media": media,
            "project": project,
            "chapter": chapter,
            "scene": scene,
            "scene_image": scene_image,
        }
    finally:
        runner.shutdown()
        database.dispose()

