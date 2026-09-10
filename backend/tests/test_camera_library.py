"""运镜词库与运镜决策诊断（克制策略）的回归测试。"""

from __future__ import annotations

import pytest

from app.services.camera_library import (
    CAMERA_PRESETS,
    DEFAULT_CAMERA_POLICY,
    POLICY_CONSERVATIVE,
    POLICY_RICH,
    POLICY_STANDARD,
    SUPPORTED_POLICIES,
    camera_decision_diagnostics,
    find_preset,
    is_static_camera,
    normalize_policy,
)


def _shot(**overrides) -> dict:
    payload = {
        "camera_move": "固定",
        "camera_angle": "平视",
        "shot_type": "中景",
        "action": "",
        "start_state": "",
        "end_state": "",
        "dialogue": [],
    }
    payload.update(overrides)
    return payload


def test_词库完整性_50条且分类数量正确() -> None:
    assert len(CAMERA_PRESETS) == 50
    ids = [preset.id for preset in CAMERA_PRESETS]
    assert len(set(ids)) == 50
    names = [preset.name for preset in CAMERA_PRESETS]
    assert len(set(names)) == 50
    by_category: dict[str, int] = {}
    for preset in CAMERA_PRESETS:
        by_category[preset.category] = by_category.get(preset.category, 0) + 1
    assert by_category == {"static": 12, "dynamic": 16, "pov": 12, "transition": 10}


def test_词库模板_双模板均不含品牌词与美术越权词() -> None:
    # 治理要求：预设文本只保留纯机位/构图/动作描述，品牌词与画风/摄影越权词一律移除
    banned = (
        "即梦",
        "景深",
        "焦距",
        "画风",
        "质感",
        "模糊",
        "虚化",
        "光线",
        "阴影",
        "渲染",
        "线条",
    )
    for preset in CAMERA_PRESETS:
        for template in (preset.template_core, preset.template_jimeng):
            for word in banned:
                assert word not in template, (preset.id, word)


@pytest.mark.parametrize(
    "camera_move, expected",
    [
        ("缓慢推镜（人物）", "缓慢推镜"),
        ("快速推镜", "快速推镜"),
        ("固定", None),
        ("", None),
        ("缓慢推进", None),
    ],
)
def test_查找词库条目_按规范名匹配(camera_move: str, expected: str | None) -> None:
    if expected is None:
        assert find_preset(camera_move) is None
        return
    preset = find_preset(camera_move)
    assert preset is not None
    assert preset.name == expected


def test_静态判定_空值与固定标记均为定镜() -> None:
    assert is_static_camera("")
    assert is_static_camera("固定")
    assert is_static_camera("机位不动")
    assert not is_static_camera("缓慢推镜")


@pytest.mark.parametrize(
    "value, expected",
    [
        ("标准", "标准"),
        ("丰富", "丰富"),
        ("", "克制"),
        ("乱写", "克制"),
        (None, "克制"),
    ],
)
def test_策略归一化_非法值回落克制(value, expected: str) -> None:
    assert normalize_policy(value) == expected
    assert DEFAULT_CAMERA_POLICY == POLICY_CONSERVATIVE
    assert SUPPORTED_POLICIES == ("克制", "标准", "丰富")


def test_多插检测_无动机运镜在克制下警告并建议固定() -> None:
    issues = camera_decision_diagnostics(
        _shot(camera_move="快速推镜", action="他推门走进房间"),
        camera_motivation="",
        policy=POLICY_CONSERVATIVE,
    )
    assert [issue.code for issue in issues] == ["camera_unmotivated"]
    issue = issues[0]
    assert issue.severity == "warning"
    assert issue.recommended_camera_move == "固定"


def test_多插检测_有动机运镜不报警() -> None:
    issues = camera_decision_diagnostics(
        _shot(camera_move="缓慢推镜", action="他握紧拳头，下定决心"),
        camera_motivation="",
        policy=POLICY_CONSERVATIVE,
    )
    assert issues == []


def test_多插检测_反模式场景对谈用运镜报警() -> None:
    issues = camera_decision_diagnostics(
        _shot(camera_move="缓慢推镜", action="两人对谈，交换情报"),
        camera_motivation="",
        policy=POLICY_CONSERVATIVE,
    )
    assert [issue.code for issue in issues] == ["camera_anti_pattern"]
    assert issues[0].recommended_camera_move == "固定"


def test_漏加检测_克制策略不提醒() -> None:
    issues = camera_decision_diagnostics(
        _shot(camera_move="固定", action="他深吸一口气，下定决心"),
        policy=POLICY_CONSERVATIVE,
    )
    assert issues == []


def test_漏加检测_标准策略给运镜建议() -> None:
    issues = camera_decision_diagnostics(
        _shot(camera_move="固定", action="他深吸一口气，下定决心"),
        policy=POLICY_STANDARD,
    )
    assert [issue.code for issue in issues] == ["camera_suggestion"]
    assert issues[0].severity == "suggestion"
    assert issues[0].recommended_camera_move == "缓慢推镜"


def test_漏加检测_丰富策略同样给建议() -> None:
    issues = camera_decision_diagnostics(
        _shot(camera_move="固定", action="他深吸一口气，下定决心"),
        policy=POLICY_RICH,
    )
    assert any(issue.code == "camera_suggestion" for issue in issues)


def test_未知运镜写法_不做判断() -> None:
    issues = camera_decision_diagnostics(
        _shot(camera_move="缓慢推进", action="两人对谈"),
        policy=POLICY_CONSERVATIVE,
    )
    assert issues == []


def test_命中触发词的镜头_克制策略默认固定无建议() -> None:
    issues = camera_decision_diagnostics(
        _shot(camera_move="", action="主角在城市废墟中孤身站立"),
        policy=POLICY_CONSERVATIVE,
    )
    assert issues == []
