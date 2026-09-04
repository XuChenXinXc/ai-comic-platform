from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import update

from app.db import Database
from app.models.tables import screenplay, shot
from app.schemas import MAX_SHOT_DURATION_SEC
from app.services.asset_service import AssetService
from app.services.chapter_service import ChapterService
from app.services.errors import ServiceError
from app.services.file_service import FileService
from app.services.project_service import ProjectService
from app.services.shot_service import ShotService
from app.services.utils import json_dumps
from app.services.writing_service import WritingService


class RecordingJobService:
    def __init__(self) -> None:
        self.created: dict[str, Any] = {}

    def create(self, **values: Any) -> dict[str, Any]:
        self.created = values
        return {"id": "job-recorded", **values}


class ExtractionOutput:
    def __init__(self, shots: list[dict[str, Any]]) -> None:
        self.shots = shots

    def model_dump(self, **_kwargs: Any) -> dict[str, Any]:
        return {"shots": self.shots}


def _screenplay_content() -> dict[str, Any]:
    return {
        "title": "下山",
        "logline": "少年决定离开师门。",
        "scenes": [
            {
                "scene_no": 1,
                "scene_name": "青云山巅",
                "time_weather": "清晨，晴",
                "characters": ["少年"],
                "objective": "做出下山决定",
                "action": "少年在山巅远望后转身。",
                "dialogue": [],
                "narration": [],
                "emotion": "坚定",
                "transition_to_next": "切到山门。",
            }
        ],
    }


def _extracted_shot(scene_asset_id: str) -> dict[str, Any]:
    return {
        "shot_no": 1,
        "scene_no": 1,
        "source_ref": "决心下山",
        "scene_name": "青云山巅",
        "scene_asset_id": scene_asset_id,
        "character_names": [],
        "character_asset_ids": [],
        "prop_names": [],
        "prop_asset_ids": [],
        "positions": [],
        "start_state": "少年站在山巅边缘。",
        "action": "少年转身。",
        "expressions": [],
        "dialogue": [],
        "narration": "",
        "shot_type": "中景",
        "camera_angle": "平视",
        "camera_move": "固定",
        "duration_sec": 5,
        "end_state": "少年面向下山方向。",
        "next_hook": "准备迈步。",
        "frame_description": "少年站在山巅。",
        "image_prompt": "少年转身前的稳定首帧。",
        "video_prompt": "0-3秒：少年转身并停稳。",
        "negative_prompt": "身份漂移，场景变化。",
    }


@pytest.fixture
def storyboard_services(
    tmp_path: Path,
) -> tuple[Database, ShotService, RecordingJobService, dict[str, Any]]:
    database = Database(f"sqlite:///{(tmp_path / 'storyboard.db').as_posix()}")
    database.initialize(seed_mock_providers=True)
    project = ProjectService(database, FileService(tmp_path)).create({"name": "分镜流水线"})
    chapter = ChapterService(database, FileService(tmp_path)).create(
        project["id"],
        {"title": "第一章", "source_text": "少年在青云山巅决定下山。"},
    )
    WritingService(database).update_screenplay(
        chapter["id"],
        {"content": _screenplay_content()},
    )
    jobs = RecordingJobService()
    service = ShotService(database, jobs=jobs)  # type: ignore[arg-type]
    yield database, service, jobs, chapter
    database.dispose()


def test_extract_queue_with_legacy_novel_source_uses_screenplay(
    storyboard_services: tuple[Database, ShotService, RecordingJobService, dict[str, Any]],
) -> None:
    database, service, jobs, chapter = storyboard_services
    AssetService(database).create(
        chapter["project_id"],
        {"asset_type": "scene", "name": "青云山巅"},
    )

    service.queue_extract(
        chapter["id"],
        {"source_type": "novel", "scope": "all"},
    )

    assert jobs.created["input_data"]["source_type"] == "screenplay"


def test_extract_queue_without_scene_assets_returns_conflict(
    storyboard_services: tuple[Database, ShotService, RecordingJobService, dict[str, Any]],
) -> None:
    _database, service, _jobs, chapter = storyboard_services

    with pytest.raises(ServiceError) as exc_info:
        service.queue_extract(
            chapter["id"],
            {"source_type": "screenplay", "scope": "all"},
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "请先提取场景素材，再提取分镜"


def test_extract_shots_with_catalog_asset_id_binds_scene(
    storyboard_services: tuple[Database, ShotService, RecordingJobService, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, service, _jobs, chapter = storyboard_services
    scene = AssetService(database).create(
        chapter["project_id"],
        {
            "asset_type": "scene",
            "name": "青云山巅",
            "profile": {"concept": "云海环绕的山巅平台"},
        },
    )
    captured: dict[str, Any] = {}

    class CatalogProvider:
        async def generate_json(self, **values: Any) -> ExtractionOutput:
            captured.update(values)
            return ExtractionOutput([_extracted_shot(scene["id"])])

    monkeypatch.setattr(
        service.providers,
        "resolve",
        lambda **_kwargs: (
            CatalogProvider(),
            {"id": "mock-text", "params": {}},
        ),
    )
    job = {
        "project_id": chapter["project_id"],
        "chapter_id": chapter["id"],
        "provider_id": "mock-text",
        "input_json": json_dumps(
            {
                "source_type": "screenplay",
                "scope": "all",
                "scene_no": None,
                "params": {},
            }
        ),
    }

    asyncio.run(service.handle_extract(job))

    shots = service.list(chapter["id"])
    assert scene["id"] in captured["user_prompt"]
    assert "青云山巅" in captured["user_prompt"]
    assert shots[0]["scene_asset_id"] == scene["id"]
    assert shots[0]["source_type"] == "screenplay"
    assert shots[0]["duration_sec"] == 5


def test_extract_shots_uses_one_full_chapter_request_for_multiple_scenes(
    storyboard_services: tuple[Database, ShotService, RecordingJobService, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, service, _jobs, chapter = storyboard_services
    scene = AssetService(database).create(
        chapter["project_id"],
        {
            "asset_type": "scene",
            "name": "青云山巅",
            "profile": {"concept": "云海环绕的山巅平台"},
        },
    )
    content = _screenplay_content()
    content["scenes"] = [
        {**content["scenes"][0], "scene_no": 1},
        {**content["scenes"][0], "scene_no": 2, "scene_name": "青云山巅"},
    ]
    WritingService(database).update_screenplay(chapter["id"], {"content": content})
    captured: list[str] = []

    class FullChapterProvider:
        async def generate_json(self, **values: Any) -> ExtractionOutput:
            captured.append(str(values["user_prompt"]))
            first = _extracted_shot(scene["id"])
            second = _extracted_shot(scene["id"])
            second["shot_no"] = 2
            second["scene_no"] = 2
            second["scene_name"] = "青云山巅"
            return ExtractionOutput([first, second])

    monkeypatch.setattr(
        service.providers,
        "resolve",
        lambda **_kwargs: (
            FullChapterProvider(),
            {"id": "mock-text", "params": {}},
        ),
    )
    job = {
        "project_id": chapter["project_id"],
        "chapter_id": chapter["id"],
        "provider_id": "mock-text",
        "input_json": json_dumps(
            {
                "source_type": "screenplay",
                "scope": "all",
                "scene_no": None,
                "params": {},
            }
        ),
    }

    result = asyncio.run(service.handle_extract(job))
    shots = service.list(chapter["id"])

    assert len(captured) == 1
    assert "范围：全部剧本场次" in captured[0]
    assert "来源内容" in captured[0]
    # 整章模式必须携带每场分镜数量预算表（镜头密度治理的预测闸门）
    assert "每场分镜数量预算表" in captured[0]
    assert len(shots) == 2
    assert {item["scene_no"] for item in shots} == {1, 2}
    assert result["count"] == 2


def test_extract_retries_with_merge_instruction_when_scene_density_rejected(
    storyboard_services: tuple[Database, ShotService, RecordingJobService, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """场次镜头密度超限时：把合并指令回注进下一轮生成并重试，而不是直接失败。

    回归 2026-09 缺陷：模型整章输出过密（如场次 1 返回 6 镜、超 45 秒预算），
    自动合并兜底也压不回上限，任务直接失败。修复后应带着「同场相邻的连续动作
    与对话回合合并进同一镜头」的修正指令重新生成一次。
    """
    database, service, _jobs, chapter = storyboard_services
    scene = AssetService(database).create(
        chapter["project_id"],
        {
            "asset_type": "scene",
            "name": "青云山巅",
            "profile": {"concept": "云海环绕的山巅平台"},
        },
    )
    captured: list[str] = []
    call_count = {"n": 0}

    class DensityFlakyProvider:
        async def generate_json(self, **values: Any) -> ExtractionOutput:
            captured.append(str(values["user_prompt"]))
            call_count["n"] += 1
            # 场次 1 无时长字段：预算按文本量兜底为 15 秒 → 硬上限 3 镜。
            # 第一轮返回 5 镜触发密度拒绝，第二轮返回 3 镜保存成功。
            count = 5 if call_count["n"] == 1 else 3
            shots = []
            for index in range(count):
                item = _extracted_shot(scene["id"])
                item["shot_no"] = index + 1
                item["scene_no"] = 1
                # 全部用转场/省略边界，确保自动合并兜底无法压回上限，
                # 覆盖「只有重新生成才能修正」的路径。
                item["transition_from_prev"] = "elliptical" if index else "scene_change"
                shots.append(item)
            return ExtractionOutput(shots)

    monkeypatch.setattr(
        service.providers,
        "resolve",
        lambda **_kwargs: (
            DensityFlakyProvider(),
            {"id": "mock-text", "params": {}},
        ),
    )
    job = {
        "project_id": chapter["project_id"],
        "chapter_id": chapter["id"],
        "provider_id": "mock-text",
        "input_json": json_dumps(
            {
                "source_type": "screenplay",
                "scope": "all",
                "scene_no": None,
                "params": {},
            }
        ),
    }

    result = asyncio.run(service.handle_extract(job))

    assert call_count["n"] == 2
    assert "分镜密度修正指令" in captured[1]
    assert "合并进同一镜头" in captured[1]
    assert "只保留独立结果与转场" in captured[1]
    assert result["count"] == 3
    assert len(service.list(chapter["id"])) == 3


def test_scene_density_rejects_excessive_shots_before_save() -> None:
    # 30 秒场次：下限 ceil(30/15)=2 镜，硬上限 2+2=4 镜
    scenes = [{"scene_no": 1, "duration_sec": 30}]
    shots = [{"scene_no": 1} for _ in range(5)]

    with pytest.raises(ServiceError, match="超过本场 30 秒预算允许的 4 个"):
        ShotService._validate_scene_shot_density(scenes, shots)


def test_scene_density_allows_valid_complex_scene() -> None:
    scenes = [{"scene_no": 1, "duration_sec": 30}]
    shots = [{"scene_no": 1} for _ in range(4)]

    ShotService._validate_scene_shot_density(scenes, shots)


@pytest.mark.parametrize(
    ("budget", "expected_lower", "expected_upper"),
    [
        (15, 1, 3),  # 整数倍边界
        (30, 2, 4),
        (60, 4, 6),
        (61, 5, 7),  # 非整数倍：曾被 floor 少算 1 镜
        (70, 5, 7),  # 用户实际报错场景
        (76, 6, 8),
    ],
)
def test_shot_count_limits_lower_bound_covers_budget_with_max_shot_length(
    budget: int, expected_lower: int, expected_upper: int
) -> None:
    """下限必须 >= ceil(budget/15)：否则上限会低于覆盖该预算所需的最少镜头数。"""
    lower, upper = ShotService._shot_count_limits(budget)

    assert (lower, upper) == (expected_lower, expected_upper)
    # 单镜最长 15 秒，lower 镜必须真的能覆盖 budget 秒
    assert lower * MAX_SHOT_DURATION_SEC >= budget


def test_scene_density_allows_ceil_based_shot_count_for_seventy_second_scene() -> None:
    """70 秒场次至少需 ceil(70/15)=5 镜，硬上限 7；6 和 7 镜都必须放行。

    回归 2026-08 缺陷：_shot_count_limits 曾用 floor(70/15)=4 得出上限 6，
    与 shot_extract.md 第 7.2 条发给模型的 ceil 公式（上限 7）不一致，
    模型按 7 镜输出却被门禁拒绝。
    """
    scenes = [{"scene_no": 2, "duration_sec": 70}]

    for count in (5, 6, 7):
        ShotService._validate_scene_shot_density(
            scenes, [{"scene_no": 2} for _ in range(count)]
        )

    with pytest.raises(ServiceError, match="超过本场 70 秒预算允许的 7 个"):
        ShotService._validate_scene_shot_density(
            scenes, [{"scene_no": 2} for _ in range(8)]
        )


def test_scene_density_uses_estimated_budget_when_script_lacks_duration() -> None:
    """旧剧本没有时长字段时按文本量估算预算，密度上限随之收紧而不是固定 8 镜。"""
    scene = {
        "scene_no": 1,
        "dialogue": [{"speaker": "甲", "text": "这句台词" * 15}],  # 60 字 ≈ 24 秒
        "narration": [],
        "action": "他走进房间。他坐下。",
    }
    budget = ShotService._scene_budget_sec(scene)
    assert budget >= 25
    _, upper = ShotService._shot_count_limits(budget)
    assert upper <= 4
    shots = [{"scene_no": 1} for _ in range(upper + 1)]

    with pytest.raises(ServiceError, match="预算"):
        ShotService._validate_scene_shot_density([scene], shots)


def test_auto_merge_combines_adjacent_continuous_shots() -> None:
    first = {
        "shot_no": 1,
        "scene_no": 1,
        "duration_sec": 6,
        "transition_from_prev": "scene_change",
        "action": "林舟低头查看照片",
        "start_state": "站在屋檐下",
        "end_state": "仍站在屋檐下看着照片",
        "frame_description": "屋檐下的青年低头看旧照片",
        "image_prompt": "近景，青年低头看照片",
        "video_prompt": "0-3秒：近景，林舟掏出照片。3-6秒：特写照片中的钟表店。",
        "narration": "",
        "positions": [{"character": "林舟", "position": "画面中央"}],
        "expressions": [{"character": "林舟", "expression": "凝重"}],
        "character_names": ["林舟"],
        "dialogue": [],
    }
    second = {
        "shot_no": 2,
        "scene_no": 1,
        "duration_sec": 7,
        "transition_from_prev": "continuous",
        "action": "林舟抬头接过红伞",
        "start_state": "仍低头看照片",
        "end_state": "红伞已撑在头顶",
        "next_hook": "两人共伞走入雨巷",
        "frame_description": "一把红伞从旁边伸过来",
        "image_prompt": "近景，红伞伸入画面",
        "video_prompt": "0-2秒：红伞伸入画面。2-7秒：林舟伸手接伞并抬起头。",
        "narration": "",
        "positions": [
            {"character": "林舟", "position": "画面左侧"},
            {"character": "苏晴", "position": "画面右侧"},
        ],
        "expressions": [{"character": "林舟", "expression": "惊讶"}],
        "character_names": ["林舟", "苏晴"],
        "dialogue": [{"speaker": "苏晴", "text": "一起走吧"}],
    }

    merged, notes = ShotService._auto_merge_dense_shots([first, dict(second)])

    assert len(merged) == 1
    assert len(notes) == 1
    merged_shot = merged[0]
    assert merged_shot["duration_sec"] == 13
    assert "随后" in merged_shot["action"]
    assert merged_shot["end_state"] == "红伞已撑在头顶"
    assert merged_shot["next_hook"] == "两人共伞走入雨巷"
    # 后段视频时间轴整体平移 6 秒，与前段衔接成单套连续时间轴
    assert "0-3秒" in merged_shot["video_prompt"]
    assert "6-8秒" in merged_shot["video_prompt"]
    assert "11-16秒" not in merged_shot["video_prompt"]
    # 台词按序保留不丢失
    assert merged_shot["dialogue"] == [{"speaker": "苏晴", "text": "一起走吧"}]
    # 站位同角色取后镜状态，新增角色保留
    positions = {item["character"]: item for item in merged_shot["positions"]}
    assert positions["林舟"]["position"] == "画面左侧"
    assert positions["苏晴"]["position"] == "画面右侧"


def test_auto_merge_skips_non_continuous_transitions_and_capacity_overflow() -> None:
    # 无场景预算表时只合并 continuous；same_scene_cut 是独立机位对切、
    # elliptical 有时间跳跃，均不参与合并。
    shots = [
        {"scene_no": 1, "duration_sec": 6, "transition_from_prev": "scene_change"},
        {"scene_no": 1, "duration_sec": 6, "transition_from_prev": "same_scene_cut"},
        {"scene_no": 1, "duration_sec": 6, "transition_from_prev": "elliptical"},
    ]

    merged, notes = ShotService._auto_merge_dense_shots(shots)
    assert len(merged) == 3
    assert notes == []

    # 合并后总时长超过单镜上限时压到 15 秒；跨场的 continuous 不合并
    overflow = [
        {"scene_no": 1, "duration_sec": 9, "transition_from_prev": "scene_change", "dialogue": []},
        {"scene_no": 1, "duration_sec": 9, "transition_from_prev": "continuous", "dialogue": []},
        {"scene_no": 1, "duration_sec": 6, "transition_from_prev": "scene_change", "dialogue": []},
        {
            "scene_no": 2,
            "duration_sec": 6,
            "transition_from_prev": "continuous",
            "dialogue": [],
        },
    ]
    merged, notes = ShotService._auto_merge_dense_shots(overflow)
    assert len(merged) == 3
    assert len(notes) == 1
    assert merged[0]["duration_sec"] == 15
    assert [item["scene_no"] for item in merged] == [1, 1, 2]


def _dense_shot(
    no: int,
    *,
    transition: str,
    duration: float = 8,
    dialogue: list[dict[str, str]] | None = None,
    action: str = "",
) -> dict[str, Any]:
    return {
        "shot_no": no,
        "scene_no": 1,
        "duration_sec": duration,
        "transition_from_prev": transition,
        "action": action or f"动作{no}",
        "start_state": f"状态{no}起点",
        "end_state": f"状态{no}结果",
        "video_prompt": f"0-{duration:g}秒：动作{no}。",
        "dialogue": dialogue or [],
    }


def test_auto_merge_merges_dialogue_rounds_when_scene_over_budget() -> None:
    # 30 秒场次：下限 ceil(30/15)=2，硬上限 4。模型返回 5 镜时，
    # 同场相邻的对话回合（same_scene_cut）应合并进同一镜头，压回 4 镜以内。
    # 用 30 秒而非 40 秒：40 秒在 ceil 公式下上限为 5，5 镜已合法、不触发合并。
    scenes = [{"scene_no": 1, "duration_sec": 30}]
    shots = [
        _dense_shot(1, transition="scene_change", dialogue=[{"speaker": "甲", "text": "字" * 20}]),
        _dense_shot(
            2, transition="same_scene_cut", dialogue=[{"speaker": "乙", "text": "字" * 20}]
        ),
        _dense_shot(3, transition="same_scene_cut", dialogue=[{"speaker": "丙", "text": "刚到。"}]),
        _dense_shot(4, transition="same_scene_cut"),
        _dense_shot(5, transition="same_scene_cut"),
    ]

    merged, notes = ShotService._auto_merge_dense_shots(shots, scenes=scenes)

    assert len(merged) == 4
    assert len(notes) == 1
    # pair(1,2) 压缩后 40 字超出 15 秒承载（纯对白 37 字）不合并；
    # 对话回合 pair(2,3) 合并进同一镜头，台词按序保留
    assert [item["speaker"] for shot in merged for item in shot["dialogue"]] == ["甲", "乙", "丙"]
    # 合并后的镜头 16 秒压到 15 秒单镜上限，两段动作时间轴都保留
    assert merged[1]["duration_sec"] == 15
    assert "动作2" in merged[1]["video_prompt"] and "动作3" in merged[1]["video_prompt"]
    assert all(5 <= item["duration_sec"] <= 15 for item in merged)


def test_auto_merge_stops_when_scene_within_budget() -> None:
    # 场次已在上限内时不再追加合并 same_scene_cut，保留模型设计的分镜粒度
    scenes = [{"scene_no": 1, "duration_sec": 40}]
    shots = [
        _dense_shot(1, transition="scene_change"),
        _dense_shot(2, transition="same_scene_cut"),
        _dense_shot(3, transition="same_scene_cut"),
        _dense_shot(4, transition="same_scene_cut"),
    ]

    merged, notes = ShotService._auto_merge_dense_shots(shots, scenes=scenes)

    assert len(merged) == 4
    assert notes == []


def test_auto_merge_keeps_transition_boundaries_with_budget() -> None:
    # scene_change 与 elliptical 是转场/省略边界，即使场次超限也不合并
    scenes = [{"scene_no": 1, "duration_sec": 15}]
    shots = [
        _dense_shot(1, transition="scene_change"),
        _dense_shot(2, transition="elliptical"),
        _dense_shot(3, transition="elliptical"),
        _dense_shot(4, transition="elliptical"),
    ]

    merged, notes = ShotService._auto_merge_dense_shots(shots, scenes=scenes)

    assert len(merged) == 4
    assert notes == []


def test_auto_merge_skips_pair_when_compressed_capacity_insufficient() -> None:
    # 压缩到 15 秒后对白仍超出承载能力（纯对白 2.5 字/秒 → 37 字）不合并该对
    # 预算取 30 秒（上限 4）以确保 5 镜确实超限，从而进入合并路径。
    scenes = [{"scene_no": 1, "duration_sec": 30}]
    shots = [
        _dense_shot(1, transition="scene_change", dialogue=[{"speaker": "甲", "text": "字" * 20}]),
        _dense_shot(2, transition="continuous", dialogue=[{"speaker": "乙", "text": "字" * 20}]),
        _dense_shot(3, transition="same_scene_cut"),
        _dense_shot(4, transition="same_scene_cut"),
        _dense_shot(5, transition="same_scene_cut"),
    ]

    merged, _notes = ShotService._auto_merge_dense_shots(shots, scenes=scenes)

    # pair(1,2) 压缩后 40 字 > 37 字承载不合并；pair(2,3) 台词 20 字可合并压回 4 镜
    assert len(merged) == 4
    assert [len(item["dialogue"]) for item in merged] == [1, 1, 0, 0]


def test_extracted_continuity_rejects_scene_interleaving() -> None:
    shots = [
        {"scene_no": 1, "transition_from_prev": "scene_change"},
        {"scene_no": 2, "transition_from_prev": "scene_change"},
        {"scene_no": 1, "transition_from_prev": "scene_change"},
    ]

    with pytest.raises(ServiceError, match="场次编号回跳或交叉"):
        ShotService._validate_extracted_continuity(shots)


def test_extracted_continuity_downgrades_first_shot_continuous() -> None:
    """首镜标 continuous 是模型常见小偏差：自动降级为 scene_change 并警告。"""
    shots = [
        {"scene_no": 1, "transition_from_prev": "continuous", "start_state": "站立"},
    ]

    warnings = ShotService._validate_extracted_continuity(shots)

    assert shots[0]["transition_from_prev"] == "scene_change"
    assert len(warnings) == 1
    assert "continuous" in warnings[0]


def test_extracted_continuity_downgrades_continuous_missing_state() -> None:
    """continuous 缺少前镜 end_state 或本镜 start_state 时降级为 scene_change。"""
    shots = [
        {
            "scene_no": 1,
            "transition_from_prev": "scene_change",
            "start_state": "站立",
            "end_state": "向前走",
        },
        {
            "scene_no": 1,
            "transition_from_prev": "continuous",
            "start_state": "",
            "end_state": "停下",
        },
    ]

    warnings = ShotService._validate_extracted_continuity(shots)

    assert shots[1]["transition_from_prev"] == "scene_change"
    assert len(warnings) == 1


def test_extracted_continuity_keeps_valid_continuous() -> None:
    """有完整衔接状态的 continuous 保持不变，不产生警告。"""
    shots = [
        {
            "scene_no": 1,
            "transition_from_prev": "scene_change",
            "start_state": "站立",
            "end_state": "向前走",
        },
        {
            "scene_no": 1,
            "transition_from_prev": "continuous",
            "start_state": "继续向前走",
            "end_state": "停在门口",
        },
    ]

    warnings = ShotService._validate_extracted_continuity(shots)

    assert warnings == []
    assert shots[1]["transition_from_prev"] == "continuous"


def test_dialogue_fallback_distributes_lines_across_existing_shots() -> None:
    source_scene = {
        "dialogue": [
            {"speaker": "甲", "text": "第一句"},
            {"speaker": "乙", "text": "第二句"},
            {"speaker": "甲", "text": "第三句"},
            {"speaker": "乙", "text": "第四句"},
        ]
    }
    shots = [{"dialogue": []}, {"dialogue": []}]

    repaired = ShotService._synchronize_dialogue_with_source(
        source_scene, shots, allow_missing=True
    )

    assert repaired is True
    assert [line["text"] for line in shots[0]["dialogue"]] == ["第一句", "第二句"]
    assert [line["text"] for line in shots[1]["dialogue"]] == ["第三句", "第四句"]


def test_extract_shots_preserves_screenplay_dialogue_verbatim_and_in_order(
    storyboard_services: tuple[Database, ShotService, RecordingJobService, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, service, _jobs, chapter = storyboard_services
    scene = AssetService(database).create(
        chapter["project_id"],
        {"asset_type": "scene", "name": "青云山巅"},
    )
    content = _screenplay_content()
    content["scenes"][0]["dialogue"] = [
        {"speaker": "少年", "text": "我已经决定下山。"},
        {"speaker": "师父", "text": "你想清楚了吗？"},
        {"speaker": "少年", "text": "我已经想清楚了。"},
    ]
    WritingService(database).update_screenplay(chapter["id"], {"content": content})
    first = _extracted_shot(scene["id"])
    first["dialogue"] = [{"speaker": "少年", "text": "我决定下山。"}]
    second = _extracted_shot(scene["id"])
    second["shot_no"] = 2
    second["dialogue"] = [{"speaker": "师父", "text": "想清楚了吗？"}]
    third = _extracted_shot(scene["id"])
    third["shot_no"] = 3
    third["dialogue"] = [{"speaker": "少年", "text": "我已经想清楚了。"}]

    class DialogueProvider:
        async def generate_json(self, **_values: Any) -> ExtractionOutput:
            return ExtractionOutput([first, second, third])

    monkeypatch.setattr(
        service.providers,
        "resolve",
        lambda **_kwargs: (
            DialogueProvider(),
            {"id": "mock-text", "params": {}},
        ),
    )
    job = {
        "project_id": chapter["project_id"],
        "chapter_id": chapter["id"],
        "provider_id": "mock-text",
        "input_json": json_dumps(
            {
                "source_type": "screenplay",
                "scope": "all",
                "scene_no": None,
                "params": {},
            }
        ),
    }

    asyncio.run(service.handle_extract(job))

    saved = service.list(chapter["id"])
    assert [line for item in saved for line in item["dialogue"]] == content["scenes"][0]["dialogue"]


def test_extract_shots_rejects_when_model_drops_all_screenplay_dialogue() -> None:
    source_scene = {"dialogue": [{"speaker": "少年", "text": "我已经决定下山。"}]}

    with pytest.raises(ServiceError, match="未返回本场对白"):
        ShotService._synchronize_dialogue_with_source(
            source_scene,
            [_extracted_shot("scene-1")],
        )


def test_extract_shots_rejects_when_model_leaves_trailing_dialogue_unassigned() -> None:
    source_scene = {
        "dialogue": [
            {"speaker": "少年", "text": "我已经决定下山。"},
            {"speaker": "师父", "text": "你想清楚了吗？"},
        ]
    }
    first = _extracted_shot("scene-1")
    first["dialogue"] = [source_scene["dialogue"][0]]
    second = _extracted_shot("scene-1")
    second["shot_no"] = 2
    second["dialogue"] = []

    # 严格模式（allow_missing=False）仍拒绝，保留原有契约
    with pytest.raises(ServiceError, match="对白集中到最后一镜"):
        ShotService._synchronize_dialogue_with_source(source_scene, [first, second])


def test_synchronize_dialogue_backfills_trailing_lines_when_missing_allowed() -> None:
    """allow_missing=True 时尾部漏填按镜头顺序补齐，不中止提取。

    回归 2026-08 缺陷：生产路径全部传 allow_missing=True，但该参数原先只作用于
    「模型完全没填对白」，尾部/局部漏填仍无条件 raise。模型只要漏引用一句
    （改写一个标点即可导致匹配停下），整场提取就被中止。
    """
    source_scene = {
        "dialogue": [
            {"speaker": "少年", "text": "我已经决定下山。"},
            {"speaker": "师父", "text": "你想清楚了吗？"},
            {"speaker": "少年", "text": "想清楚了。"},
        ]
    }
    first = _extracted_shot("scene-1")
    first["dialogue"] = [source_scene["dialogue"][0]]
    second = _extracted_shot("scene-1")
    second["shot_no"] = 2
    second["dialogue"] = []

    repaired = ShotService._synchronize_dialogue_with_source(
        source_scene, [first, second], allow_missing=True
    )

    assert repaired is True
    # 一句都不能丢，且顺序与剧本一致
    assert [line["text"] for shot in (first, second) for line in shot["dialogue"]] == [
        item["text"] for item in source_scene["dialogue"]
    ]
    # 已明确归属的第一句留在首镜，剩余两句铺到后续镜头而非全挤首镜
    assert [line["text"] for line in first["dialogue"]] == ["我已经决定下山。"]
    assert len(second["dialogue"]) == 2


def test_synchronize_dialogue_backfill_keeps_all_lines_when_only_one_shot() -> None:
    """只有一个镜头时，未分配的尾部对白兜到该镜，不丢句也不越界。"""
    source_scene = {
        "dialogue": [
            {"speaker": "少年", "text": "第一句。"},
            {"speaker": "师父", "text": "第二句。"},
        ]
    }
    only = _extracted_shot("scene-1")
    only["dialogue"] = [source_scene["dialogue"][0]]

    repaired = ShotService._synchronize_dialogue_with_source(
        source_scene, [only], allow_missing=True
    )

    assert repaired is True
    assert [line["text"] for line in only["dialogue"]] == ["第一句。", "第二句。"]


def test_extract_shots_rejects_dialogue_over_fifteen_second_capacity() -> None:
    shot = _extracted_shot("scene-1")
    shot["duration_sec"] = 10
    shot["action"] = "少年走入房间。"
    shot["dialogue"] = [{"speaker": "少年", "text": "台词" * 28}]

    with pytest.raises(ServiceError, match="必须拆分为至少"):
        ShotService._validate_extracted_dialogue_capacity([shot])


def test_extract_shots_splits_overlong_dialogue_without_losing_text(
    storyboard_services: tuple[Database, ShotService, RecordingJobService, dict[str, Any]],
) -> None:
    shot = _extracted_shot("scene-1")
    shot["duration_sec"] = 10
    shot["action"] = "少年走入房间。"
    shot["dialogue"] = [{"speaker": "少年", "text": "台" * 111}]

    parts = ShotService._split_dialogue_shot(shot)

    assert len(parts) == 5
    assert all(5 <= part["duration_sec"] <= 15 for part in parts)
    assert all(not ShotService._shot_quality_warnings(part) for part in parts)
    assert "".join(item["text"] for part in parts for item in part["dialogue"]) == "台" * 111


def test_extract_shots_saves_split_dialogue_as_multiple_shots(
    storyboard_services: tuple[Database, ShotService, RecordingJobService, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, service, _jobs, chapter = storyboard_services
    scene = AssetService(database).create(
        chapter["project_id"],
        {"asset_type": "scene", "name": "青云山巅"},
    )
    content = _screenplay_content()
    source_text = "台" * 111
    content["scenes"][0]["dialogue"] = [{"speaker": "少年", "text": source_text}]
    WritingService(database).update_screenplay(chapter["id"], {"content": content})
    extracted = _extracted_shot(scene["id"])
    extracted["duration_sec"] = 10
    extracted["action"] = "少年走入房间。"
    extracted["dialogue"] = [{"speaker": "少年", "text": source_text}]

    class OverlongDialogueProvider:
        async def generate_json(self, **_values: Any) -> ExtractionOutput:
            return ExtractionOutput([extracted])

    monkeypatch.setattr(
        service.providers,
        "resolve",
        lambda **_kwargs: (
            OverlongDialogueProvider(),
            {"id": "mock-text", "params": {}},
        ),
    )
    job = {
        "id": "job-extract-overlong-dialogue",
        "project_id": chapter["project_id"],
        "chapter_id": chapter["id"],
        "provider_id": "mock-text",
        "input_json": json_dumps(
            {
                "source_type": "screenplay",
                "scope": "all",
                "scene_no": None,
                "params": {},
            }
        ),
    }

    asyncio.run(service.handle_extract(job))

    saved = service.list(chapter["id"])
    assert len(saved) == 5
    assert all(5 <= item["duration_sec"] <= 15 for item in saved)
    assert "".join(item["text"] for shot in saved for item in shot["dialogue"]) == source_text


def test_quality_warning_does_not_recommend_impossible_seventy_second_shot() -> None:
    warning = ShotService._shot_quality_warnings(
        {
            "dialogue": [{"speaker": "少年", "text": "台" * 111}],
            "duration_sec": 10,
            "action": "少年走入房间。",
            "end_state": "",
            "video_prompt": "",
            "video_prompt_override": "",
        }
    )[0]

    assert "必须拆分为至少5个分镜" in warning
    assert "70秒" not in warning


def test_quality_warning_keeps_twelve_second_recommendation_within_limit() -> None:
    warning = ShotService._shot_quality_warnings(
        {
            "dialogue": [{"speaker": "少年", "text": "台" * 18}],
            "duration_sec": 8,
            "action": "少年走入房间。",
            "end_state": "",
            "video_prompt": "",
            "video_prompt_override": "",
        }
    )[0]

    assert "建议延长至12秒" in warning
    assert "必须拆分" not in warning


def test_quality_fix_splits_existing_overlong_dialogue(
    storyboard_services: tuple[Database, ShotService, RecordingJobService, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _database, service, jobs, chapter = storyboard_services
    current = {
        "id": "shot-overlong",
        "shot_no": 6,
        "revision": 1,
        "duration_sec": 10,
        "action": "少年走入房间。",
        "start_state": "少年站在门外。",
        "end_state": "少年进入房间。",
        "next_hook": "",
        "video_prompt": "0-10秒：少年走入房间。",
        "video_prompt_override": "",
        "dialogue": [{"speaker": "少年", "text": "台" * 111}],
    }
    calls: list[tuple[str, list[dict[str, Any]]]] = []
    monkeypatch.setattr(jobs, "update_progress", lambda *_args, **_kwargs: None, raising=False)
    monkeypatch.setattr(service, "get", lambda _shot_id: current)
    monkeypatch.setattr(service, "_job_cancelled", lambda _job_id: False)
    monkeypatch.setattr(
        service,
        "split",
        lambda shot_id, parts: calls.append((shot_id, parts))
        or [{"id": "shot-overlong"}, {"id": "shot-overlong-2"}],
    )
    job = {
        "id": "job-quality-fix",
        "target_id": chapter["project_id"],
        "provider_id": "mock-text",
        "input_json": json_dumps({"ids": [current["id"]], "params": {}}),
    }

    result = asyncio.run(service.handle_quality_fix(job))

    assert result["fixed"] == 1
    assert len(calls) == 1
    assert len(calls[0][1]) == 5
    assert result["changes"][0]["created_shot_ids"] == [
        "shot-overlong",
        "shot-overlong-2",
    ]


def test_quality_fix_persists_split_shots_for_existing_overlong_dialogue(
    storyboard_services: tuple[Database, ShotService, RecordingJobService, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, service, jobs, chapter = storyboard_services
    scene = AssetService(database).create(
        chapter["project_id"],
        {"asset_type": "scene", "name": "青云山巅"},
    )
    current = service.create(
        chapter["id"],
        {
            "scene_asset_id": scene["id"],
            "action": "少年走入房间。",
            "duration_sec": 10,
        },
    )
    dialogue = [{"speaker": "少年", "text": "台" * 111}]
    with database.engine.begin() as connection:
        connection.execute(
            update(shot)
            .where(shot.c.id == current["id"])
            .values(dialogue_json=json_dumps(dialogue))
        )
    monkeypatch.setattr(jobs, "update_progress", lambda *_args, **_kwargs: None, raising=False)
    monkeypatch.setattr(service, "_job_cancelled", lambda _job_id: False)
    job = {
        "id": "job-quality-fix-real-split",
        "target_id": chapter["project_id"],
        "provider_id": "mock-text",
        "input_json": json_dumps({"ids": [current["id"]], "params": {}}),
    }

    result = asyncio.run(service.handle_quality_fix(job))

    saved = service.list(chapter["id"])
    assert result["fixed"] == 1
    assert len(saved) == 5
    assert all(5 <= item["duration_sec"] <= 15 for item in saved)
    assert "".join(
        line["text"] for shot_item in saved for line in shot_item["dialogue"]
    ) == "台" * 111


def test_extract_shots_legacy_screenplay_restores_structured_dialogue(
    storyboard_services: tuple[Database, ShotService, RecordingJobService, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, service, _jobs, chapter = storyboard_services
    scene = AssetService(database).create(
        chapter["project_id"],
        {"asset_type": "scene", "name": "青云山巅"},
    )
    legacy_content = {
        "title": "旧格式",
        "action": "少年转身。",
        "dialogue": [{"speaker": "少年", "text": "我已经决定下山。"}],
    }
    with database.engine.begin() as connection:
        connection.execute(
            update(screenplay)
            .where(screenplay.c.chapter_id == chapter["id"])
            .values(content_json=json_dumps(legacy_content))
        )
    extracted = _extracted_shot(scene["id"])
    extracted["dialogue"] = [{"speaker": "少年", "text": "我决定下山。"}]

    class LegacyProvider:
        async def generate_json(self, **_values: Any) -> ExtractionOutput:
            return ExtractionOutput([extracted])

    monkeypatch.setattr(
        service.providers,
        "resolve",
        lambda **_kwargs: (
            LegacyProvider(),
            {"id": "mock-text", "params": {}},
        ),
    )
    job = {
        "project_id": chapter["project_id"],
        "chapter_id": chapter["id"],
        "provider_id": "mock-text",
        "input_json": json_dumps(
            {
                "source_type": "screenplay",
                "scope": "all",
                "scene_no": None,
                "params": {},
            }
        ),
    }

    asyncio.run(service.handle_extract(job))

    assert service.list(chapter["id"])[0]["dialogue"] == legacy_content["dialogue"]


def test_extract_shots_with_names_and_aliases_bind_assets(
    storyboard_services: tuple[Database, ShotService, RecordingJobService, dict[str, Any]],
) -> None:
    database, service, _jobs, chapter = storyboard_services
    scene = AssetService(database).create(
        chapter["project_id"],
        {
            "asset_type": "scene",
            "name": "青云山巅",
            "profile": {"aliases": ["山巅平台"]},
        },
    )
    character = AssetService(database).create(
        chapter["project_id"],
        {
            "asset_type": "character",
            "name": "少年",
        },
    )
    prop = AssetService(database).create(
        chapter["project_id"],
        {
            "asset_type": "prop",
            "name": "下山令",
            "profile": {"aliases": ["青铜令牌"]},
        },
    )
    extracted = _extracted_shot("")
    extracted["scene_name"] = "山巅平台"
    extracted["character_names"] = ["少年"]
    extracted["prop_names"] = ["青铜令牌"]

    service._save_extracted(
        chapter,
        "screenplay",
        {"scope": "all", "scene_no": None},
        [extracted],
    )

    saved = service.list(chapter["id"])[0]
    assert saved["scene_asset_id"] == scene["id"]
    assert saved["character_ids"] == [character["id"]]
    assert saved["prop_ids"] == [prop["id"]]


def test_extract_shot_with_primary_name_colliding_with_alias_binds_primary_asset(
    storyboard_services: tuple[Database, ShotService, RecordingJobService, dict[str, Any]],
) -> None:
    database, service, _jobs, chapter = storyboard_services
    scene = AssetService(database).create(
        chapter["project_id"],
        {"asset_type": "scene", "name": "办公室"},
    )
    key_character = AssetService(database).create(
        chapter["project_id"],
        {"asset_type": "character", "name": "小明"},
    )
    AssetService(database).create(
        chapter["project_id"],
        {
            "asset_type": "character",
            "name": "李总",
            "profile": {"aliases": ["小明"]},
        },
    )
    extracted = _extracted_shot(scene["id"])
    extracted["scene_name"] = "办公室"
    extracted["character_names"] = ["小明"]
    extracted["character_asset_ids"] = [key_character["id"]]

    service._save_extracted(
        chapter,
        "screenplay",
        {"scope": "all", "scene_no": None},
        [extracted],
    )

    saved = service.list(chapter["id"])[0]
    assert saved["character_ids"] == [key_character["id"]]


def test_extract_shots_with_key_and_minor_characters_binds_only_key_character(
    storyboard_services: tuple[Database, ShotService, RecordingJobService, dict[str, Any]],
) -> None:
    database, service, _jobs, chapter = storyboard_services
    scene = AssetService(database).create(
        chapter["project_id"],
        {"asset_type": "scene", "name": "办公室"},
    )
    key_character = AssetService(database).create(
        chapter["project_id"],
        {"asset_type": "character", "name": "小明"},
    )
    extracted = _extracted_shot(scene["id"])
    extracted["scene_name"] = "办公室"
    extracted["character_names"] = ["小明", "律师", "前台", "张助理", "女助理"]
    extracted["character_asset_ids"] = [key_character["id"]]

    result = service._save_extracted(
        chapter,
        "screenplay",
        {"scope": "all", "scene_no": None},
        [extracted],
    )

    saved = service.list(chapter["id"])[0]
    assert result["count"] == 1
    assert saved["scene_asset_id"] == scene["id"]
    assert saved["character_ids"] == [key_character["id"]]


def test_extract_shots_with_unmatched_scene_raises_but_unmatched_prop_name_only_warns(
    storyboard_services: tuple[Database, ShotService, RecordingJobService, dict[str, Any]],
) -> None:
    """场景失配仍中止（视频生成必需）；物品纯名字未匹配只降级为警告。

    回归 2026-08 缺陷：模型在 prop_names 里写了一个不在素材库的小物件
    （如「纸条」），原逻辑把物品名失配与场景失配同样当作硬错误，导致整批
    中止；现在物品与角色处理对齐——只有提供了 ID 却解析不到才算错误。
    """
    database, service, _jobs, chapter = storyboard_services
    AssetService(database).create(
        chapter["project_id"],
        {"asset_type": "scene", "name": "办公室"},
    )
    AssetService(database).create(
        chapter["project_id"],
        {"asset_type": "character", "name": "小明"},
    )
    AssetService(database).create(
        chapter["project_id"],
        {"asset_type": "prop", "name": "下山令"},
    )
    extracted = _extracted_shot("")
    # 「天台」与素材库无任何包含/变体关系，是真实不可解析场景
    extracted["scene_name"] = "天台"
    extracted["prop_names"] = ["未知令牌"]

    with pytest.raises(ServiceError) as exc_info:
        service._save_extracted(
            chapter,
            "screenplay",
            {"scope": "all", "scene_no": None},
            [extracted],
        )

    assert "分镜 1" in exc_info.value.detail
    assert "天台" in exc_info.value.detail
    # 物品纯名字未匹配不再出现在硬错误里，而是降级为警告
    assert "未知令牌" not in exc_info.value.detail
    assert service.list(chapter["id"]) == []


def test_resolve_scene_name_matches_suffix_variant(
    storyboard_services: tuple[Database, ShotService, RecordingJobService, dict[str, Any]],
) -> None:
    """方位修饰后缀变体：剧本「公司电梯内」应绑定素材「公司电梯」。"""
    database, service, _jobs, chapter = storyboard_services
    scene = AssetService(database).create(
        chapter["project_id"],
        {"asset_type": "scene", "name": "公司电梯"},
    )
    lookup = service._asset_lookup(chapter["project_id"])["scene"]

    resolved = service._resolve_asset_name(lookup, "公司电梯内")

    assert resolved == scene["id"]


def test_resolve_scene_name_matches_containment_for_merged_asset(
    storyboard_services: tuple[Database, ShotService, RecordingJobService, dict[str, Any]],
) -> None:
    """合并素材：剧本「苏棠家客厅」应绑定合并素材「苏棠家客厅及厨房」。"""
    database, service, _jobs, chapter = storyboard_services
    scene = AssetService(database).create(
        chapter["project_id"],
        {"asset_type": "scene", "name": "苏棠家客厅及厨房"},
    )
    lookup = service._asset_lookup(chapter["project_id"])["scene"]

    assert service._resolve_asset_name(lookup, "苏棠家客厅") == scene["id"]
    assert service._resolve_asset_name(lookup, "苏棠家厨房") == scene["id"]


def test_resolve_scene_name_requires_unique_candidate(
    storyboard_services: tuple[Database, ShotService, RecordingJobService, dict[str, Any]],
) -> None:
    """子串包含有歧义时不绑定：查询名是多个素材的公共子串时不能臆断。"""
    database, service, _jobs, chapter = storyboard_services
    AssetService(database).create(
        chapter["project_id"],
        {"asset_type": "scene", "name": "出租屋客厅"},
    )
    AssetService(database).create(
        chapter["project_id"],
        {"asset_type": "scene", "name": "公司接待客厅"},
    )
    lookup = service._asset_lookup(chapter["project_id"])["scene"]

    assert service._resolve_asset_name(lookup, "客厅") is None


def test_resolve_scene_name_keeps_unrelated_name_unmatched(
    storyboard_services: tuple[Database, ShotService, RecordingJobService, dict[str, Any]],
) -> None:
    """无任何关系的名称（楼道转角 vs 苏棠家客厅及厨房）仍不匹配。"""
    database, service, _jobs, chapter = storyboard_services
    AssetService(database).create(
        chapter["project_id"],
        {"asset_type": "scene", "name": "苏棠家客厅及厨房"},
    )
    lookup = service._asset_lookup(chapter["project_id"])["scene"]

    assert service._resolve_asset_name(lookup, "楼道转角") is None


def test_extract_shots_saves_when_only_prop_name_unmatched(
    storyboard_services: tuple[Database, ShotService, RecordingJobService, dict[str, Any]],
) -> None:
    """场景匹配、物品纯名字未匹配时正常保存，并携带降级警告。"""
    database, service, _jobs, chapter = storyboard_services
    scene = AssetService(database).create(
        chapter["project_id"],
        {"asset_type": "scene", "name": "青云山巅"},
    )
    AssetService(database).create(
        chapter["project_id"],
        {"asset_type": "prop", "name": "下山令"},
    )
    extracted = _extracted_shot(scene["id"])
    extracted["prop_names"] = ["未知令牌"]

    result = service._save_extracted(
        chapter,
        "screenplay",
        {"scope": "all", "scene_no": None},
        [extracted],
    )

    saved = service.list(chapter["id"])
    assert result["count"] == 1
    assert saved[0]["scene_asset_id"] == scene["id"]
    assert saved[0]["prop_ids"] == []
    assert any(
        "未匹配到素材库" in warning["message"] for warning in result["warnings"]
    )


def test_extract_shots_saves_with_warning_when_character_name_unmatched(
    storyboard_services: tuple[Database, ShotService, RecordingJobService, dict[str, Any]],
) -> None:
    """角色纯名字未匹配时正常保存并携带降级警告（与物品处理对齐，不再静默丢弃）。"""
    database, service, _jobs, chapter = storyboard_services
    scene = AssetService(database).create(
        chapter["project_id"],
        {"asset_type": "scene", "name": "青云山巅"},
    )
    extracted = _extracted_shot(scene["id"])
    extracted["character_names"] = ["律师", "前台"]

    result = service._save_extracted(
        chapter,
        "screenplay",
        {"scope": "all", "scene_no": None},
        [extracted],
    )

    saved = service.list(chapter["id"])
    assert result["count"] == 1
    assert saved[0]["character_ids"] == []
    assert any(
        "分镜角色名未匹配到素材库" in warning["message"]
        and "律师" in warning["message"]
        for warning in result["warnings"]
    )


def test_extract_shots_rejects_unresolvable_prop_id_but_allows_missing_name(
    storyboard_services: tuple[Database, ShotService, RecordingJobService, dict[str, Any]],
) -> None:
    """模型提供了物品 ID 却解析不到时仍是硬错误（ID 冲突/编造）。"""
    database, service, _jobs, chapter = storyboard_services
    scene = AssetService(database).create(
        chapter["project_id"],
        {"asset_type": "scene", "name": "青云山巅"},
    )
    AssetService(database).create(
        chapter["project_id"],
        {"asset_type": "prop", "name": "下山令"},
    )
    extracted = _extracted_shot(scene["id"])
    extracted["prop_asset_ids"] = ["asset-prop-nonexistent"]

    with pytest.raises(ServiceError) as exc_info:
        service._save_extracted(
            chapter,
            "screenplay",
            {"scope": "all", "scene_no": None},
            [extracted],
        )

    assert "asset-prop-nonexistent" in exc_info.value.detail
    assert service.list(chapter["id"]) == []


def test_extract_shot_binds_by_scene_id_even_when_name_differs(
    storyboard_services: tuple[Database, ShotService, RecordingJobService, dict[str, Any]],
) -> None:
    """绑定以 scene_asset_id 为准：模型给出的 ID 合法时直接采用，
    名称只是展示字段，名称与素材不一致不再报错（新绑定协议）。"""
    database, service, _jobs, chapter = storyboard_services
    AssetService(database).create(
        chapter["project_id"],
        {"asset_type": "scene", "name": "青云山巅"},
    )
    wrong_scene = AssetService(database).create(
        chapter["project_id"],
        {"asset_type": "scene", "name": "竹林"},
    )
    extracted = _extracted_shot(wrong_scene["id"])

    result = service._save_extracted(
        chapter,
        "screenplay",
        {"scope": "all", "scene_no": None},
        [extracted],
    )

    assert result["count"] == 1
    saved = service.list(chapter["id"])
    assert saved[0]["scene_asset_id"] == wrong_scene["id"]


def test_extract_shot_binds_by_character_id_even_when_name_differs(
    storyboard_services: tuple[Database, ShotService, RecordingJobService, dict[str, Any]],
) -> None:
    """角色绑定以 character_asset_ids 为准，名称与 ID 不一致不再报错：
    名称只作展示与兜底，ID 必须指向目录中真实素材。"""
    database, service, _jobs, chapter = storyboard_services
    scene = AssetService(database).create(
        chapter["project_id"],
        {"asset_type": "scene", "name": "青云山巅"},
    )
    AssetService(database).create(
        chapter["project_id"],
        {"asset_type": "character", "name": "少年"},
    )
    wrong_character = AssetService(database).create(
        chapter["project_id"],
        {"asset_type": "character", "name": "师姐"},
    )
    extracted = _extracted_shot(scene["id"])
    extracted["character_names"] = ["少年"]
    extracted["character_asset_ids"] = [wrong_character["id"]]

    result = service._save_extracted(
        chapter,
        "screenplay",
        {"scope": "all", "scene_no": None},
        [extracted],
    )

    assert result["count"] == 1
    saved = service.list(chapter["id"])
    assert saved[0]["character_ids"] == [wrong_character["id"]]


def test_resolve_asset_ids_with_duplicate_ids_returns_unique_ids(
    storyboard_services: tuple[Database, ShotService, RecordingJobService, dict[str, Any]],
) -> None:
    database, service, _jobs, chapter = storyboard_services
    character = AssetService(database).create(
        chapter["project_id"],
        {"asset_type": "character", "name": "少年"},
    )
    lookup = service._asset_lookup(chapter["project_id"])["character"]

    resolved, unmatched = service._resolve_asset_ids(
        lookup,
        provided_ids=[character["id"], character["id"]],
        names=["少年"],
    )

    assert resolved == [character["id"]]
    assert unmatched == []



def test_auto_extend_dialogue_shot_within_fifteen_second_limit() -> None:
    """未超单镜上限、仅需延长的镜头：自动把时长调到推荐值并重排时间轴。"""
    shot = _extracted_shot("scene-1")
    shot["duration_sec"] = 7
    shot["action"] = "少年走入房间。"
    shot["dialogue"] = [{"speaker": "少年", "text": "台" * 13}]
    shot["video_prompt"] = "0-3秒：少年走入房间；3-7秒：少年停下脚步。"

    extended = ShotService._auto_extend_dialogue_shot(shot)

    assert extended["duration_sec"] == 9
    assert "7秒" not in extended["video_prompt"]
    assert "9秒" in extended["video_prompt"]
    assert extended["dialogue"] == shot["dialogue"]
    assert not ShotService._shot_quality_warnings(extended)


def test_extract_shots_auto_extends_dialogue_duration_on_save(
    storyboard_services: tuple[Database, ShotService, RecordingJobService, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """批量提取遇到 7 秒/13 字镜头时自动延长到 9 秒并保存成功，不再 409 中断整批。"""
    database, service, _jobs, chapter = storyboard_services
    scene = AssetService(database).create(
        chapter["project_id"],
        {"asset_type": "scene", "name": "青云山巅"},
    )
    content = _screenplay_content()
    source_text = "台" * 13
    content["scenes"][0]["dialogue"] = [{"speaker": "少年", "text": source_text}]
    WritingService(database).update_screenplay(chapter["id"], {"content": content})
    extracted = _extracted_shot(scene["id"])
    extracted["duration_sec"] = 7
    extracted["action"] = "少年走入房间。"
    extracted["dialogue"] = [{"speaker": "少年", "text": source_text}]
    extracted["video_prompt"] = "0-3秒：少年走入房间；3-7秒：少年停下脚步。"

    class ShortDialogueProvider:
        async def generate_json(self, **_values: Any) -> ExtractionOutput:
            return ExtractionOutput([extracted])

    monkeypatch.setattr(
        service.providers,
        "resolve",
        lambda **_kwargs: (
            ShortDialogueProvider(),
            {"id": "mock-text", "params": {}},
        ),
    )
    job = {
        "id": "job-extract-short-dialogue",
        "project_id": chapter["project_id"],
        "chapter_id": chapter["id"],
        "provider_id": "mock-text",
        "input_json": json_dumps(
            {
                "source_type": "screenplay",
                "scope": "all",
                "scene_no": None,
                "params": {},
            }
        ),
    }

    asyncio.run(service.handle_extract(job))

    saved = service.list(chapter["id"])
    assert len(saved) == 1
    assert saved[0]["duration_sec"] == 9
    assert saved[0]["dialogue"][0]["text"] == source_text
    assert "".join(item["text"] for shot in saved for item in shot["dialogue"]) == source_text


def test_extract_shots_unmatched_scene_names_report_missing_instead_of_blocking(
    storyboard_services: tuple[Database, ShotService, RecordingJobService, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """剧本场次名无法匹配素材库时不再前置拦截整批提取：改为在 user_prompt 提示模型
    报告 missing_assets（后续由补素材流程兜底），模型若既不给 ID 也不报告缺失，
    保存阶段按绑定失败明确报错，绝不硬选近似素材兜底。"""
    database, service, _jobs, chapter = storyboard_services
    AssetService(database).create(
        chapter["project_id"],
        {"asset_type": "scene", "name": "咖啡馆"},
    )
    content = _screenplay_content()
    content["scenes"][0]["scene_name"] = "坐错桌"
    WritingService(database).update_screenplay(chapter["id"], {"content": content})
    captured: dict[str, Any] = {}

    class UnexpectedProvider:
        async def generate_json(self, **values: Any) -> ExtractionOutput:
            captured.update(values)
            return ExtractionOutput([_extracted_shot("")])

    monkeypatch.setattr(
        service.providers,
        "resolve",
        lambda **_kwargs: (
            UnexpectedProvider(),
            {"id": "mock-text", "params": {}},
        ),
    )
    job = {
        "project_id": chapter["project_id"],
        "chapter_id": chapter["id"],
        "provider_id": "mock-text",
        "input_json": json_dumps(
            {
                "source_type": "screenplay",
                "scope": "all",
                "scene_no": None,
                "params": {},
            }
        ),
    }

    # 模型被调用（不再前置拦截），且 user_prompt 收到不匹配提示
    with pytest.raises(ServiceError) as exc_info:
        asyncio.run(service.handle_extract(job))

    assert "坐错桌" in captured["user_prompt"]
    # 模型未报告缺失素材时，保存阶段按绑定失败报错，不允许静默兜底
    assert "未匹配到场景素材" in exc_info.value.detail
    assert service.list(chapter["id"]) == []


def test_extract_shots_single_scene_scope_only_checks_target_scene(
    storyboard_services: tuple[Database, ShotService, RecordingJobService, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单场提取只校验目标场次：其他场次的旧名不影响单场重试。"""
    database, service, _jobs, chapter = storyboard_services
    AssetService(database).create(
        chapter["project_id"],
        {"asset_type": "scene", "name": "咖啡馆"},
    )
    content = _screenplay_content()
    content["scenes"] = [
        {**content["scenes"][0], "scene_no": 1, "scene_name": "坐错桌"},
        {**content["scenes"][0], "scene_no": 2, "scene_name": "咖啡馆"},
    ]
    WritingService(database).update_screenplay(chapter["id"], {"content": content})
    captured: dict[str, Any] = {}

    class TargetSceneProvider:
        async def generate_json(self, **values: Any) -> ExtractionOutput:
            captured.update(values)
            shot = _extracted_shot("")
            shot["scene_no"] = 2
            shot["scene_name"] = "咖啡馆"
            return ExtractionOutput([shot])

    monkeypatch.setattr(
        service.providers,
        "resolve",
        lambda **_kwargs: (
            TargetSceneProvider(),
            {"id": "mock-text", "params": {}},
        ),
    )
    job = {
        "project_id": chapter["project_id"],
        "chapter_id": chapter["id"],
        "provider_id": "mock-text",
        "input_json": json_dumps(
            {
                "source_type": "screenplay",
                "scope": "scene",
                "scene_no": 2,
                "params": {},
            }
        ),
    }

    asyncio.run(service.handle_extract(job))

    saved = service.list(chapter["id"])
    assert len(saved) == 1
    assert saved[0]["scene_no"] == 2


def test_extract_shots_missing_scene_triggers_refill_then_saves(
    storyboard_services: tuple[Database, ShotService, RecordingJobService, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺失素材 → 自动补建 → 重新提取 → 保存：剧情完整性优先，不硬绑近似素材。

    第一轮模型报告 missing_assets（场景「坐错桌」不在素材库），后端应调用
    complete_missing 自动补建素材；第二轮模型拿到新素材目录（含补建的场景 ID）
    正常绑定并保存分镜。
    """
    database, service, _jobs, chapter = storyboard_services
    AssetService(database).create(
        chapter["project_id"],
        {"asset_type": "scene", "name": "咖啡馆"},
    )
    content = _screenplay_content()
    content["scenes"][0]["scene_name"] = "坐错桌"
    WritingService(database).update_screenplay(chapter["id"], {"content": content})
    calls: list[dict[str, Any]] = []
    refilled: dict[str, Any] = {}

    class RefillProvider:
        """第一轮报告缺失；第二轮检查补建后正常绑定。"""

        def __init__(self) -> None:
            self.round = 0

        async def generate_json(self, **values: Any) -> ExtractionOutput:
            calls.append(values)
            self.round += 1
            if self.round == 1:
                shot = _extracted_shot("")
                shot["missing_assets"] = [
                    {
                        "asset_type": "scene",
                        "reason": "剧本场次「坐错桌」无对应场景素材",
                        "source_ref": "第一场",
                        "suggested_name": "坐错桌",
                    }
                ]
                return ExtractionOutput([shot])
            shot = _extracted_shot(refilled["asset_ids"][0])
            return ExtractionOutput([shot])

    provider = RefillProvider()
    monkeypatch.setattr(
        service.providers,
        "resolve",
        lambda **_kwargs: (provider, {"id": "mock-text", "params": {}}),
    )

    async def fake_complete_missing(self, _chapter_id, missing_assets, *, provider_id=None):  # type: ignore[no-untyped-def]
        # 真实补建场景素材（名称取自缺失清单），供第二轮目录绑定
        created = AssetService(database).create(
            chapter["project_id"],
            {
                "asset_type": missing_assets[0]["asset_type"],
                "name": missing_assets[0]["suggested_name"],
            },
        )
        refilled.update({"asset_ids": [created["id"]], "count": 1, "warnings": []})
        return refilled

    monkeypatch.setattr(AssetService, "complete_missing", fake_complete_missing)
    job = {
        "project_id": chapter["project_id"],
        "chapter_id": chapter["id"],
        "provider_id": "mock-text",
        "input_json": json_dumps(
            {
                "source_type": "screenplay",
                "scope": "all",
                "scene_no": None,
                "params": {},
            }
        ),
    }

    result = asyncio.run(service.handle_extract(job))

    # 第二轮 user_prompt 的素材目录必须包含补建场景「坐错桌」（ID 优先绑定）
    assert len(calls) == 2
    assert "坐错桌" in calls[1]["user_prompt"]
    saved = service.list(chapter["id"])
    assert len(saved) == 1
    assert saved[0]["scene_asset_id"] == refilled["asset_ids"][0]
    assert result["count"] == 1
    assert any("检测到素材缺失" in warning["message"] for warning in result["warnings"])


def test_extract_shots_last_round_refill_verified_before_failing(
    storyboard_services: tuple[Database, ShotService, RecordingJobService, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """最后一轮补建成功后必须重新提取验证，而不是按补建前的清单直接报失败。

    复现真实故障：素材补建其实已经入库，但任务在同一秒仍以
    「分镜素材缺失」失败结束。修复后补建成功的那一轮会再提取一次，
    若重新提取不再报告缺失，任务应正常保存。
    """
    database, service, _jobs, chapter = storyboard_services
    AssetService(database).create(
        chapter["project_id"],
        {"asset_type": "scene", "name": "咖啡馆"},
    )
    content = _screenplay_content()
    content["scenes"][0]["scene_name"] = "坐错桌"
    WritingService(database).update_screenplay(chapter["id"], {"content": content})
    calls: list[dict[str, Any]] = []
    refilled: dict[str, Any] = {"asset_ids": []}

    class RefillProvider:
        """前两轮报告缺失；第二轮补建后第三轮正常绑定。"""

        def __init__(self) -> None:
            self.round = 0

        async def generate_json(self, **values: Any) -> ExtractionOutput:
            calls.append(values)
            self.round += 1
            if self.round <= 2:
                shot = _extracted_shot("")
                shot["missing_assets"] = [
                    {
                        "asset_type": "scene",
                        "reason": "剧本场次「坐错桌」无对应场景素材",
                        "source_ref": "第一场",
                        "suggested_name": f"坐错桌第{self.round}别名",
                    }
                ]
                return ExtractionOutput([shot])
            # 第三轮：补建已入库，按 ID 正常绑定
            shot = _extracted_shot(refilled["asset_ids"][-1])
            return ExtractionOutput([shot])

    provider = RefillProvider()
    monkeypatch.setattr(
        service.providers,
        "resolve",
        lambda **_kwargs: (provider, {"id": "mock-text", "params": {}}),
    )

    async def fake_complete_missing(self, _chapter_id, missing_assets, *, provider_id=None):  # type: ignore[no-untyped-def]
        created = AssetService(database).create(
            chapter["project_id"],
            {
                "asset_type": missing_assets[0]["asset_type"],
                "name": missing_assets[0]["suggested_name"],
            },
        )
        refilled["asset_ids"].append(created["id"])
        return {"asset_ids": [created["id"]], "count": 1, "warnings": []}

    monkeypatch.setattr(AssetService, "complete_missing", fake_complete_missing)
    job = {
        "project_id": chapter["project_id"],
        "chapter_id": chapter["id"],
        "provider_id": "mock-text",
        "input_json": json_dumps(
            {
                "source_type": "screenplay",
                "scope": "all",
                "scene_no": None,
                "params": {},
            }
        ),
    }

    result = asyncio.run(service.handle_extract(job))

    # 两次补建 + 每次补建后的验证提取 = 3 次分镜模型调用
    assert len(calls) == 3
    saved = service.list(chapter["id"])
    assert len(saved) == 1
    assert saved[0]["scene_asset_id"] == refilled["asset_ids"][-1]
    assert result["count"] == 1
