"""阶段 4 分镜语义一致性门禁（storyboard_* warning 族）回归测试。

规则沉淀自 2026-08 两组连续性评审的缺陷类别：站位与动作起点矛盾、
断裂残句、空镜误用、时段漂移、持有道具失联、可读文字渲染风险。
"""

from __future__ import annotations

from typing import Any

from app.dynamic_pipeline.storyboard_stage import validate_storyboard_output

_SEMANTIC_CODES = {
    "storyboard_position_action_conflict",
    "storyboard_fragmented_sentence",
    "storyboard_empty_shot_with_cast",
    "storyboard_time_phase_drift",
    "storyboard_readable_text_risk",
    "storyboard_held_prop_untracked",
    "storyboard_prop_alias_drift",
}

_META: dict[str, Any] = {
    "schema_version": "dynamic-storyboard-v2",
    "normalizer_version": "dynamic-normalizer-v1",
    "input_signature": "semantic-gate-input",
    "content_signature": "semantic-gate-content",
    "source_attempt": 1,
}


def _shot(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "external_key": "storyboard:run-1:scene-1:shot-1:ordinal-1",
        "shot_no": 1,
        "ordinal": 1,
        "scene_id": "scene-1",
        "time_weather": "黄昏；金色霞光",
        "character_names": ["林风"],
        "prop_names": [],
        "positions": [
            {"character": "林风", "position": "古松下画面中央偏左", "facing": "背对镜头，面向云海"}
        ],
        "start_state": "林风立于古松下望向云海",
        "action": "0-11秒，林风静立不动，山风吹动衣摆。",
        "end_state": "林风保持原位静立",
        "transition_from_prev": "scene_change",
        "duration_sec": 11,
    }
    payload.update(overrides)
    return payload


def _key(no: int) -> str:
    return f"storyboard:run-1:scene-1:shot-{no}:ordinal-{no}"


def _output(*shots: dict[str, Any]) -> dict[str, Any]:
    total = sum(shot["duration_sec"] for shot in shots)
    return {"artifact_meta": _META, "target_duration_sec": total, "shots": list(shots)}


def _semantic_codes(payload: dict[str, Any]) -> set[str]:
    diagnostics = validate_storyboard_output(payload)
    return {item.code for item in diagnostics} & _SEMANTIC_CODES


def test_semantic_gate_flags_facing_then_turn_contradiction() -> None:
    shot = _shot(
        shot_no=2,
        ordinal=2,
        external_key=_key(2),
        transition_from_prev="continuous",
        character_names=["林风", "苏月"],
        positions=[{"character": "林风", "position": "画面中央偏左", "facing": "面向苏月"}],
        action="0-3秒，两人静立；3-6秒，苏月轻声开口；6-9秒，林风闻声微微侧过脸看向苏月。",
    )
    assert "storyboard_position_action_conflict" in _semantic_codes(_output(shot))


def test_semantic_gate_flags_duplicated_phrase_and_broken_clause() -> None:
    broken = _shot(action="她走到林风身后半步停住，没有；人物保持闭口。")
    duplicated = _shot(
        shot_no=2,
        ordinal=2,
        external_key=_key(2),
        character_names=["林风", "苏月"],
        positions=[
            {"character": "林风", "position": "画面中央偏左，古松下，背对苏月"},
            {"character": "苏月", "position": "画面中央偏左，侧身面向苏月，侧身面向苏月，苏月"},
        ],
    )
    codes = _semantic_codes(_output(broken, duplicated))
    assert "storyboard_fragmented_sentence" in codes


def test_semantic_gate_flags_empty_shot_term_and_time_phase_drift() -> None:
    empty_term = _shot(narration="两人一前一后，空镜中有风吹过。")
    drift = _shot(
        shot_no=2,
        ordinal=2,
        external_key=_key(2),
        transition_from_prev="continuous",
        character_names=["林风", "苏月"],
        action="0-10秒，苏月从松影后走出，白色纱裙沾着夜露，走到林风身后半步停住。",
    )
    codes = _semantic_codes(_output(empty_term, drift))
    assert {"storyboard_empty_shot_with_cast", "storyboard_time_phase_drift"} <= codes


def test_semantic_gate_flags_runes_but_keeps_negated_mention_clean() -> None:
    risky = _shot(action="0-14秒，破天剑剑身上的符文从剑柄向剑尖依次亮起，蓝光渐盛。")
    assert "storyboard_readable_text_risk" in _semantic_codes(_output(risky))

    safe = _shot(action="0-14秒，剑身上并无符文，只有淡蓝能量纹路缓缓变亮。")
    assert "storyboard_readable_text_risk" not in _semantic_codes(_output(safe))


def test_semantic_gate_flags_untracked_held_prop_between_continuous_shots() -> None:
    previous = _shot(prop_names=["破天剑"], end_state="林风右手紧握破天剑，纹路微光稳定。")
    current = _shot(
        shot_no=2,
        ordinal=2,
        external_key=_key(2),
        transition_from_prev="continuous",
        character_names=["林风", "苏月"],
        start_state="两人并肩静立于云海之前",
        action="0-6秒，两人对视无言。",
    )
    codes = _semantic_codes(_output(previous, current))
    assert "storyboard_held_prop_untracked" in codes


def test_semantic_gate_clean_pair_and_released_prop_stay_silent() -> None:
    previous = _shot(prop_names=["破天剑"], end_state="林风右手仍握着破天剑静立。")
    tracked = _shot(
        shot_no=2,
        ordinal=2,
        external_key=_key(2),
        transition_from_prev="continuous",
        character_names=["林风", "苏月"],
        start_state="林风右手仍握着破天剑，苏月在其身后半步静立",
        action="0-6秒，两人对视无言。",
    )
    assert _semantic_codes(_output(previous, tracked)) == set()

    released = _shot(prop_names=["破天剑"], end_state="林风把破天剑放下，负手而立。")
    after_release = _shot(
        shot_no=2,
        ordinal=2,
        external_key=_key(2),
        transition_from_prev="continuous",
        character_names=["林风", "苏月"],
        start_state="林风负手而立",
        action="0-6秒，远眺云海。",
    )
    assert _semantic_codes(_output(released, after_release)) == set()


def test_semantic_gate_flags_weapon_alias_drift_for_bound_prop() -> None:
    shot = _shot(
        prop_names=["破天剑"],
        action="0-6秒，林风静立古松下，右手垂在身侧，腰侧长剑泛着淡蓝光芒。",
    )
    assert "storyboard_prop_alias_drift" in _semantic_codes(_output(shot))


def test_semantic_gate_keeps_bound_name_mention_clean() -> None:
    shot = _shot(prop_names=["破天剑"], start_state="林风右手握着佩剑形状的破天剑垂于身侧")
    shot = dict(shot, action="0-6秒，他握紧破天剑，剑身纹路微亮。")
    codes = _semantic_codes(_output(shot))
    assert "storyboard_prop_alias_drift" not in codes
