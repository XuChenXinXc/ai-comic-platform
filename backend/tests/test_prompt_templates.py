from __future__ import annotations

import pytest

from app.prompts import load_prompt
from app.services.prompt_service import (
    _normalize_sentence_end,
    _strip_sentence_end,
    compile_asset_prompt,
    compile_keyframe_prompt,
    compile_video_prompt,
    resolve_asset_negative_prompt,
    shot_template_context,
)


def test_normalize_sentence_end_collapses_double_periods() -> None:
    assert (
        _normalize_sentence_end("低饱和冷暖协调色调，层次分明的电影光影。。")
        == "低饱和冷暖协调色调，层次分明的电影光影。"
    )
    assert _normalize_sentence_end("只用了不到一小时。") == "只用了不到一小时。"
    assert _normalize_sentence_end("真的吗？") == "真的吗？"


def test_strip_sentence_end_removes_trailing_punctuation_for_joins() -> None:
    assert _strip_sentence_end("画面不出现任何文字。") == "画面不出现任何文字"
    assert _strip_sentence_end("准备打开面前的玻璃罐。") == "准备打开面前的玻璃罐"


def test_compile_video_prompt_normalizes_field_punctuation_joins() -> None:
    prompt = compile_video_prompt(
        style_prompt="电影感现实主义二维国漫，层次分明的电影光影。",
        aspect_ratio="16:9",
        video_prompt="0-2秒：拧开罐盖；2-4秒：取胡萝卜。",
        duration_sec=6,
        anchors=[],
        shot_context={
            "transition_from_prev": "continuous",
            "prev_end_state": "已走到厨房操作台前停下。",
            "opening_frame": "站在厨房操作台前，准备打开面前的玻璃罐。",
            "ending_frame": "站在操作台前，低头等待泡发。",
            "characters": ["陈鑫"],
            "scene_name": "陈鑫的一楼公寓。",
            "shot_type": "近景",
            "camera_angle": "俯视",
            "camera_move": "缓慢下移",
            "focus": "陈鑫",
            "composition": "画面不出现任何文字。",
            "lighting": "符合场景设定的光线",
            "color_tone": "低饱和冷暖协调",
            "environment_details": "场景固定环境细节。",
            "dialogue": "",
            "voice_over": "只用了不到一小时。",
            "sound_effects": "罐盖拧开的轻响和温水声。",
        },
    )

    assert "。。" not in prompt
    assert "。，" not in prompt
    assert "。开始。" not in prompt
    assert "从站在厨房操作台前，准备打开面前的玻璃罐开始。" in prompt
    assert "已走到厨房操作台前停下。本镜动作直接承接该状态" in prompt
    assert "只用了不到一小时" not in prompt
    assert "画面不出现任何文字，景别和光线方向保持稳定。" in prompt
    assert "场景：陈鑫的一楼公寓。" in prompt


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


def test_shot_extraction_prompt_requires_verbatim_screenplay_facts_and_dialogue() -> None:
    prompt = load_prompt("shot_extract")

    assert "逐字复制" in prompt
    assert "不得改写、删减、补写" in prompt
    assert "不得新增或改变剧情事实" in prompt


def test_screenplay_prompt_constrains_scene_name_to_location_and_asset_library() -> None:
    """scene_name 必须是地点名且与场景素材库同名。

    回归 2026-08 缺陷：screenplay.md 原先对 scene_name 零约束（schema 也是裸 str），
    模型填成「坐错桌」这类桥段标题；而素材提取读正文、剧本生成读正文+分析，
    两步各自命名必然对不齐，分镜保存时按名称绑定场景素材就会整批失败。
    """
    prompt = load_prompt("screenplay")

    assert "`scene_name` 只能是场景的地点名称" in prompt
    assert "不得写桥段标题" in prompt
    assert "必须与场景素材库保持同名" in prompt
    assert "逐字复用" in prompt


def test_screenplay_scene_prompt_forbids_renaming_existing_scene_name() -> None:
    """单场重写不得改动已与素材库对齐的 scene_name。"""
    prompt = load_prompt("screenplay_scene")

    assert "`scene_name` 只能是场景的地点名称" in prompt
    assert "不要改动原场次已有的 `scene_name`" in prompt


def test_shot_extraction_prompt_delegates_scene_name_to_screenplay() -> None:
    """分镜阶段只能转抄剧本场次名，不该被要求自行对齐素材库。

    分镜提取读的是剧本，无法凭空知道素材库用哪个名；对齐责任在剧本阶段。
    """
    prompt = load_prompt("shot_extract")

    assert "逐字沿用剧本本场的场次名称" in prompt
    # 旧文案把对齐责任错放在分镜阶段，不应再出现
    assert "`scene_name` 必须使用素材库中的准确名称" not in prompt


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
    assert "文字水印" in prompt
    if asset_type == "character":
        assert "除指定人物资料卡外的其他文字" in prompt
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


def test_character_sheet_counts_expression_group_and_strips_scene_staging() -> None:
    """表情组四个就写四个特写；角色灯光、场景与手持道具不进入设定板。"""
    prompt = compile_asset_prompt(
        style_prompt="东方奇幻二维动画",
        aspect_ratio="3:2",
        asset_type="character",
        asset_name="徐尘心",
        profile={
            "age": "二十岁上下",
            "gender": "男",
            "identity": "万灯阁守灯弟子，无灵根・守灯人",
            "expression_group": ["平静倾听", "惊讶屏息", "执拗追问", "沉默悲伤"],
            "lighting": "命灯金暖光与夜色冷光，面部偏侧光",
            "carried_item": "常提一只旧铜油壶",
            "immutable_features": ["黑色低马尾", "青灰交领布袍"],
        },
        prompt="清瘦青年，黑色低马尾，青灰交领粗布内袍",
        negative_prompt="",
        consistency_keywords=["黑色低马尾"],
        composition="主人物立绘位于画面左侧主体区域，并在该区域内部居中完整站立。",
    )

    assert "排列4个高精度面部特写" in prompt
    assert "4个面部特写必须保持完全一致" in prompt
    assert "命灯金暖光与夜色冷光" not in prompt
    assert "旧铜油壶" not in prompt
    assert "柔和中性主光，辅以极轻微暖金轮廓光" in prompt
    assert "双臂自然下垂，双手自然放松" in prompt
    assert "不出现任何环境、布景或场景元素" in prompt
    assert "【角色身份唯一性】" in prompt
    assert "主人物立绘位于画面左侧主体区域，并在该区域内部居中完整站立" in prompt


def test_character_negative_prompt_allows_only_designated_info_card_text() -> None:
    merged = resolve_asset_negative_prompt("character", "不要其他人物")

    assert "多人" in merged
    assert "除指定人物资料卡外的其他文字" in merged
    assert "水印" in merged


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
    assert "视频末帧边界" not in prompt
    assert "木门刚被推开一条缝" not in prompt
    assert "运镜" not in prompt
    assert "缓慢推进" not in prompt
    assert "环境细节：旧木门与浮尘" in prompt
    assert "不做角色海报、概念插画封面或设定展示页构图。" in prompt
    assert "只出现本镜绑定的角色，不出现背景行人、看客、路人或模糊人影。" in prompt
    assert "negative prompt: 身份漂移、服装变化、文字水印" in prompt
    assert "画面比例：16:9。" in prompt
    assert "画面比例：9:16。" not in prompt
    assert "导演画面补充" not in prompt


def test_compile_keyframe_prompt_wide_shot_requires_readable_character_size() -> None:
    """全景/远景必须写明人物体量，避免人物被缩成小黑点。"""
    prompt = compile_keyframe_prompt(
        style_prompt="东方奇幻二维动画",
        aspect_ratio="16:9",
        image_prompt="万灯阁第七层夜巡开场全景。",
        anchors=[],
        shot_context={
            "opening_frame": "徐尘心左手提旧铜油壶，右手微抬但尚未触及命灯。",
            "characters": ["徐尘心"],
            "scene_name": "万灯阁第七层",
            "shot_type": "全景",
            "camera_angle": "略高于人物视线，轻微下压",
            "focus": "徐尘心缓步前行并准备扶正命灯",
        },
    )

    assert "约占画面高度的1/5至1/4" in prompt
    assert "不得缩成远景小黑点" in prompt


def test_compile_keyframe_prompt_close_shot_omits_wide_body_rule() -> None:
    """特写等近距离景别不追加全景人物体量约束。"""
    prompt = compile_keyframe_prompt(
        style_prompt="东方奇幻二维动画",
        aspect_ratio="16:9",
        image_prompt="特写：指尖触向灯身。",
        anchors=[],
        shot_context={
            "opening_frame": "指尖尚未贴到灯身。",
            "characters": ["徐尘心"],
            "shot_type": "特写",
        },
    )

    assert "1/5至1/4" not in prompt


def test_compile_keyframe_prompt_empty_environment_details_has_no_redundant_suffix() -> None:
    """environment_details 为空时不得拼出「环境细节场景固定环境细节」。"""
    prompt = compile_keyframe_prompt(
        style_prompt="东方奇幻二维动画",
        aspect_ratio="16:9",
        image_prompt="徐尘心行走于灯架甬道。",
        anchors=[],
        shot_context={
            "opening_frame": "右手微抬但尚未触及灯盏。",
            "characters": ["徐尘心"],
            "environment_details": "",
        },
    )

    assert "环境细节场景固定环境细节" not in prompt
    assert "环境细节" not in prompt
    assert "统一色调。" in prompt


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

    assert "【角色锁定】" in prompt
    assert "@Image2：林雪，锁定角色身份与服装。" in prompt
    assert "【物品锁定】" in prompt
    assert "铜制罗盘" in prompt
    assert "【场景锁定】" in prompt
    assert "古寺偏殿" in prompt
    assert "【本镜唯一开始状态】" in prompt
    assert "@Image1：当前分镜关键帧" in prompt
    assert "【核心动作】" in prompt
    assert "林雪缓慢推开木门，门缝透出暖光。" in prompt
    assert "【镜头】" in prompt
    assert "【声音】" in prompt
    assert "【结束状态】" in prompt
    assert "木门打开一条缝，林雪停住。" in prompt
    assert "【禁止项】" in prompt
    assert "身份漂移、道具穿模、文字水印" in prompt
    assert "画面比例：16:9，总时长：6秒。" in prompt
    # 验证新模板的核心特征
    assert "严格保持每位角色的身份、面部特征、发型、服装、体型、左右位置关系不变" in prompt
    assert "重点道具的外观、尺寸、颜色和材质保持一致" in prompt
    assert "枝叶可受风力影响产生幅度克制、方向一致的自然摆动" in prompt
    assert "不得在动画中移动、旋转、重构或重新生成" in prompt


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


def test_compile_video_prompt_keeps_voice_over_out_of_spoken_video_prompt() -> None:
    """录音/旁白不进入视频模型的画内对白指令。"""
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

    assert "本镜没有画内对白" in prompt
    assert "所有人物全程闭口" in prompt
    assert "怀表留给你" not in prompt
    assert "环境与动作声音" in prompt


def test_compile_video_prompt_binds_each_dialogue_to_structured_speaker() -> None:
    """视频时间轴中的自由文本不能覆盖结构化对白的角色绑定。"""
    context = shot_template_context(
        {
            "dialogue": [
                {"speaker": "苏瑶", "text": "你只是送外卖的，也配进这里？"},
                {"speaker": "顾沉舟", "text": "我是来送餐的。"},
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
        video_prompt=(
            "0-2秒：苏瑶站在门口；2-7秒：她开口说出台词“我是来送餐的。”；"
            "7-10秒：顾沉舟平静地回答“你只是送外卖的，也配进这里？”。"
        ),
        duration_sec=10,
        anchors=["@Image1：当前分镜关键帧。"],
        shot_context=context,
    )

    assert "第1句：苏瑶说：“你只是送外卖的，也配进这里？”" in prompt
    assert "第2句：顾沉舟说：“我是来送餐的。”" in prompt
    assert "按第1句对白执行对应口型" in prompt
    assert "按第2句对白执行对应口型" in prompt
    assert "2-7秒：她开口说出台词" not in prompt
    assert "7-10秒：顾沉舟平静地回答" not in prompt


def test_compile_video_prompt_binds_quote_text_when_speaker_order_is_swapped() -> None:
    """时间轴调整了说话顺序时，口型编号必须跟随引号原文而不是出现顺序。"""
    context = shot_template_context(
        {
            "dialogue": [
                {"speaker": "苏瑶", "text": "你只是送外卖的，也配进这里？"},
                {"speaker": "顾沉舟", "text": "我是来送餐的。"},
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
        # 先说顾沉舟（数组里的第 2 句），再说苏瑶（第 1 句）
        video_prompt=(
            "0-4秒：顾沉舟开口说出台词“我是来送餐的。”；"
            "4-10秒：苏瑶质问“你只是送外卖的，也配进这里？”。"
        ),
        duration_sec=10,
        anchors=["@Image1：当前分镜关键帧。"],
        shot_context=context,
    )

    # 口型编号必须出现在各自引号原文所在的时间段里（先“我是来送餐的”→第2句）
    assert "按第2句对白执行对应口型" in prompt
    assert "按第1句对白执行对应口型" in prompt
    assert prompt.index("按第2句对白执行对应口型") < prompt.index(
        "按第1句对白执行对应口型"
    )


def test_compile_video_prompt_removes_narration_from_action_timeline() -> None:
    """动作描述旁白不得作为视频模型的可朗读内容。"""
    context = shot_template_context(
        {
            "narration": "他未发一言，打开了外卖箱。",
            "dialogue": [],
        },
        {},
    )

    prompt = compile_video_prompt(
        style_prompt="都市二维国漫",
        aspect_ratio="16:9",
        video_prompt=(
            "0-2秒：顾沉舟弯腰取下身侧的外卖箱；2-4秒：他拨开箱盖锁扣；"
            "旁白：“他未发一言，打开了外卖箱。”"
        ),
        duration_sec=4,
        anchors=[],
        shot_context=context,
    )

    assert "顾沉舟弯腰取下身侧的外卖箱" in prompt
    assert "旁白：“他未发一言，打开了外卖箱。”" not in prompt
    assert "他未发一言，打开了外卖箱" not in prompt
    assert "本镜没有画内对白" in prompt


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
