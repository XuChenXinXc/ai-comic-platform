from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.db import Database
from app.models.tables import novel_analysis
from app.services.project_service import ProjectService
from app.services.utils import json_dumps
from app.services.writing_service import WritingService


class RecordingJobService:
    def __init__(self) -> None:
        self.created: dict[str, Any] = {}

    def create(self, **values: Any) -> dict[str, Any]:
        self.created = values
        return {"id": "job-recorded", **values}


class FailingTextProvider:
    async def generate_json(self, **_values: Any) -> Any:
        raise RuntimeError("模拟生成失败")


@pytest.fixture
def writing_service(
    tmp_path: Path,
) -> tuple[Database, WritingService, RecordingJobService, dict[str, Any]]:
    database = Database(f"sqlite:///{(tmp_path / 'writing.db').as_posix()}")
    database.initialize(seed_mock_providers=True)
    project = ProjectService(database).create(
        {"name": "写作并发测试", "source_text": "主角在雨夜发现一封信。"}
    )
    jobs = RecordingJobService()
    service = WritingService(database, jobs=jobs)  # type: ignore[arg-type]
    yield database, service, jobs, project
    database.dispose()


def _analysis_content(summary: str) -> dict[str, Any]:
    return {
        "summary": summary,
        "genre": "悬疑",
        "time_period": "当代",
        "locations": ["旧屋"],
        "worldview": "现实世界",
        "main_characters": ["主角"],
        "character_relations": [],
        "core_conflict": "主角必须查明真相。",
        "plot_structure": {"beginning": "发现信件", "middle": "追查线索", "end": "揭露真相"},
        "emotion_curve": [{"stage": "开端", "emotion": "紧张"}],
        "foreshadowing": ["信封上的旧印章"],
        "visualizable_content": ["雨夜旧屋"],
        "hard_to_visualize": [],
        "production_risks": [],
    }


def _screenplay_content(title: str, action: str = "主角拆开信封。") -> dict[str, Any]:
    return {
        "title": title,
        "logline": "主角从一封旧信中发现真相。",
        "scenes": [
            {
                "scene_no": 1,
                "scene_name": "雨夜旧屋",
                "time_weather": "夜晚，暴雨",
                "characters": ["主角"],
                "objective": "确认信件内容",
                "action": action,
                "dialogue": [],
                "narration": [],
                "emotion": "紧张",
                "transition_to_next": "镜头切到信纸特写。",
            }
        ],
    }


def _queued_job(
    project_id: str,
    jobs: RecordingJobService,
) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "provider_id": "mock-text",
        "input_json": json_dumps(jobs.created["input_data"]),
    }


def test_analysis_completed_after_manual_save_preserves_manual_content(
    writing_service: tuple[Database, WritingService, RecordingJobService, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _database, service, jobs, project = writing_service
    service.queue_analysis(project["id"], {})
    assert jobs.created["input_data"]["expected_revision"] == 0

    manual_content = _analysis_content("人工修订内容")
    service.update_analysis(project["id"], {"content": manual_content})

    def fail_if_provider_is_resolved(_provider_id: str | None) -> Any:
        raise AssertionError("过期写作任务不应调用 Provider")

    monkeypatch.setattr(service, "_provider", fail_if_provider_is_resolved)
    result = asyncio.run(service.handle_analysis(_queued_job(project["id"], jobs)))
    current = service.get_analysis(project["id"])

    assert result["skipped"] is True
    assert result["reason"] == "内容在生成期间已被编辑，生成结果未覆盖当前版本"
    assert result["generated_content"] == {}
    assert current["content"] == manual_content
    assert current["status"] == "ready"
    assert current["revision"] == 1


def test_screenplay_queue_after_manual_save_records_current_revision(
    writing_service: tuple[Database, WritingService, RecordingJobService, dict[str, Any]],
) -> None:
    _database, service, jobs, project = writing_service
    service.update_screenplay(
        project["id"],
        {"content": _screenplay_content("人工剧本")},
    )

    service.queue_screenplay(project["id"], {})

    assert jobs.created["input_data"]["expected_revision"] == 1


def test_screenplay_scene_completed_after_manual_save_preserves_manual_content(
    writing_service: tuple[Database, WritingService, RecordingJobService, dict[str, Any]],
) -> None:
    _database, service, jobs, project = writing_service
    service.update_screenplay(
        project["id"],
        {"content": _screenplay_content("初始剧本")},
    )
    service.queue_screenplay_scene(project["id"], {"scene_no": 1})
    assert jobs.created["input_data"]["expected_revision"] == 1
    manual_content = _screenplay_content("人工修订剧本", "主角把信件锁进抽屉。")
    service.update_screenplay(project["id"], {"content": manual_content})

    result = asyncio.run(service.handle_screenplay_scene(_queued_job(project["id"], jobs)))
    current = service.get_screenplay(project["id"])

    assert result["skipped"] is True
    assert result["reason"] == "内容在生成期间已被编辑，生成结果未覆盖当前版本"
    assert result["generated_content"] != manual_content
    assert current["content"] == manual_content
    assert current["status"] == "ready"
    assert current["revision"] == 2


def test_analysis_generation_failure_marks_matching_revision_failed(
    writing_service: tuple[Database, WritingService, RecordingJobService, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _database, service, jobs, project = writing_service
    service.queue_analysis(project["id"], {})
    monkeypatch.setattr(
        service,
        "_provider",
        lambda _provider_id: (
            FailingTextProvider(),
            {"id": "failing-text", "params": {}},
        ),
    )

    with pytest.raises(RuntimeError, match="模拟生成失败"):
        asyncio.run(service.handle_analysis(_queued_job(project["id"], jobs)))

    current = service.get_analysis(project["id"])
    assert current["content"] == {}
    assert current["status"] == "failed"
    assert current["revision"] == 0


def test_legacy_analysis_job_with_generating_target_uses_current_revision(
    writing_service: tuple[Database, WritingService, RecordingJobService, dict[str, Any]],
) -> None:
    _database, service, _jobs, project = writing_service
    service._set_status(novel_analysis, project["id"], "generating")
    legacy_job = {
        "project_id": project["id"],
        "provider_id": "mock-text",
        "input_json": json_dumps({"params": {}}),
    }

    result = asyncio.run(service.handle_analysis(legacy_job))
    current = service.get_analysis(project["id"])

    assert result["content"]["summary"]
    assert current["status"] == "ready"
    assert current["revision"] == 1


def test_legacy_analysis_job_with_ready_target_preserves_current_content(
    writing_service: tuple[Database, WritingService, RecordingJobService, dict[str, Any]],
) -> None:
    _database, service, _jobs, project = writing_service
    manual_content = _analysis_content("旧任务执行前已人工保存")
    service.update_analysis(project["id"], {"content": manual_content})
    legacy_job = {
        "project_id": project["id"],
        "provider_id": "mock-text",
        "input_json": json_dumps({"params": {}}),
    }

    result = asyncio.run(service.handle_analysis(legacy_job))
    current = service.get_analysis(project["id"])

    assert result["skipped"] is True
    assert current["content"] == manual_content
    assert current["status"] == "ready"
    assert current["revision"] == 1


def test_analysis_provider_resolution_failure_marks_matching_revision_failed(
    writing_service: tuple[Database, WritingService, RecordingJobService, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _database, service, jobs, project = writing_service
    service.queue_analysis(project["id"], {})

    def fail_provider_resolution(_provider_id: str | None) -> Any:
        raise RuntimeError("Provider 配置不可用")

    monkeypatch.setattr(service, "_provider", fail_provider_resolution)

    with pytest.raises(RuntimeError, match="Provider 配置不可用"):
        asyncio.run(service.handle_analysis(_queued_job(project["id"], jobs)))

    current = service.get_analysis(project["id"])
    assert current["status"] == "failed"
    assert current["revision"] == 0
