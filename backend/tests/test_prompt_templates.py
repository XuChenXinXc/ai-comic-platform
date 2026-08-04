from __future__ import annotations

import pytest

from app.prompts import load_prompt
from app.services.prompt_service import (
    compile_asset_prompt,
    compile_keyframe_prompt,
    compile_video_prompt,
    resolve_asset_negative_prompt,
    shot_template_context,
)


def test_asset_extraction_prompt_requires_independent_key_props() -> None:
    prompt = load_prompt("asset_extract")

    assert "值得单独生成素材参考图" in prompt
    assert "普通钥匙、手机、纸袋、杯子、文件、清洁工具" in prompt
    assert "物品的 `importance` 必须为 `core`" in prompt


def test_asset_extraction_prompt_requires_scene_assets_to_be_empty_backgrounds() -> None:
    prompt = load_prompt("asset_extract")

    assert "场景设定图必须是无人纯背景空镜" in prompt
    for field in (
        "`prompt`",
        "`composition`",
        "`main_view`",
        "`standard_view_1`",
        "`standard_view_2`",
    ):
        assert field in prompt
    assert "不得包含人物姓名、人物动作、人影、剪影、倒影中的人物或人体局部" in prompt
    assert "`character_position` 只描述可供表演的空置区域" in prompt


def test_asset_prompt_requires_scene_outputs_to_be_empty_backgrounds() -> None:
    prompt = load_prompt("asset_prompt")

    assert "场景素材必须输出无人纯背景空镜" in prompt
    assert "即使素材资料含有人物姓名、动作、站位、人物视角或随身物" in prompt
    assert "不得写入人物、" in prompt
    assert "人群、路人、人影、剪影、倒影中的人物或人体局部" in prompt


def test_shot_extraction_prompt_keeps_generic_items_out_of_asset_bindings() -> None:
    prompt = load_prompt("shot_extract")

    assert "普通物件即使出现在动作或画面中" in prompt
    assert "保留在 `action`、`frame_description`" in prompt
    assert "不得写入 `prop_names` 或 `prop_asset_ids`" in prompt


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
            "3:2横向宽幅、分块拼接式专业角色设定面板",
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
            "3:2横向宽幅、分块拼接式专业道具设定面板",
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
            "16:9横向宽幅、分块拼接式专业场景设定面板",
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
    # 自定义负向与默认负向合并：自定义项保留，默认约束自动追加
    assert "negative prompt: 文字水印、真人摄影" in prompt
    assert "文字、水印" in prompt
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


def test_scene_negative_prompt_merge_is_idempotent() -> None:
    merged = resolve_asset_negative_prompt("scene", "现代装修")

    assert resolve_asset_negative_prompt("scene", merged) == merged
    assert merged.count("人物、角色、人群、路人、行人、人影、人形、剪影") == 1


def test_compile_scene_asset_prompt_enforces_empty_background_with_custom_negative_prompt() -> None:
    prompt = compile_asset_prompt(
        style_prompt="都市温情二维国漫",
        aspect_ratio="16:9",
        asset_type="scene",
        asset_name="林记钟表店",
        profile={
            "concept": "林舟父亲留下的钟表店",
            "architecture": "老旧钟表店内部",
            "layout": "入口正对木柜台，后墙挂满旧钟",
            "character_position": "林舟站在柜台前打开暗格",
            "furnishings": ["木柜台", "挂钟墙"],
            "main_view": "人物站在柜台前，镜头从入口向内",
            "main_light": "林舟手机电筒光",
            "lighting": "冷白手电筒光束扫过挂钟墙",
        },
        prompt="钟表店内设有儿童阅读区。两个人影站在柜台前。手电光扫过挂钟墙。",
        negative_prompt="现代装修",
        consistency_keywords=["木柜台", "挂钟墙"],
        composition="人物位于画面中央",
    )

    assert "【最高优先级：无人纯背景空镜】" in prompt
    assert "人物姓名、人物动作、角色站位、人物视角或人物持有物" in prompt
    assert "表演活动区按主要区域和动线预留为空置空间" in prompt
    assert "林舟站在柜台前打开暗格" not in prompt
    assert "两个人影站在柜台前" not in prompt
    assert "人物位于画面中央" not in prompt
    assert "林舟" not in prompt
    assert "儿童阅读区" in prompt
    assert "手电光扫过挂钟墙" in prompt
    assert "negative prompt: 现代装修、人物、角色、人群、路人、行人、人影、人形、剪影" in prompt


def test_compile_scene_asset_prompt_removes_people_actions_from_all_free_text_fields() -> None:
    injected_names = [
        "林舟",
        "张助理",
        "顾律师",
        "周前台",
        "王经理",
        "陈女士",
        "赵先生",
        "孙秘书",
        "钱主任",
        "吴助理",
        "郑律师",
        "冯店员",
        "褚保安",
        "卫顾客",
        "蒋同事",
        "沈主管",
        "韩记者",
        "杨医生",
        "朱护士",
        "秦师傅",
        "许老板",
        "何警官",
        "吕司机",
        "施老师",
        "孔学生",
        "曹导演",
        "严摄影师",
    ]
    profile = {
        "worldview": f"{injected_names[0]}站在当代都市办公室",
        "place_type": f"{injected_names[1]}坐在室内接待区",
        "layout": f"{injected_names[2]}走向开放式办公区",
        "scale": f"{injected_names[3]}跑向宽阔大厅",
        "furnishings": f"{injected_names[4]}躺在深色沙发旁",
        "architecture": f"{injected_names[5]}蹲在现代玻璃隔断后",
        "regions": f"{injected_names[6]}看向会议室",
        "entrance_position": f"{injected_names[7]}手持文件站在东侧入口",
        "window_position": f"{injected_names[8]}穿着西装靠近落地窗",
        "movement_path": f"{injected_names[9]}走进中央走廊",
        "wall_material": f"{injected_names[10]}推开灰色石材墙旁的门",
        "floor_material": f"{injected_names[11]}拉开浅灰地毯上的椅子",
        "furniture_material": f"{injected_names[12]}打开深色木柜",
        "glass_metal_material": f"{injected_names[13]}关上金属玻璃门",
        "special_material": f"{injected_names[14]}转身离开磨砂玻璃墙",
        "color_mood": f"{injected_names[15]}抬手触碰冷灰色背景",
        "main_color": f"{injected_names[16]}低头站在深灰主色区域",
        "accent_color": f"{injected_names[17]}抬头看向青绿点缀区",
        "color_quality": f"{injected_names[18]}拿着病历进入低饱和空间",
        "main_view": f"{injected_names[19]}握着工具站在主视角中央",
        "standard_view_1": f"{injected_names[20]}走出正向全景",
        "standard_view_2": f"{injected_names[21]}离开反向全景",
        "detail_zone": f"{injected_names[22]}坐在接待台细节区",
        "lighting": f"{injected_names[23]}手持灯具照亮主光区",
        "fill_light": f"{injected_names[24]}的手机光形成柔和辅光",
        "atmosphere": f"{injected_names[25]}走入安静氛围区",
    }
    prompt = compile_asset_prompt(
        style_prompt="都市二维国漫",
        aspect_ratio="16:9",
        asset_type="scene",
        asset_name="律师事务所",
        profile=profile,
        prompt=f"木柜台保留。{injected_names[26]}站在柜台旁。",
        negative_prompt="",
        consistency_keywords=["木柜台", f"{injected_names[0]}坐在入口"],
        composition=f"入口位于左侧。{injected_names[1]}看向镜头。",
    )

    assert "木柜台" in prompt
    assert "入口位于左侧" in prompt
    for name in injected_names:
        assert name not in prompt


def test_compile_keyframe_prompt_uses_v3_template_and_reference_order() -> None:
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

    assert "【参考】" in prompt
    assert "@Image1：林雪" in prompt
    assert "用途：生成当前分镜唯一的分镜图" in prompt
    assert "不得提前呈现动作完成结果" in prompt
    assert "视频末帧边界：木门刚被推开一条缝。" in prompt
    assert "negative prompt: 身份漂移、服装变化、文字水印" in prompt
    assert "画面比例：16:9。" in prompt
    assert "画面比例：9:16。" not in prompt
    assert "导演画面补充" not in prompt


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
    assert "结束状态：木门打开一条缝，林雪停住，末0.5秒保持稳定" in prompt
    assert "【镜头与声音】" in prompt
    assert "【保持】" in prompt
    assert "补充禁止事项：身份漂移、道具穿模、文字水印。" in prompt
    assert "东方奇幻二维动画，6秒，16:9。" in prompt
    assert "画面比例：9:16。" not in prompt
    # 新行为：优先级段、参考图冲突裁决、机位二选一、口型、删音乐、禁止项压缩、道具锁定
    assert "【最高优先级】" in prompt
    assert "以参考图中的人物外观、服装、道具和空间关系为准" in prompt
    assert "缓慢跟随人物运动，不切换机位。" in prompt
    assert "固定机位，画面变化由人物动作推动" not in prompt
    assert "音乐" not in prompt
    assert "禁止出现：人物变形、服装变化、新增人物、字幕文字、物体瞬移。" in prompt
    assert "重点道具（铜制罗盘）的外观、尺寸、颜色" in prompt
    assert "不新增角色、手臂、手指、道具或其他物体" not in prompt


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
    # 双句号修复：非时间轴分支对白为空时不再追加多余句号
    assert "。。" not in prompt


def test_compile_video_prompt_without_anchors_omits_reference_ruling() -> None:
    """无参考图时省略冲突裁决句与道具锁定行；机位为空走固定默认。"""
    prompt = compile_video_prompt(
        style_prompt="东方奇幻二维动画",
        aspect_ratio="16:9",
        video_prompt="0-2秒：林雪推门；2-4秒：门缝扩大；4-6秒：林雪停住。",
        duration_sec=6,
        anchors=[],
        shot_context={},
    )

    assert "以参考图中的人物外观、服装、道具和空间关系为准" not in prompt
    assert "重点道具" not in prompt
    assert "固定机位，画面变化由人物动作推动，摄影机不移动。" in prompt


def test_camera_line_treats_stable_move_as_moving() -> None:
    """「稳定」是防抖描述，不视为固定机位。"""
    prompt = compile_video_prompt(
        style_prompt="东方奇幻二维动画",
        aspect_ratio="16:9",
        video_prompt="0-2秒：林雪推门；2-4秒：门缝扩大；4-6秒：林雪停住。",
        duration_sec=6,
        anchors=[],
        shot_context={"camera_move": "稳定横移"},
    )

    assert "稳定横移；缓慢跟随人物运动" in prompt
    assert "固定机位" not in prompt


def test_compile_video_prompt_extends_short_timeline_to_generation_duration() -> None:
    """时间轴末段不足生成时长时，模板补足稳定收尾段（供应商最低 6 秒场景）。"""
    timeline = "0-2秒：林雪推门。"

    prompt = compile_video_prompt(
        style_prompt="东方奇幻二维动画",
        aspect_ratio="16:9",
        video_prompt=timeline,
        duration_sec=6,
        anchors=["@Image1：当前分镜关键帧。"],
        shot_context={},
    )

    assert timeline in prompt
    assert "2-6秒：保持上一段结束状态稳定" in prompt


def test_compile_video_prompt_does_not_extend_complete_timeline() -> None:
    timeline = "0-2秒：林雪推门；2-4秒：门缝扩大；4-6秒：林雪停住。"

    prompt = compile_video_prompt(
        style_prompt="东方奇幻二维动画",
        aspect_ratio="16:9",
        video_prompt=timeline,
        duration_sec=6,
        anchors=["@Image1：当前分镜关键帧。"],
        shot_context={},
    )

    assert "4-6秒：林雪停住。" in prompt
    assert "保持上一段结束状态稳定" not in prompt


def test_compile_keyframe_prompt_uses_start_state_as_opening_frame() -> None:
    """关键帧必须以 start_state（动作前状态）为开场，而不是 frame_description。"""
    context = shot_template_context(
        {
            "start_state": "林舟站在屋檐下，照片还没从口袋取出。",
            "frame_description": "林舟已经跑进屋檐、掏出照片低头看。",
            "end_state": "林舟低头看着照片。",
            "character_ids": [],
            "scene_asset_id": "",
            "prop_ids": [],
            "dialogue": [],
        },
        {},
    )

    assert context["opening_frame"] == "林舟站在屋檐下，照片还没从口袋取出。"


def test_compile_keyframe_prompt_marks_voice_over_as_closed_lips() -> None:
    """录音笔/旁白属于画外音，关键帧要求画面人物全程闭口。"""
    context = shot_template_context(
        {
            "dialogue": [
                {"speaker": "父亲（录音）", "text": "小舟，听到这段话时我已经不在了。"},
            ],
            "character_ids": [],
            "scene_asset_id": "",
            "prop_ids": [],
        },
        {},
    )

    assert context["dialogue"] == ""
    assert "父亲（录音）：小舟" in context["voice_over"]
    prompt = compile_keyframe_prompt(
        style_prompt="东方奇幻二维动画",
        aspect_ratio="16:9",
        image_prompt="林舟听着录音笔。",
        negative_prompt="",
        anchors=[],
        shot_context=context,
    )
    assert "画面中所有人物全程闭口" in prompt


def test_compile_video_prompt_voice_over_keeps_mouths_closed() -> None:
    context = shot_template_context(
        {
            "dialogue": [
                {"speaker": "父亲（录音）", "text": "怀表留给你。"},
            ],
            "character_ids": [],
            "scene_asset_id": "",
            "prop_ids": [],
        },
        {},
    )

    prompt = compile_video_prompt(
        style_prompt="东方奇幻二维动画",
        aspect_ratio="16:9",
        video_prompt="0-2秒：林舟听着录音笔；2-4秒：他握紧怀表。",
        duration_sec=4,
        anchors=["@Image1：当前分镜关键帧。"],
        shot_context=context,
    )

    assert "画面内无台词对白：所有人物全程闭口" in prompt
    assert "画外音（后期配音，画面人物闭口）：父亲（录音）：怀表留给你。" in prompt


def test_compile_video_prompt_mixes_dialogue_and_voice_over() -> None:
    """画面内对白与画外音并存时：两者都输出，口型指令按画面内对白处理。"""
    context = shot_template_context(
        {
            "dialogue": [
                {"speaker": "林舟", "text": "你是谁？"},
                {"speaker": "父亲（录音）", "text": "小舟，别学我逃跑。"},
            ],
            "character_ids": [],
            "scene_asset_id": "",
            "prop_ids": [],
        },
        {},
    )

    assert "林舟自然开口说：你是谁？" in context["dialogue"]
    assert "父亲（录音）：小舟" in context["voice_over"]

    prompt = compile_video_prompt(
        style_prompt="东方奇幻二维动画",
        aspect_ratio="16:9",
        video_prompt="林舟质问苏晴。",
        duration_sec=4,
        anchors=["@Image1：当前分镜关键帧。"],
        shot_context=context,
    )

    assert "说话角色嘴唇轻微开合、表情自然" in prompt
    assert "画面内无台词对白" not in prompt
    assert "画外音（后期配音，画面人物闭口）：父亲（录音）：小舟" in prompt


def test_compile_asset_prompt_clean_reference_mode_has_no_setting_sheet_text() -> None:
    """干净参考图模式：无资料卡、无标注文字，专供图生图和视频引用。"""
    prompt = compile_asset_prompt(
        style_prompt="东方奇幻二维动画",
        aspect_ratio="16:9",
        asset_type="character",
        asset_name="林雪",
        profile={"hair": "黑色长发", "costume": "青色长裙", "age": "22", "gender": "女"},
        prompt="左眉有浅疤",
        negative_prompt="",
        consistency_keywords=["黑色长发"],
        composition="",
        reference_mode=True,
    )

    assert "干净角色参考图" in prompt
    assert "左上人物资料卡" not in prompt
    assert "三视图" not in prompt
    assert "纯白色" in prompt
    assert "不包含任何文字" in prompt


def test_compile_asset_prompt_reference_mode_merges_default_negative_prompt() -> None:
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
        reference_mode=True,
    )

    assert "干净道具参考图" in prompt
    assert "negative prompt: 完整人物、人物脸、多件物品" in prompt


def test_character_negative_prompt_merges_default_items() -> None:
    merged = resolve_asset_negative_prompt("character", "身份漂移，服装变化")

    assert "多人" in merged
    assert "身份漂移" in merged
    assert merged.count("身份漂移") == 1
    assert resolve_asset_negative_prompt("character", merged) == merged


def test_prop_negative_prompt_merges_default_items() -> None:
    merged = resolve_asset_negative_prompt("prop", "不要现代电子表")

    assert "完整人物、人物脸" in merged
    assert "不要现代电子表" in merged
    assert resolve_asset_negative_prompt("prop", merged) == merged
