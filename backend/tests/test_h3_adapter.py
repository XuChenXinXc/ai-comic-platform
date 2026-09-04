"""H3 出口适配层回归测试：精简压缩、预检互斥告警、Ref2VA 请求与校验器。"""

from __future__ import annotations

import json

from app.dynamic_pipeline.h3_adapter import (
    build_ref2va_rewrite_request,
    compact_chinese_prompt,
    detect_preflight_warnings,
    extract_dialogue_lines,
    validate_ref2va_sections,
)

# 取自青云山真实产物的高危片段（镜4），保留病灶原貌作为夹具。
_SOURCE = """【全局风格】
国风3D动漫。
画面比例：16:9，总时长：14.0秒。

【本镜唯一开始状态】
@Image1：当前分镜关键帧，锁定构图、人物左右关系和起始站位。
从苏月站在林风身后半步开始；林风仍面向云海。
人物站位：林风：画面中央偏左，古松下，背对苏月，面向云海。
人物站位必须与起始姿态一致：若起始为蜷缩/躺姿，站位不得写成坐姿。
起始朝向不得写成「面向镜头」。

【对白与口型】
第1句：苏月说："你还是要走吗？"
对白按时间轴中出现的对白时段依次执行；只有对应说话人开口。
时间轴中的动作、表情、旁白、画外音和环境声只用于画面效果。

【核心动作】
本镜只执行以下唯一一套时间轴，忽略其他时间描述；时间轴须连续覆盖 0 到 14 秒。
时间轴内不要重复描述机位；机位以【镜头】段落为准。
时间轴：0-3秒：山风持续，苏月望着林风的背影；林风闻声背影微顿。3-5秒：苏月迈步前行，行至林风身后半步处停下，静立未语。6-9秒：符文纹路亮起淡蓝光，蓝光沿剑身向剑尖蔓延。9-12秒：符文依次亮至剑尖，蓝光渐盛。12-14秒：两人无声相望，古松枝叶晃动。

【镜头】
双人中景，侧面平拍视角，缓慢环绕微移；缓慢跟随人物运动，不切换机位。
焦点围绕苏月望着林风的背影，轻声开口；林风闻声微微侧过脸，破天剑符文依次亮起。进行平缓、连续的调度；昏黄山巅，背景古松与云海。无文字，景别和光线方向保持稳定。

【声音】
环境与动作声音：符合场景的环境声。

【结束状态】
林风侧脸面向苏月方向，右手握紧剑柄。
结束状态必须明确所有已持有道具的最终位置；若某道具不再持有，必须交代已放下或离开画面，不得凭空消失。

【禁止项】
禁止出现：人物变形、服装变化、新增人物、字幕文字、物体瞬移；任何文字、数字、刻度数值、温度标识；苏月已走到林风身后停步；剑身清晰可读的符文文字；缴费单、笔记、招牌等画面文字由后期叠加；
"""


def _codes(source: str) -> set[str]:
    return {item.code for item in detect_preflight_warnings(source)}


def test_preflight_flags_prohibition_vs_destination_conflict() -> None:
    codes = _codes(_SOURCE)
    assert "storyboard_negative_action_conflict" in codes


def test_compact_removes_meta_dedupe_and_lowvalue_negatives() -> None:
    result = compact_chinese_prompt(_SOURCE)
    lowered_positions_removed = "人物站位必须与起始姿态一致" not in result.text
    assert lowered_positions_removed
    assert "温度标识" not in result.text
    assert "缴费单" not in result.text
    assert result.text.count("全片无任何文字、字幕或水印。") == 1
    assert "须连续覆盖 0 到 14 秒" in result.text
    camera_block = result.text.split("【镜头】\n", 1)[1].split("\n\n", 1)[0]
    assert camera_block.count("\n") == 0
    assert result.shrink_ratio > 0


def test_compact_keeps_dialogue_line_and_order() -> None:
    result = compact_chinese_prompt(_SOURCE)
    order = [segment.split("\n", 1)[0] for segment in result.text.split("\n\n")]
    headers = [segment.strip("【】") for segment in order]
    expected_head = [
        "全局风格",
        "本镜唯一开始状态",
        "对白与口型",
        "核心动作",
        "镜头",
        "声音",
        "结束状态",
        "禁止项",
    ]
    assert headers == expected_head
    assert '苏月说："你还是要走吗？"' in result.text


def test_ref2va_request_carries_labels_speakers_and_fallback() -> None:
    request = build_ref2va_rewrite_request(
        _SOURCE,
        reference_labels={"Picture 2": "Lin Feng character sheet"},
        dialogue_lines=[("苏月", "你还是要走吗？")],
    )
    payload = json.loads(request.user_payload)
    assert payload["speakers"] == {"苏月": "S1"}
    assert payload["dialogue_lines"][0]["required_markup"] == "<d>[Chinese] 你还是要走吗？</d>"
    assert payload["reference_labels"]["Picture 1"].startswith("current keyframe")
    assert request.fallback_text  # 校验失败时的退路是精简中文版


_VALID_REF2VA = """subject_definitions:
<Picture 1> is the exact opening frame.
<Subject 1> is Lin Feng from <Picture 2>.
<Subject 2> is the rune sword from <Picture 1>.
<Subject 4> is Su Yue, the young woman from <Picture 2>.

summary:
[keyframe completion + reference generation] A 14-second shot beginning from <Picture 1>.

retention_analysis:
<Picture 1> (first frame): fully_preserved - composition kept.
<Subject 1> (throughout): fully_preserved - identity kept.

detailed_description:
Golden dusk. [Shot 1] begins exactly from <Picture 1>. <Subject 4> (S1) asks,
<d>[Chinese] 你还是要走吗？</d> <Subject 1> turns slightly, tightening his grip
until pale-blue lines light up along <Subject 2>. No readable text appears anywhere.

overall_soundscape:
Mountain wind and a faint blade hum.

non_diegetic_music:
N/A
"""


def test_validator_accepts_wellformed_native_output() -> None:
    assert validate_ref2va_sections(_VALID_REF2VA, expect_dialogue=True) == []


def test_validator_rejects_missing_section_and_missing_dialogue_markup() -> None:
    needle = "overall_soundscape:\nMountain wind and a faint blade hum.\n"
    broken = _VALID_REF2VA.replace(needle, "")
    errors = validate_ref2va_sections(broken, expect_dialogue=True)
    assert any("order mismatch" in item for item in errors)

    plain = "\"你还是要走吗？\""
    swapped = _VALID_REF2VA.replace("<d>[Chinese] 你还是要走吗？</d>", plain)
    assert validate_ref2va_sections(swapped, expect_dialogue=True)


def test_ref2va_request_carries_source_text_times_and_rewrite_rules() -> None:
    request = build_ref2va_rewrite_request(
        _SOURCE,
        dialogue_lines=[("苏月", "你还是要走吗？", (0.5, 3.0))],
    )
    payload = json.loads(request.user_payload)
    assert payload["source_text"] == _SOURCE
    assert payload["dialogue_lines"][0]["time_range_sec"] == [0.5, 3.0]
    system = request.system_prompt.lower()
    assert "voice identity" in system
    assert "motion type" in system
    assert "never trim" in system
    assert "<cutoff>" in system
    assert "map each @imagen token" in system


def test_ref2va_speaker_ids_are_stable_per_speaker() -> None:
    request = build_ref2va_rewrite_request(
        _SOURCE,
        dialogue_lines=[("苏月", "第一句"), ("林风", "第二句"), ("苏月", "第三句")],
    )
    payload = json.loads(request.user_payload)
    assert payload["speakers"] == {"苏月": "S1", "林风": "S2"}
    assert [item["speaker_tag"] for item in payload["dialogue_lines"]] == ["S1", "S2", "S1"]


def test_extract_dialogue_lines_parses_section_format() -> None:
    assert extract_dialogue_lines(_SOURCE) == [("苏月", "你还是要走吗？")]


def test_validator_flags_undefined_reference_label() -> None:
    broken = _VALID_REF2VA.replace(
        "<Subject 4> is Su Yue, the young woman from <Picture 2>.\n",
        "",
    )
    errors = validate_ref2va_sections(broken)
    assert any("never defined" in item for item in errors)


def test_validator_flags_dropped_dialogue_line() -> None:
    errors = validate_ref2va_sections(
        _VALID_REF2VA,
        expect_dialogue=True,
        expected_dialogue_texts=["你还是要走吗？", "这句不存在"],
    )
    assert any("not preserved verbatim" in item for item in errors)


def test_validator_flags_cut_time_beyond_duration() -> None:
    injected = _VALID_REF2VA.replace(
        "Golden dusk.",
        "Golden dusk. At 00:16.000, the camera cuts to a wider view.",
    )
    errors = validate_ref2va_sections(injected, expected_duration_sec=14)
    assert any("exceeds requested duration" in item for item in errors)


def test_validator_allows_cut_time_exactly_at_duration_boundary() -> None:
    injected = _VALID_REF2VA.replace(
        "Golden dusk.",
        "Golden dusk. At 00:14.000, the camera cuts to a wider view.",
    )
    # 切点正好等于视频时长是合法边界，不算越界。
    errors = validate_ref2va_sections(injected, expected_duration_sec=14)
    assert not any("exceeds requested duration" in item for item in errors)


def test_ref2va_speaker_ids_number_by_first_appearance_not_line_number() -> None:
    request = build_ref2va_rewrite_request(
        _SOURCE,
        dialogue_lines=[("林风", "第一句"), ("苏月", "第二句"), ("林风", "第三句")],
    )
    payload = json.loads(request.user_payload)
    # 首个出场的说话人固定拿到 S1，与其出现在第几行无关。
    assert payload["speakers"] == {"林风": "S1", "苏月": "S2"}
    assert [item["speaker_tag"] for item in payload["dialogue_lines"]] == ["S1", "S2", "S1"]
