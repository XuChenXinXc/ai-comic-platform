"""素材设定图提示词模板渲染器。

将 ``backend/app/prompts/templates/`` 下的设定图模板渲染为最终提示词正文。

模板语法（极简）：
- ``{slot}``：占位符，渲染时替换为 context 中同名槽位的值；
  模板引用了 context 中不存在的槽位时抛 ``ValueError``。
- ``[[if:slot]]…[[/if:slot]]``：条件块，slot 的值为空（空串或假值）时整块删除；
  支持跨行与段落内嵌，不支持嵌套。
- 段落之间以空行分隔，输出以两个换行符连接，首尾空白被清理。

槽位值在渲染前由 ``*_context`` 函数基于素材档案（profile）计算完成：
列表拼接、字段别名与 fallback、场景人物标记过滤等逻辑复用
``prompt_service`` 的既有工具函数，保证与内置模板函数输出一致。
"""

import re
from pathlib import Path
from typing import Any

from app.services.prompt_service import (
    _asset_detail,
    _expression_items,
    _profile_value,
    _scene_environment_text,
    _scene_profile_value,
)

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "prompts" / "templates"

_SLOT_PATTERN = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_IF_PATTERN = re.compile(
    r"\[\[if:([A-Za-z_][A-Za-z0-9_]*)\]\](.*?)\[\[/if:\1\]\]",
    re.DOTALL,
)


def render_template(template_text: str, context: dict[str, Any]) -> str:
    """渲染模板文本：条件块 → 占位符 → 分段清理。"""
    template_text = template_text.replace("\r\n", "\n")

    def conditional(match: re.Match[str]) -> str:
        slot, body = match.group(1), match.group(2)
        value = context.get(slot)
        if isinstance(value, str):
            keep = bool(value.strip())
        else:
            keep = bool(value)
        return body if keep else ""

    template_text = _IF_PATTERN.sub(conditional, template_text)

    missing: list[str] = []

    def fill(match: re.Match[str]) -> str:
        slot = match.group(1)
        if slot not in context:
            missing.append(slot)
            return ""
        return str(context.get(slot) or "")

    template_text = _SLOT_PATTERN.sub(fill, template_text)
    if missing:
        raise ValueError(f"模板引用了未提供的槽位：{sorted(set(missing))}")

    paragraphs = [part.strip() for part in template_text.split("\n\n")]
    return "\n\n".join(part for part in paragraphs if part)


def _character_context(
    *,
    name: str,
    profile: dict[str, Any],
    detail: str,
    consistency_keywords: list[str],
    composition: str,
    negative_prompt: str,
) -> dict[str, Any]:
    """角色设定图槽位：与 prompt_service._character_setting_sheet 取值逻辑一致。"""
    concept = _profile_value(profile, "concept", "identity", fallback="核心角色")
    age = _profile_value(profile, "age", fallback="年龄以素材资料为准")
    gender = _profile_value(profile, "gender", fallback="性别以素材资料为准")
    identity = _profile_value(profile, "identity", fallback="核心角色")
    worldview = _profile_value(profile, "worldview", "world", fallback="本项目世界观")
    personality = _profile_value(profile, "personality", fallback="以素材资料为准")
    core = _profile_value(profile, "core", "concept", fallback=identity)
    appearance = _profile_value(profile, "appearance", "face", fallback="脸部特征以素材资料为准")
    hair = _profile_value(profile, "hair", fallback="发型以素材资料为准")
    eyes = _profile_value(profile, "eyes", "appearance", fallback="眼睛特征以素材资料为准")
    normal_expression = _profile_value(
        profile,
        "normal_expression",
        "acting_traits",
        fallback="自然克制",
    )
    body = _profile_value(profile, "body", fallback="身形以素材资料为准")
    immutable = _profile_value(profile, "immutable_features", fallback="固定识别特征以素材资料为准")
    costume = _profile_value(profile, "costume", fallback="服装以素材资料为准")
    main_color = _profile_value(profile, "main_color", "color", fallback="服装主色以素材资料为准")
    accent_color = _profile_value(profile, "accent_color", fallback="服装点缀色以素材资料为准")
    material = _profile_value(
        profile, "costume_material", "material", fallback="服装材质以素材资料为准"
    )
    upper = _profile_value(profile, "upper", "top", fallback=costume)
    lower = _profile_value(profile, "lower", "bottom", fallback=costume)
    outer = _profile_value(profile, "outer", "outerwear", fallback=costume)
    shoes = _profile_value(profile, "shoes", "footwear", fallback=costume)
    accessories = _profile_value(profile, "accessories", fallback=immutable)
    expression_group = _profile_value(
        profile,
        "expression_group",
        "acting_traits",
        fallback=normal_expression,
    )
    expression_items = _expression_items(
        profile.get("expression_group") or profile.get("acting_traits")
    )
    expression_count = len(expression_items) or 3
    anchors = "、".join(consistency_keywords)
    detail_clean = detail.rstrip("。") if detail else detail
    has_detail = bool(detail_clean) and detail_clean != "以素材资料保持稳定一致"
    return {
        "name": name,
        "concept": concept,
        "age": age,
        "gender": gender,
        "identity": identity,
        "worldview": worldview,
        "personality": personality,
        "core": core,
        "appearance": appearance,
        "hair": hair,
        "eyes": eyes,
        "normal_expression": normal_expression,
        "body": body,
        "immutable": immutable,
        "costume": costume,
        "main_color": main_color,
        "accent_color": accent_color,
        "material": material,
        "upper": upper,
        "lower": lower,
        "outer": outer,
        "shoes": shoes,
        "accessories": accessories,
        "expression_group": expression_group,
        "expression_count": expression_count,
        "anchors": anchors,
        "detail": detail_clean if has_detail else "",
        "composition": composition.rstrip("。") if composition else "",
        "negative_prompt": negative_prompt,
    }


def _prop_context(
    *,
    name: str,
    profile: dict[str, Any],
    detail: str,
    consistency_keywords: list[str],
    composition: str,
    negative_prompt: str,
) -> dict[str, Any]:
    """物品设定图槽位：与 prompt_service._prop_setting_sheet 取值逻辑一致。"""
    concept = _profile_value(profile, "concept", "description", fallback="剧情道具")
    worldview = _profile_value(profile, "worldview", "world", fallback="本项目世界观")
    category = _profile_value(profile, "category", fallback="剧情道具")
    owner = _profile_value(profile, "owner", "user", fallback="剧情角色")
    plot_function = _profile_value(profile, "plot_function", fallback="推动当前剧情")
    description = _profile_value(profile, "description", fallback="外观以素材资料为准")
    silhouette = _profile_value(
        profile, "silhouette", "description", fallback="整体轮廓以素材资料为准"
    )
    dimensions = _profile_value(profile, "dimensions", "size", fallback="与使用场景相符")
    weight = _profile_value(profile, "weight", fallback="符合材质与结构的重量感")
    special_features = _profile_value(
        profile, "special_features", fallback="关键结构以素材资料为准"
    )
    front = _profile_value(profile, "front_structure", "description", fallback=description)
    side = _profile_value(profile, "side_structure", "description", fallback=description)
    back = _profile_value(profile, "back_structure", "description", fallback=description)
    other = _profile_value(profile, "other_structure", fallback=special_features)
    key_position = _profile_value(profile, "key_structure_position", fallback=special_features)
    material = _profile_value(profile, "material", fallback="主体材质以素材资料为准")
    secondary_material = _profile_value(
        profile, "secondary_material", fallback="辅助材质以素材资料为准"
    )
    color = _profile_value(profile, "color", "main_color", fallback="主色以素材资料为准")
    accent_color = _profile_value(profile, "accent_color", fallback="点缀色以素材资料为准")
    surface = _profile_value(profile, "surface_texture", fallback="表面质感以素材资料为准")
    wear = _profile_value(profile, "wear", "usage_marks", fallback="固定使用痕迹以素材资料为准")
    detail_zone = _profile_value(profile, "detail_zone", fallback=special_features)
    scale = _profile_value(
        profile, "scale_reference", fallback="与成年人手掌的比例以素材资料为准"
    )
    anchors = "、".join(consistency_keywords)
    detail_clean = detail.rstrip("。") if detail else detail
    has_detail = bool(detail_clean) and detail_clean != "以素材资料保持稳定一致"
    return {
        "name": name,
        "concept": concept,
        "worldview": worldview,
        "category": category,
        "owner": owner,
        "plot_function": plot_function,
        "silhouette": silhouette,
        "dimensions": dimensions,
        "weight": weight,
        "special_features": special_features,
        "front": front,
        "side": side,
        "back": back,
        "other": other,
        "key_position": key_position,
        "material": material,
        "secondary_material": secondary_material,
        "color": color,
        "accent_color": accent_color,
        "surface": surface,
        "wear": wear,
        "detail_zone": detail_zone,
        "scale": scale,
        "anchors": anchors,
        "detail": detail_clean if has_detail else "",
        "composition": composition.rstrip("。") if composition else "",
        "negative_prompt": negative_prompt,
    }


def _scene_context(
    *,
    name: str,
    profile: dict[str, Any],
    detail: str,
    consistency_keywords: list[str],
    composition: str,
    negative_prompt: str,
) -> dict[str, Any]:
    """场景设定图槽位：与 prompt_service._scene_setting_sheet 取值逻辑一致。"""
    worldview = _scene_profile_value(profile, "worldview", "world", fallback="本项目世界观")
    place_type = _scene_profile_value(profile, "place_type", "indoor_outdoor", fallback="剧情地点")
    layout = _scene_profile_value(
        profile,
        "space_keywords",
        "layout",
        fallback="空间布局以素材资料为准",
    )
    scale = _scene_profile_value(
        profile, "scale", "shape_scale", fallback="形状与尺度以素材资料为准"
    )
    furnishings = _scene_profile_value(profile, "furnishings", fallback="固定陈设以素材资料为准")
    architecture = _scene_profile_value(
        profile, "architecture", fallback="建筑/室内风格以素材资料为准"
    )
    regions = _scene_profile_value(profile, "regions", "layout", fallback=layout)
    entrance = _scene_profile_value(profile, "entrance_position", fallback="以主视角为准")
    windows = _scene_profile_value(profile, "window_position", fallback="以主视角为准")
    movement = _scene_profile_value(profile, "movement_path", fallback="遵循主要区域动线")
    wall_material = _scene_profile_value(profile, "wall_material", fallback=architecture)
    floor_material = _scene_profile_value(profile, "floor_material", fallback=architecture)
    furniture_material = _scene_profile_value(profile, "furniture_material", fallback=architecture)
    glass_metal = _scene_profile_value(
        profile, "glass_metal_material", fallback="按场景资料保持一致"
    )
    special_material = _scene_profile_value(profile, "special_material", fallback="无额外特殊材质")
    color_mood = _scene_profile_value(profile, "color_mood", fallback="主色与点缀色以素材资料为准")
    main_color = _scene_profile_value(profile, "main_color", "color_mood", fallback=color_mood)
    accent_color = _scene_profile_value(profile, "accent_color", fallback="以场景资料为准")
    # 视觉语义重解释治理（Phase 2）：场景只陈述受控事实（时间/天气/光源方向/固有色），
    # 不输出主辅光效果、氛围或色彩气质等自由艺术指导，表达方式由项目统一画风决定。
    time_of_day = _scene_profile_value(profile, "time_of_day", fallback="昼夜时间以场景资料为准")
    weather = _scene_profile_value(profile, "weather", fallback="天气以场景资料为准")
    light_direction = _scene_profile_value(
        profile, "light_direction", "lighting", "main_light", fallback="光源方向以场景资料为准"
    )
    main_view = _scene_profile_value(profile, "main_view", fallback="主场景透视图（无人空镜）")
    standard_view_1 = _scene_profile_value(
        profile, "standard_view_1", fallback="正向全景（无人空镜）"
    )
    standard_view_2 = _scene_profile_value(
        profile, "standard_view_2", fallback="反向或侧向全景（无人空镜）"
    )
    detail_zone = _scene_profile_value(profile, "detail_zone", fallback=furnishings)
    environment_detail = _scene_environment_text(detail, "按上述资料补充环境细节")
    environment_composition = _scene_environment_text(composition, "")
    environment_keywords = []
    for keyword in consistency_keywords:
        environment_keyword = _scene_environment_text(keyword, "")
        if environment_keyword:
            environment_keywords.append(environment_keyword)
    anchors = "、".join(environment_keywords)
    return {
        "name": name,
        "worldview": worldview,
        "place_type": place_type,
        "layout": layout,
        "scale": scale,
        "furnishings": furnishings,
        "architecture": architecture,
        "regions": regions,
        "entrance": entrance,
        "windows": windows,
        "movement": movement,
        "wall_material": wall_material,
        "floor_material": floor_material,
        "furniture_material": furniture_material,
        "glass_metal": glass_metal,
        "special_material": special_material,
        "main_color": main_color,
        "accent_color": accent_color,
        "time_of_day": time_of_day,
        "weather": weather,
        "light_direction": light_direction,
        "main_view": main_view,
        "standard_view_1": standard_view_1,
        "standard_view_2": standard_view_2,
        "detail_zone": detail_zone,
        "environment_detail": environment_detail,
        "anchors": anchors,
        "composition": environment_composition.rstrip("。") if environment_composition else "",
        "negative_prompt": negative_prompt,
    }


def render_asset_body(
    *,
    asset_type: str,
    asset_name: str,
    profile: dict[str, Any],
    prompt: str,
    negative_prompt: str,
    consistency_keywords: list[str],
    composition: str,
) -> str:
    """按素材类型读取设定图模板并渲染正文。

    ``negative_prompt`` 应为已合并默认负向的最终值。
    未知素材类型抛 ``ValueError``，由调用方走内置兜底路径。
    """
    detail = _asset_detail(prompt, profile)
    context_builders = {
        "character": _character_context,
        "prop": _prop_context,
        "scene": _scene_context,
    }
    builder = context_builders.get(asset_type)
    if builder is None:
        raise ValueError(f"没有可用的设定图模板：{asset_type}")
    context = builder(
        name=asset_name,
        profile=profile,
        detail=detail,
        consistency_keywords=consistency_keywords,
        composition=composition,
        negative_prompt=negative_prompt,
    )
    template_path = TEMPLATE_DIR / f"{asset_type}_sheet.md"
    template_text = template_path.read_text(encoding="utf-8")
    return render_template(template_text, context)
