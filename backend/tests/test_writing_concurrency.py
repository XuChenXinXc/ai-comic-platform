from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import insert

from app.db import Database
from app.models.tables import asset, novel_analysis
from app.prompts import load_prompt
from app.schemas import NovelAnalysisOutput, ScreenplayOutput
from app.services.chapter_service import ChapterService
from app.services.file_service import FileService
from app.services.project_service import ProjectService
from app.services.utils import json_dumps, utc_now
from app.services.writing_service import WritingService


class RecordingJobService:
    def __init__(self) -> None:
        self.created: dict[str, Any] = {}

    def create(self, **values: Any) -> dict[str, Any]:
        self.created = values
        return {"id": "job-recorded", **values}


class FailingCreateJobService:
    def create(self, **values: Any) -> dict[str, Any]:
        raise RuntimeError("模拟建任务失败")


class FailingTextProvider:
    async def generate_json(self, **_values: Any) -> Any:
        raise RuntimeError("模拟生成失败")


class SequencedTextProvider:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def generate_json(self, **values: Any) -> Any:
        self.calls.append(values)
        if values["schema"] is NovelAnalysisOutput:
            return NovelAnalysisOutput.model_validate(_analysis_content("本次自动分析"))
        if values["schema"] is ScreenplayOutput:
            return ScreenplayOutput.model_validate(_screenplay_content("本次自动剧本"))
        raise AssertionError("出现未预期的写作 Schema")


@pytest.fixture
def writing_service(
    tmp_path: Path,
) -> tuple[Database, WritingService, RecordingJobService, dict[str, Any], dict[str, Any]]:
    database = Database(f"sqlite:///{(tmp_path / 'writing.db').as_posix()}")
    database.initialize(seed_mock_providers=True)
    project = ProjectService(database, FileService(tmp_path)).create({"name": "写作并发测试"})
    chapter = ChapterService(database, FileService(tmp_path)).create(
        project["id"], {"title": "第一章", "source_text": "主角在雨夜发现一封信。"}
    )
    jobs = RecordingJobService()
    service = WritingService(database, jobs=jobs)  # type: ignore[arg-type]
    yield database, service, jobs, project, chapter
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
    chapter_id: str,
    jobs: RecordingJobService,
    *,
    project_id: str = "",
) -> dict[str, Any]:
    return {
        "project_id": project_id or jobs.created.get("project_id", ""),
        "chapter_id": chapter_id,
        "provider_id": "mock-text",
        "input_json": json_dumps(jobs.created["input_data"]),
    }


def test_analysis_completed_after_manual_save_preserves_manual_content(
    writing_service: tuple[
        Database, WritingService, RecordingJobService, dict[str, Any], dict[str, Any]
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _database, service, jobs, project, chapter = writing_service
    service.queue_analysis(chapter["id"], {})
    assert jobs.created["input_data"]["expected_revision"] == 0

    manual_content = _analysis_content("人工修订内容")
    service.update_analysis(chapter["id"], {"content": manual_content})

    def fail_if_provider_is_resolved(_provider_id: str | None) -> Any:
        raise AssertionError("过期写作任务不应调用 Provider")

    monkeypatch.setattr(service, "_provider", fail_if_provider_is_resolved)
    result = asyncio.run(service.handle_analysis(_queued_job(chapter["id"], jobs)))
    current = service.get_analysis(chapter["id"])

    assert result["skipped"] is True
    assert result["reason"] == "内容在生成期间已被编辑，生成结果未覆盖当前版本"
    assert result["generated_content"] == {}
    assert current["content"] == manual_content
    assert current["status"] == "ready"
    assert current["revision"] == 1


def test_screenplay_queue_after_manual_save_records_current_revision(
    writing_service: tuple[
        Database, WritingService, RecordingJobService, dict[str, Any], dict[str, Any]
    ],
) -> None:
    _database, service, jobs, project, chapter = writing_service
    service.update_screenplay(
        chapter["id"],
        {"content": _screenplay_content("人工剧本")},
    )

    service.queue_screenplay(chapter["id"], {})

    assert jobs.created["input_data"]["expected_revision"] == 1


def test_screenplay_generation_runs_analysis_before_screenplay(
    writing_service: tuple[
        Database, WritingService, RecordingJobService, dict[str, Any], dict[str, Any]
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, service, jobs, project, chapter = writing_service
    project_service = ProjectService(database)
    project_service.update(project["id"], {"style_prompt": "用户点击前填写的旧画风"})
    provider = SequencedTextProvider()
    monkeypatch.setattr(
        service,
        "_provider",
        lambda _provider_id: (
            provider,
            {"id": "mock-text", "params": {}},
        ),
    )

    service.queue_screenplay(chapter["id"], {})
    result = asyncio.run(service.handle_screenplay(_queued_job(chapter["id"], jobs)))

    assert [call["schema"] for call in provider.calls] == [
        NovelAnalysisOutput,
        ScreenplayOutput,
    ]
    assert provider.calls[0]["system_prompt"] == load_prompt("novel_analysis")
    assert provider.calls[1]["system_prompt"] == load_prompt("screenplay")
    assert service.get_analysis(chapter["id"])["content"]["summary"] == "本次自动分析"
    assert "本次自动分析" in provider.calls[1]["user_prompt"]
    assert service.get_screenplay(chapter["id"])["content"]["title"] == "本次自动剧本"
    # 画风只允许用户手动修改：生成剧本不得改写它
    assert project_service.get(project["id"])["style_prompt"] == "用户点击前填写的旧画风"
    assert "style_prompt" not in result
    assert result["content"]["title"] == "本次自动剧本"


def test_stale_screenplay_job_restores_hidden_analysis_status(
    writing_service: tuple[
        Database, WritingService, RecordingJobService, dict[str, Any], dict[str, Any]
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _database, service, jobs, project, chapter = writing_service
    service.queue_screenplay(chapter["id"], {})
    service.update_screenplay(
        chapter["id"],
        {"content": _screenplay_content("人工修订剧本")},
    )

    def fail_if_provider_is_resolved(_provider_id: str | None) -> Any:
        raise AssertionError("过期剧本任务不应调用 Provider")

    monkeypatch.setattr(service, "_provider", fail_if_provider_is_resolved)

    result = asyncio.run(service.handle_screenplay(_queued_job(chapter["id"], jobs)))

    assert result["skipped"] is True
    assert service.get_analysis(chapter["id"])["status"] == "draft"


def test_screenplay_scene_completed_after_manual_save_preserves_manual_content(
    writing_service: tuple[
        Database, WritingService, RecordingJobService, dict[str, Any], dict[str, Any]
    ],
) -> None:
    _database, service, jobs, project, chapter = writing_service
    service.update_screenplay(
        chapter["id"],
        {"content": _screenplay_content("初始剧本")},
    )
    service.queue_screenplay_scene(chapter["id"], {"scene_no": 1})
    assert jobs.created["input_data"]["expected_revision"] == 1
    manual_content = _screenplay_content("人工修订剧本", "主角把信件锁进抽屉。")
    service.update_screenplay(chapter["id"], {"content": manual_content})

    result = asyncio.run(service.handle_screenplay_scene(_queued_job(chapter["id"], jobs)))
    current = service.get_screenplay(chapter["id"])

    assert result["skipped"] is True
    assert result["reason"] == "内容在生成期间已被编辑，生成结果未覆盖当前版本"
    assert result["generated_content"] != manual_content
    assert current["content"] == manual_content
    assert current["status"] == "ready"
    assert current["revision"] == 2


def test_analysis_generation_failure_marks_matching_revision_failed(
    writing_service: tuple[
        Database, WritingService, RecordingJobService, dict[str, Any], dict[str, Any]
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _database, service, jobs, project, chapter = writing_service
    service.queue_analysis(chapter["id"], {})
    monkeypatch.setattr(
        service,
        "_provider",
        lambda _provider_id: (
            FailingTextProvider(),
            {"id": "failing-text", "params": {}},
        ),
    )

    with pytest.raises(RuntimeError, match="模拟生成失败"):
        asyncio.run(service.handle_analysis(_queued_job(chapter["id"], jobs)))

    current = service.get_analysis(chapter["id"])
    assert current["content"] == {}
    assert current["status"] == "failed"
    assert current["revision"] == 0


def test_legacy_analysis_job_with_generating_target_uses_current_revision(
    writing_service: tuple[
        Database, WritingService, RecordingJobService, dict[str, Any], dict[str, Any]
    ],
) -> None:
    _database, service, _jobs, project, chapter = writing_service
    service._set_status(novel_analysis, chapter["id"], "generating")
    legacy_job = {
        "project_id": project["id"],
        "chapter_id": chapter["id"],
        "provider_id": "mock-text",
        "input_json": json_dumps({"params": {}}),
    }

    result = asyncio.run(service.handle_analysis(legacy_job))
    current = service.get_analysis(chapter["id"])

    assert result["content"]["summary"]
    assert current["status"] == "ready"
    assert current["revision"] == 1


def test_legacy_analysis_job_with_ready_target_preserves_current_content(
    writing_service: tuple[
        Database, WritingService, RecordingJobService, dict[str, Any], dict[str, Any]
    ],
) -> None:
    _database, service, _jobs, project, chapter = writing_service
    manual_content = _analysis_content("旧任务执行前已人工保存")
    service.update_analysis(chapter["id"], {"content": manual_content})
    legacy_job = {
        "project_id": project["id"],
        "chapter_id": chapter["id"],
        "provider_id": "mock-text",
        "input_json": json_dumps({"params": {}}),
    }

    result = asyncio.run(service.handle_analysis(legacy_job))
    current = service.get_analysis(chapter["id"])

    assert result["skipped"] is True
    assert current["content"] == manual_content
    assert current["status"] == "ready"
    assert current["revision"] == 1


def test_analysis_provider_resolution_failure_marks_matching_revision_failed(
    writing_service: tuple[
        Database, WritingService, RecordingJobService, dict[str, Any], dict[str, Any]
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _database, service, jobs, project, chapter = writing_service
    service.queue_analysis(chapter["id"], {})

    def fail_provider_resolution(_provider_id: str | None) -> Any:
        raise RuntimeError("Provider 配置不可用")

    monkeypatch.setattr(service, "_provider", fail_provider_resolution)

    with pytest.raises(RuntimeError, match="Provider 配置不可用"):
        asyncio.run(service.handle_analysis(_queued_job(chapter["id"], jobs)))

    current = service.get_analysis(chapter["id"])
    assert current["status"] == "failed"
    assert current["revision"] == 0


def test_screenplay_generation_passes_existing_scene_asset_names_to_model(
    writing_service: tuple[
        Database, WritingService, RecordingJobService, dict[str, Any], dict[str, Any]
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """剧本生成必须把已有场景素材名喂给模型，否则 scene_name 无法与素材库对齐。

    回归 2026-08 缺陷：素材提取读章节正文、剧本生成只读正文+分析，两步各自命名，
    剧本的 scene_name 与场景素材名必然对不上，分镜保存时按名称绑定场景素材会
    整批失败（「未匹配到场景素材：坐错桌」）。
    """
    database, service, jobs, project, chapter = writing_service
    now = utc_now()
    with database.engine.begin() as connection:
        connection.execute(
            insert(asset).values(
                id="asset-scene-1",
                project_id=project["id"],
                asset_type="scene",
                name="烧烤店",
                profile_json=json_dumps({"aliases": ["路边烧烤"]}),
                created_at=now,
                updated_at=now,
            )
        )
        # 角色素材不应出现在剧本的场景名清单里
        connection.execute(
            insert(asset).values(
                id="asset-character-1",
                project_id=project["id"],
                asset_type="character",
                name="林雪",
                profile_json=json_dumps({}),
                created_at=now,
                updated_at=now,
            )
        )
    provider = SequencedTextProvider()
    monkeypatch.setattr(
        service,
        "_provider",
        lambda _provider_id: (provider, {"id": "mock-text", "params": {}}),
    )

    service.queue_screenplay(chapter["id"], {})
    asyncio.run(service.handle_screenplay(_queued_job(chapter["id"], jobs)))

    screenplay_prompt = provider.calls[1]["user_prompt"]
    assert "已有场景素材名称" in screenplay_prompt
    assert "烧烤店" in screenplay_prompt
    assert "路边烧烤" in screenplay_prompt
    # 只传场景素材，不混入角色
    assert "林雪" not in screenplay_prompt


def test_screenplay_generation_warns_when_no_scene_assets_exist(
    writing_service: tuple[
        Database, WritingService, RecordingJobService, dict[str, Any], dict[str, Any]
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """素材库为空时生成剧本：返回 warnings 提示应先提取素材。

    回归 2026-08 缺陷：剧本生成先于素材提取时，剧本模型拿不到任何场景素材
    名称作为锚点，会把桥段标题（「坐错桌」）写进 scene_name；后续分镜按名称
    绑定场景素材必然整批失败。现在生成结果里附带明确提示，引导用户先补素材
    再重新生成剧本。
    """
    database, service, jobs, project, chapter = writing_service
    provider = SequencedTextProvider()
    monkeypatch.setattr(
        service,
        "_provider",
        lambda _provider_id: (
            provider,
            {"id": "mock-text", "params": {}},
        ),
    )
    # 该 fixture 的项目没有创建任何 scene 素材（含别名的场景素材测试在上一个用例）

    service.queue_screenplay(chapter["id"], {})
    result = asyncio.run(service.handle_screenplay(_queued_job(chapter["id"], jobs)))

    assert result["content"]["title"] == "本次自动剧本"
    assert result["warnings"]
    assert any("尚无场景素材" in message for message in result["warnings"])


def test_screenplay_scene_queue_create_failure_reverts_generating_status(
    writing_service: tuple[
        Database, WritingService, RecordingJobService, dict[str, Any], dict[str, Any]
    ],
) -> None:
    """queue_screenplay_scene 建任务失败时回退 generating 状态。

    回归：jobs.create 抛异常时剧本行永久停在 generating，前端一直显示「生成中」。
    """
    database, service, _jobs, _project, chapter = writing_service
    service.update_screenplay(
        chapter["id"],
        {"content": _screenplay_content("人工剧本")},
    )
    service.jobs = FailingCreateJobService()  # type: ignore[assignment]
    with pytest.raises(RuntimeError):
        service.queue_screenplay_scene(chapter["id"], {"scene_no": 1})
    current = service.get_screenplay(chapter["id"])
    assert current["status"] == "ready"
    assert current["revision"] == 1  # 失败不推进 revision


def test_scene_asset_names_returns_only_scene_assets_of_project(
    writing_service: tuple[
        Database, WritingService, RecordingJobService, dict[str, Any], dict[str, Any]
    ],
) -> None:
    """场景名清单按项目过滤且只含 scene 类型，别名缺失时返回空列表。"""
    database, service, _jobs, project, _chapter = writing_service
    # FileService 需要目录而非 database_url：从 sqlite URL 还原工作区目录。
    workspace_path = Path(database.database_url.removeprefix("sqlite:///")).parent
    other_project = ProjectService(database, FileService(workspace_path)).create(
        {"name": "另一个项目"}
    )
    now = utc_now()
    with database.engine.begin() as connection:
        connection.execute(
            insert(asset).values(
                id="asset-scene-no-alias",
                project_id=project["id"],
                asset_type="scene",
                name="出租车内",
                profile_json=json_dumps({}),
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(asset).values(
                id="asset-scene-other-project",
                project_id=other_project["id"],
                asset_type="scene",
                name="别的项目场景",
                profile_json=json_dumps({}),
                created_at=now,
                updated_at=now,
            )
        )

    names = service._scene_asset_names(project["id"])

    assert names == [{"name": "出租车内", "aliases": []}]
