from __future__ import annotations

import pytest

from app.services.prompt_service import (
    compile_asset_prompt,
    compile_keyframe_prompt,
    compile_video_prompt,
    shot_template_context,
)


def _shot_context_with_positions_and_expressions() -> dict[str, object]:
    return shot_template_context(
        {
            "source_ref": "scene:1|古寺门口",
            "start_state": "林雪站在门外。",
            "frame_description": "林雪与木门同框。",
            "end_state": "林雪停在门前。",
            "character_ids": ["character-1"],
            "scene_asset_id": "scene-1",
            "prop_ids": [],
            "positions": [
                {
                    "character": "林雪",
                    "position": "画面左侧，身体朝向画面右侧",
                }
            ],
            "expressions": [
                {
                    "character": "林雪",
                    "expression": "警惕皱眉",
                }
            ],
            "dialogue": [],
            "shot_type": "中景",
            "camera_angle": "平视",
            "camera_move": "固定",
            "action": "林雪观察木门。",
        },
        {
            "character-1": "林雪",
            "scene-1": "古寺偏殿",
        },
    )


def test_shot_template_context_normalizes_dialogue_aliases() -> None:
    context = shot_template_context(
        {
            "dialogue": [
                {"speaker": "林雪", "line": "别动。"},
                {"character": "守卫", "content": "站住。"},
            ]
        },
        {},
    )

    assert context["dialogue"] == "林雪自然开口说：别动。；守卫自然开口说：站住。"


@pytest.mark.parametrize(
    ("asset_type", "profile", "expected_text", "expected_aspect_ratio"),
    [
        (
            "character",
            {
                "age": "二十岁",
                "gender": "女性",
                "identity": "见习符师",
                "personality": "冷静、坚韧",
                "appearance": "黑色短发，琥珀色眼睛",
                "hair": "利落短发",
                "body": "修长",
                "costume": "青灰短斗篷与皮靴",
                "immutable_features": ["左眉小痣", "铜制符笔"],
            },
            "3:2 横向角色设定面板",
            "3:2",
        ),
        (
            "prop",
            {
                "description": "刻有星纹的铜制罗盘",
                "owner": "林雪",
                "material": "做旧黄铜与黑木",
                "color": "暗金与墨黑",
                "special_features": ["星纹刻度", "可旋转外环"],
                "plot_function": "寻找古墓入口",
            },
            "3:2 横向专业道具设定面板",
            "3:2",
        ),
        (
            "scene",
            {
                "architecture": "山中古寺偏殿",
                "layout": "正中供台，两侧木窗",
                "lighting": "窗外冷月光与烛光",
                "color_mood": "青灰、暖金",
                "furnishings": ["供台", "铜灯", "旧书架"],
            },
            "16:9 横向专业场景设定面板",
            "16:9",
        ),
    ],
)
def test_compile_asset_prompt_uses_type_specific_setting_sheet_template(
    asset_type: str,
    profile: dict[str, object],
    expected_text: str,
    expected_aspect_ratio: str,
) -> None:
    prompt = compile_asset_prompt(
        style_prompt="东方奇幻二维动画",
        aspect_ratio="9:16",
        asset_type=asset_type,
        asset_name="测试素材",
        profile=profile,
        prompt="补充的视觉细节",
        negative_prompt="文字水印，真人摄影",
        consistency_keywords=["固定服装", "固定发型"],
        composition="主体清晰居中",
    )

    assert "统一画风锁定" in prompt
    assert expected_text in prompt
    assert "补充的视觉细节" in prompt
    assert "negative prompt: 文字水印，真人摄影" in prompt
    assert f"画面比例：{expected_aspect_ratio}。" in prompt
    assert "画面比例：9:16。" not in prompt
    assert "{" not in prompt


def test_compile_asset_prompt_uses_type_specific_default_negative_prompt() -> None:
    prompt = compile_asset_prompt(
        style_prompt="东方奇幻二维动画",
        aspect_ratio="16:9",
        asset_type="prop",
        asset_name="铜制罗盘",
        profile={"description": "刻有星纹的铜制罗盘"},
        prompt="",
        negative_prompt="",
        consistency_keywords=[],
        composition="",
    )

    assert "negative prompt: 完整人物、人物脸、多件物品" in prompt


def test_compile_keyframe_prompt_uses_v2_template_and_reference_order() -> None:
    prompt = compile_keyframe_prompt(
        style_prompt="东方奇幻二维动画",
        aspect_ratio="9:16",
        image_prompt="林雪按住古寺木门，尚未推开。",
        negative_prompt="身份漂移、服装变化、文字水印",
        anchors=[
            "@Image1：林雪，锁定角色身份与服装。",
            "@Image2：古寺偏殿，锁定场景空间结构和光线。",
        ],
        shot_context={
            "time_place": "夜晚，古寺偏殿门口",
            "opening_frame": "林雪右手按住木门，身体前倾。",
            "ending_frame": "木门刚被推开一条缝。",
            "characters": ["林雪"],
            "scene_name": "古寺偏殿",
            "props": ["铜制罗盘"],
            "shot_type": "中景",
            "camera_angle": "平视",
            "camera_move": "缓慢推进",
            "focus": "林雪握门的右手",
            "lighting": "月光与烛光",
            "color_tone": "冷青与暖金",
            "environment_details": "旧木门与浮尘",
        },
    )

    assert "【当前分镜素材参考图】" in prompt
    assert "@Image1：林雪" in prompt
    assert "用途：生成当前分镜唯一的分镜图" in prompt
    assert "不得提前呈现动作完成结果" in prompt
    assert "视频末帧边界：木门刚被推开一条缝。" in prompt
    assert "negative prompt: 身份漂移、服装变化、文字水印" in prompt
    assert "画面比例：16:9。" in prompt
    assert "画面比例：9:16。" not in prompt


def test_compile_keyframe_prompt_includes_positions_and_expressions() -> None:
    prompt = compile_keyframe_prompt(
        style_prompt="东方奇幻二维动画",
        aspect_ratio="16:9",
        image_prompt="林雪站在古寺门前。",
        negative_prompt="",
        anchors=[],
        shot_context=_shot_context_with_positions_and_expressions(),
    )

    assert "画面左侧，身体朝向画面右侧" in prompt
    assert "警惕皱眉" in prompt


def test_compile_keyframe_prompt_includes_dialogue_aliases() -> None:
    context = shot_template_context(
        {
            "dialogue": [
                {"speaker": "林雪", "line": "别动。"},
                {"character": "守卫", "content": "站住。"},
            ]
        },
        {},
    )

    prompt = compile_keyframe_prompt(
        style_prompt="东方奇幻二维动画",
        aspect_ratio="16:9",
        image_prompt="林雪与守卫在古寺门前对峙。",
        negative_prompt="",
        anchors=[],
        shot_context=context,
    )

    assert "林雪自然开口说：别动。" in prompt
    assert "守卫自然开口说：站住。" in prompt


def test_compile_video_prompt_uses_v2_template_and_reference_order() -> None:
    prompt = compile_video_prompt(
        style_prompt="东方奇幻二维动画",
        aspect_ratio="9:16",
        video_prompt="林雪缓慢推开木门，门缝透出暖光。",
        negative_prompt="身份漂移、道具穿模、文字水印",
        duration_sec=6,
        anchors=[
            "@Image1：当前分镜关键帧，锁定构图、人物左右关系和起始站位。",
            "@Image2：林雪，锁定角色身份与服装。",
        ],
        shot_context={
            "previous_state": "上一镜中林雪停在门外。",
            "opening_frame": "林雪右手按住木门。",
            "ending_frame": "木门打开一条缝，林雪停住。",
            "characters": ["林雪"],
            "scene_name": "古寺偏殿",
            "props": ["铜制罗盘"],
            "shot_type": "中景",
            "camera_angle": "平视",
            "camera_move": "缓慢推进",
            "focus": "木门缝隙",
            "composition": "人物位于画面左侧",
            "lighting": "月光与烛光",
            "color_tone": "冷青与暖金",
            "environment_details": "旧木门与浮尘",
            "dialogue": "无台词",
            "sound_effects": "木门摩擦声",
            "music": "低缓弦乐",
        },
    )

    assert "【参考】" in prompt
    assert "@Image1：当前分镜关键帧" in prompt
    assert "【执行】" in prompt
    assert "结束状态：木门打开一条缝，林雪停住。" in prompt
    assert "【镜头与声音】" in prompt
    assert "【保持】" in prompt
    assert "补充禁止事项：身份漂移、道具穿模、文字水印。" in prompt
    assert "东方奇幻二维动画，6秒，16:9。" in prompt
    assert "画面比例：9:16。" not in prompt


def test_compile_video_prompt_includes_positions_and_expressions() -> None:
    prompt = compile_video_prompt(
        style_prompt="东方奇幻二维动画",
        aspect_ratio="16:9",
        video_prompt="0-3秒：林雪观察木门。",
        duration_sec=3,
        anchors=[],
        shot_context=_shot_context_with_positions_and_expressions(),
    )

    assert "画面左侧，身体朝向画面右侧" in prompt
    assert "警惕皱眉" in prompt


def test_compile_video_prompt_preserves_existing_timeline_without_nesting() -> None:
    timeline = "0-2秒：林雪推门；2-4秒：门缝扩大；4-6秒：林雪停住。"

    prompt = compile_video_prompt(
        style_prompt="东方奇幻二维动画",
        aspect_ratio="16:9",
        video_prompt=timeline,
        duration_sec=6,
        anchors=["@Image1：当前分镜关键帧。"],
        shot_context={},
    )

    assert timeline in prompt
    assert "0-2秒：0-2秒" not in prompt
    assert prompt.count("0-2秒") == 1


def test_compile_video_prompt_with_short_duration_omits_zero_length_segment() -> None:
    prompt = compile_video_prompt(
        style_prompt="东方奇幻二维动画",
        aspect_ratio="16:9",
        video_prompt="林雪眨眼。",
        duration_sec=1,
        anchors=["@Image1：当前分镜关键帧。"],
        shot_context={},
    )

    assert "0-1秒：林雪眨眼。" in prompt
    assert "1-1秒" not in prompt
