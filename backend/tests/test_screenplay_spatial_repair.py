from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.db import Database
from app.schemas import ScreenplayOutput
from app.services.chapter_service import ChapterService
from app.services.errors import ServiceError
from app.services.file_service import FileService
from app.services.project_service import ProjectService
from app.services.writing_service import WritingService, screenplay_spatial_issues


def _conflict_screenplay_content() -> dict[str, Any]:
    """生成一个包含客厅与厨房空间冲突的剧本结构。"""
    return {
        "title": "测试剧本",
        "logline": "一句话梗概",
        "scenes": [
            {
                "scene_no": 1,
                "scene_name": "苏棠公寓厨房",
                "time_weather": "傍晚 / 阴",
                "characters": ["苏棠"],
                "objective": "准备晚餐并去客厅",
                "action": "苏棠在厨房里炒菜装盘，端着菜盘走进客厅放在茶几上。",
                "dialogue": [],
                "narration": [],
                "emotion": "平静",
                "transition_to_next": "坐在沙发上休息",
                "source_refs": ["P001"],
            }
        ],
    }


def _repaired_screenplay_content() -> dict[str, Any]:
    """模拟修复后拆分为两个合规视觉空间的剧本结构。"""
    return {
        "title": "测试剧本",
        "logline": "一句话梗概",
        "scenes": [
            {
                "scene_no": 1,
                "scene_name": "苏棠公寓厨房",
                "time_weather": "傍晚 / 阴",
                "characters": ["苏棠"],
                "objective": "准备晚餐",
                "action": "苏棠在厨房里炒菜装盘，关掉炉火。",
                "dialogue": [],
                "narration": [],
                "emotion": "平静",
                "transition_to_next": "端着菜盘转身离开厨房走向客厅",
                "source_refs": ["P001"],
            },
            {
                "scene_no": 2,
                "scene_name": "苏棠公寓客厅",
                "time_weather": "傍晚 / 阴",
                "characters": ["苏棠"],
                "objective": "将菜放在茶几上",
                "action": "苏棠来到客厅，将菜盘平稳放在茶几上，坐在沙发上。",
                "dialogue": [],
                "narration": [],
                "emotion": "放松",
                "transition_to_next": "低头准备用餐",
                "source_refs": ["P001"],
            },
        ],
    }


class MockRepairTextProvider:
    def __init__(self, repair_success: bool = True) -> None:
        self.calls: list[dict[str, Any]] = []
        self.repair_success = repair_success

    async def generate_json(self, **values: Any) -> Any:
        self.calls.append(values)
        if self.repair_success:
            return ScreenplayOutput.model_validate(_repaired_screenplay_content())
        return ScreenplayOutput.model_validate(_conflict_screenplay_content())


@pytest.fixture
def writing_env(tmp_path: Path):
    database = Database(f"sqlite:///{(tmp_path / 'spatial.db').as_posix()}")
    database.initialize(seed_mock_providers=True)
    project = ProjectService(database, FileService(tmp_path)).create({"name": "空间测试项目"})
    chapter = ChapterService(database, FileService(tmp_path)).create(
        project["id"],
        {"title": "第一章", "source_text": "苏棠在厨房里做饭，然后端到客厅。"},
    )
    service = WritingService(database)
    yield database, service, project, chapter
    database.dispose()


def test_screenplay_spatial_issues_detection():
    """验证空间问题检测能够精准发现同场内的多空间冲突。"""
    conflict_content = _conflict_screenplay_content()
    issues = screenplay_spatial_issues(conflict_content)
    assert len(issues) == 1
    assert "同时出现“客厅”与“厨房”" in issues[0]
    assert "场次 1「苏棠公寓厨房」疑似跨越多个视觉空间" in issues[0]

    repaired_content = _repaired_screenplay_content()
    no_issues = screenplay_spatial_issues(repaired_content)
    assert len(no_issues) == 0


def test_update_screenplay_auto_repairs_spatial_issues_on_confirm(writing_env, monkeypatch):
    """验证确认剧本时如果存在空间冲突，系统自动调用文字模型修复并完成确认。"""
    database, service, project, chapter = writing_env
    mock_provider = MockRepairTextProvider(repair_success=True)
    monkeypatch.setattr(
        service,
        "_provider",
        lambda *args, **kwargs: (mock_provider, {"id": "mock-text", "params": {}}),
    )

    conflict_content = _conflict_screenplay_content()
    # 先保存为 ready
    saved_ready = service.update_screenplay(
        chapter["id"], {"content": conflict_content, "status": "ready"}
    )
    assert saved_ready["status"] == "ready"

    # 点击确认：触发自动修复
    saved_confirmed = service.update_screenplay(
        chapter["id"],
        {"content": conflict_content, "status": "confirmed"},
    )
    assert saved_confirmed["status"] == "confirmed"
    assert len(mock_provider.calls) == 1
    # 确认后的剧本已被拆分成两个场次
    assert len(saved_confirmed["content"]["scenes"]) == 2
    assert saved_confirmed["content"]["scenes"][0]["scene_name"] == "苏棠公寓厨房"
    assert saved_confirmed["content"]["scenes"][1]["scene_name"] == "苏棠公寓客厅"


def test_update_screenplay_fails_when_spatial_repair_unsuccessful(writing_env, monkeypatch):
    """验证当文字模型未能解决空间冲突时，如期抛出不能确认的错误。"""
    database, service, project, chapter = writing_env
    mock_provider = MockRepairTextProvider(repair_success=False)
    monkeypatch.setattr(
        service,
        "_provider",
        lambda *args, **kwargs: (mock_provider, {"id": "mock-text", "params": {}}),
    )

    conflict_content = _conflict_screenplay_content()
    with pytest.raises(ServiceError) as exc_info:
        service.update_screenplay(
            chapter["id"],
            {"content": conflict_content, "status": "confirmed"},
        )
    assert "剧本不能确认" in str(exc_info.value.detail)
    assert "同时出现“客厅”与“厨房”" in str(exc_info.value.detail)


def test_handle_screenplay_auto_repairs_spatial_issues(writing_env, monkeypatch):
    """验证初次生成剧本时若模型输出存在空间问题，Job 内部自动触发修复重构。"""
    import asyncio
    database, service, project, chapter = writing_env

    # 模拟文字 Provider：第一次生成有冲突，第二次修复生成无冲突
    class StepwiseTextProvider:
        def __init__(self) -> None:
            self.step = 0

        async def generate_json(self, **values: Any) -> Any:
            self.step += 1
            if values["schema"] is ScreenplayOutput:
                if self.step == 1:
                    return ScreenplayOutput.model_validate(_conflict_screenplay_content())
                return ScreenplayOutput.model_validate(_repaired_screenplay_content())
            from app.schemas import NovelAnalysisOutput
            if values["schema"] is NovelAnalysisOutput:
                return NovelAnalysisOutput.model_validate({
                    "summary": "概括",
                    "genre": "都市",
                    "time_period": "现代",
                    "locations": ["公寓"],
                    "worldview": "现实",
                    "main_characters": ["苏棠"],
                    "character_relations": [],
                    "core_conflict": "生活",
                    "plot_structure": {"beginning": "起", "middle": "承", "end": "合"},
                    "emotion_curve": [],
                    "foreshadowing": [],
                    "visualizable_content": [],
                    "hard_to_visualize": [],
                    "production_risks": [],
                })
            raise AssertionError(f"未预期的 Schema: {values['schema']}")

    provider = StepwiseTextProvider()
    monkeypatch.setattr(
        service,
        "_provider",
        lambda *args, **kwargs: (provider, {"id": "mock-text", "params": {}}),
    )

    queued = service.queue_screenplay(chapter["id"], {})
    job_record = service.jobs.repository.require(queued["id"])
    result = asyncio.run(service.handle_screenplay(job_record))
    assert "content" in result
    # 验证修复后包含 2 个场次，没有空间警告
    assert len(result["content"]["scenes"]) == 2
    assert not any("疑似跨越多个视觉空间" in w for w in result["warnings"])
