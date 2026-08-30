"""模板渲染器测试：落盘模板渲染结果必须与内置模板函数逐字一致。"""

from __future__ import annotations

import pytest

from app.services.prompt_service import compile_asset_prompt
from app.services.prompt_template_renderer import (
    TEMPLATE_DIR,
    render_asset_body,
    render_template,
)

STYLE = "东方奇幻二维动画"


def _compile(asset_type: str, *, profile: dict, **overrides: object) -> str:
    """按素材类型编译提示词，模板路径（use_template=True）。"""
    defaults: dict[str, object] = dict(
        style_prompt=STYLE,
        aspect_ratio="16:9",
        asset_type=asset_type,
        asset_name="测试素材",
        profile=profile,
        prompt="补充的视觉细节",
        negative_prompt="文字水印，真人摄影",
        consistency_keywords=["固定服装", "固定发型"],
        composition="主体清晰居中",
        reference_mode=False,
    )
    defaults.update(overrides)
    return compile_asset_prompt(**defaults)


def _character_profile() -> dict:
    return {
        "age": "二十岁上下",
        "gender": "男",
        "identity": "万灯阁守灯弟子",
        "concept": "守灯人",
        "worldview": "东方奇幻修真",
        "personality": "沉默执拗",
        "appearance": "清瘦青年，眉目疏朗",
        "hair": "黑色低马尾",
        "eyes": "深灰瞳孔",
        "normal_expression": "平静克制",
        "body": "清瘦挺拔",
        "immutable_features": ["黑色低马尾", "左眉一道旧疤"],
        "costume": "青灰交领粗布内袍",
        "main_color": "青灰",
        "accent_color": "暗金",
        "costume_material": "粗麻布",
        "upper": "交领内袍",
        "lower": "深灰长裤",
        "outer": "无",
        "shoes": "黑色布鞋",
        "accessories": "腰间旧铜铃",
        "style_tone": "沉静疏离",
        "expression_group": ["平静倾听", "惊讶屏息", "执拗追问", "沉默悲伤"],
    }


def _prop_profile() -> dict:
    return {
        "concept": "守灯油壶",
        "category": "随身道具",
        "owner": "徐尘心",
        "plot_function": "为命灯添油",
        "description": "旧铜油壶，壶身錾星纹",
        "silhouette": "圆腹细颈",
        "dimensions": "一掌高",
        "weight": "沉手",
        "special_features": "壶盖缺一角",
        "front_structure": "圆腹錾星纹",
        "side_structure": "细颈单耳",
        "back_structure": "光面旧铜",
        "other_structure": "壶底平",
        "key_structure_position": "壶颈处",
        "material": "黄铜",
        "secondary_material": "锡",
        "color": "暗黄铜色",
        "accent_color": "氧化青绿",
        "surface_texture": "哑光带包浆",
        "wear": "壶底三道浅划痕",
        "detail_zone": "壶盖缺口、壶颈接缝、壶底划痕",
        "scale_reference": "与成年人手掌等高",
    }


def _scene_profile() -> dict:
    return {
        "concept": "林舟父亲留下的钟表店",
        "worldview": "当代都市",
        "place_type": "街边钟表店",
        "architecture": "老旧钟表店内部",
        "space_keywords": ["入口正对柜台", "后墙挂钟"],
        "shape_scale": "单开间小店",
        "regions": ["入口区", "柜台区"],
        "entrance_position": "东侧入口",
        "window_position": "西侧两扇木窗",
        "movement_path": "入口直通柜台",
        "wall_material": "旧木墙板",
        "floor_material": "深色木地板",
        "furniture_material": "老榆木",
        "glass_metal_material": "黄铜件",
        "special_material": "无",
        "main_color": "青灰",
        "accent_color": "暖金",
        "color_quality": "陈旧克制",
        "main_view": "镜头从入口向内，正对木柜台",
        "standard_view_1": "从柜台看入口",
        "standard_view_2": "从入口看柜台",
        "detail_zone": ["柜台暗格", "挂钟墙", "地面"],
        "main_light": "暖金顶灯",
        "fill_light": "烛光",
        "atmosphere": "静谧怀旧",
        "furnishings": ["木柜台", "挂钟墙"],
        "layout": "入口正对木柜台，后墙挂满旧钟",
    }


@pytest.mark.parametrize(
    ("asset_type", "profile", "aspect_ratio"),
    [
        ("character", _character_profile(), "16:9"),
        ("character", {"gender": "男"}, "9:16"),
        ("prop", _prop_profile(), "16:9"),
        ("prop", {"description": "刻有星纹的铜制罗盘"}, "9:16"),
        ("scene", _scene_profile(), "16:9"),
        ("scene", {"architecture": "山中古寺偏殿"}, "9:16"),
    ],
)
def test_模板渲染与内置函数输出逐字一致(
    asset_type: str,
    profile: dict,
    aspect_ratio: str,
) -> None:
    templated = _compile(asset_type, profile=profile, aspect_ratio=aspect_ratio)
    builtin = _compile(asset_type, profile=profile, aspect_ratio=aspect_ratio, use_template=False)

    assert templated == builtin
    assert templated
    assert "{" not in templated


def test_模板渲染_空提示词与空负向时与内置一致() -> None:
    """prompt/负向/一致性关键词/构图全空时走兜底与默认负向，两路径仍一致。"""
    profile = {"gender": "女", "identity": "客栈老板娘"}
    templated = _compile(
        "character",
        profile=profile,
        prompt="",
        negative_prompt="",
        consistency_keywords=[],
        composition="",
    )
    builtin = _compile(
        "character",
        profile=profile,
        prompt="",
        negative_prompt="",
        consistency_keywords=[],
        composition="",
        use_template=False,
    )

    assert templated == builtin
    assert "negative prompt: " in templated


def test_场景模板_过滤人物标记与内置一致() -> None:
    """场景档案混入人物姓名与动作时，过滤逻辑在模板路径同样生效。"""
    profile = _scene_profile()
    profile["character_position"] = "林舟站在柜台前打开暗格"
    profile["main_view"] = "人物站在柜台前，镜头从入口向内"
    templated = _compile(
        "scene",
        profile=profile,
        prompt="钟表店内设有儿童阅读区。两个人影站在柜台前。手电光扫过挂钟墙。",
        negative_prompt="现代装修",
        consistency_keywords=["木柜台", "挂钟墙"],
        composition="人物位于画面中央",
    )
    builtin = _compile(
        "scene",
        profile=profile,
        prompt="钟表店内设有儿童阅读区。两个人影站在柜台前。手电光扫过挂钟墙。",
        negative_prompt="现代装修",
        consistency_keywords=["木柜台", "挂钟墙"],
        composition="人物位于画面中央",
        use_template=False,
    )

    assert templated == builtin
    assert "林舟" not in templated
    assert "两个人影" not in templated
    assert "人物位于画面中央" not in templated


def test_未知素材类型_走内置兜底路径() -> None:
    prompt = compile_asset_prompt(
        style_prompt=STYLE,
        aspect_ratio="16:9",
        asset_type="unknown",
        asset_name="未知素材",
        profile={},
        prompt="补充细节",
        negative_prompt="",
        consistency_keywords=[],
        composition="",
    )

    assert "补充细节" in prompt
    assert "negative prompt: " in prompt


def test_渲染条件块_空值删除与非空保留() -> None:
    template = "第一段[[if:detail]]细节：{detail}。[[/if:detail]]\n\n固定段"

    assert render_template(template, {"detail": ""}) == "第一段\n\n固定段"
    assert render_template(template, {"detail": "补充"}) == "第一段细节：补充。\n\n固定段"


def test_渲染条件块_跨行内容整体删除() -> None:
    template = "头部\n\n[[if:detail]]多行\n细节内容\n{detail}[[/if:detail]]\n\n尾部"

    rendered = render_template(template, {"detail": ""})

    assert rendered == "头部\n\n尾部"


def test_渲染_引用未提供槽位时报错() -> None:
    with pytest.raises(ValueError, match="missing"):
        render_template("内容{missing}槽位", {})


def test_模板文件_三类型均存在且可渲染() -> None:
    for asset_type in ("character", "prop", "scene"):
        path = TEMPLATE_DIR / f"{asset_type}_sheet.md"
        assert path.exists(), f"缺少模板文件：{path}"
        text = path.read_text(encoding="utf-8")
        # 槽位声明必须存在，渲染由上下文填充
        assert "{name}" in text
        assert "[[if:" in text or asset_type == "character"
        body = render_asset_body(
            asset_type=asset_type,
            asset_name="测试",
            profile={},
            prompt="补充细节",
            negative_prompt="文字",
            consistency_keywords=["锚点"],
            composition="构图",
        )
        assert body
