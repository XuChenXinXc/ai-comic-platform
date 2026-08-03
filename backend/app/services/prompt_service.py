import re
from collections.abc import Iterable
from typing import Any

STYLE_ANCHOR = "[STYLE_ANCHOR]"
VISUAL_PROMPT_TEMPLATE_VERSION = "visual-prompt-v2.1"
_FIXED_VISUAL_ASPECT_RATIOS = {
    "character": "3:2",
    "prop": "3:2",
    "scene": "16:9",
    "keyframe": "16:9",
    "video": "16:9",
}
_TIMELINE_PATTERN = re.compile(r"(?:^|[；;\n])\s*\d+(?:\.\d+)?\s*[-–—~－至]\s*\d+(?:\.\d+)?\s*秒")
_SCENE_CLAUSE_PATTERN = re.compile(r"[^。！？；;\n]+[。！？；;]?\s*")
_SCENE_PERSON_MARKERS = (
    "人物",
    "角色",
    "人群",
    "路人",
    "行人",
    "人影",
    "身影",
    "人形",
    "人体",
)
_SCENE_PERSON_ACTION_MARKERS = (
    "站在",
    "坐在",
    "走向",
    "走入",
    "走进",
    "走出",
    "跑向",
    "跑入",
    "跑进",
    "跑出",
    "躺在",
    "蹲在",
    "看向",
    "望向",
    "手持",
    "拿着",
    "握着",
    "穿着",
    "进入",
    "离开",
    "推开",
    "拉开",
    "打开",
    "关上",
    "转身",
    "抬手",
    "低头",
    "抬头",
)
_SCENE_NON_ENVIRONMENT_MARKERS = (*_SCENE_PERSON_MARKERS, *_SCENE_PERSON_ACTION_MARKERS)
_SCENE_PERSON_OWNED_OBJECT_PATTERN = re.compile(
    r"[\u4e00-\u9fff]{2,4}的(?:手机|手电筒?|随身物|衣物|服装|外套|背包|提包|公文包|文件|道具|武器)"
)
_SCENE_REQUIRED_NEGATIVE_PROMPT = (
    "人物、角色、人群、路人、行人、人影、人形、剪影、倒影中的人物、镜中人物、"
    "照片或海报中的人物、人体局部"
)

_SAFETY_REPLACEMENTS = {
    "血肉模糊": "受伤后的非血腥画面",
    "肢解": "以遮挡和画外结果表现严重冲突",
    "斩首": "以剪影表现致命冲突",
    "爆头": "强烈能量冲击后倒下",
    "内脏": "受损痕迹",
    "色情": "克制的情感表达",
    "全裸": "穿着得体服装",
}


def join_non_empty(parts: Iterable[Any]) -> str:
    return "\n".join(str(part).strip() for part in parts if str(part or "").strip())


def resolve_visual_aspect_ratio(target_type: str, fallback: str) -> str:
    return _FIXED_VISUAL_ASPECT_RATIOS.get(target_type, fallback)


def style_anchor(
    style_prompt: str,
    aspect_ratio: str,
    novel_style_bridge: str = "",
) -> str:
    return join_non_empty(
        [
            "画面美术风格：",
            f"{STYLE_ANCHOR} 核心主题: {style_prompt.strip() or '项目既定动画风格'}",
            "统一画风锁定：角色图、场景图、物品图、分镜图与视频画面使用同一动画/"
            "插画管线，不是真人摄影、产品摄影或3D建模渲染。",
            f"小说风格桥接：{novel_style_bridge.strip()}。" if novel_style_bridge.strip() else "",
            "统一视觉规范：线条、上色、阴影、材质、色彩、光影与写实度保持一致。",
            f"画面比例：{aspect_ratio}。",
        ]
    )


def apply_content_safety(prompt: str) -> str:
    compiled = prompt
    for unsafe, replacement in _SAFETY_REPLACEMENTS.items():
        compiled = compiled.replace(unsafe, replacement)
    return compiled


def resolve_keyframe_description(image_prompt: str, frame_description: str) -> str:
    return image_prompt.strip() or frame_description.strip()


def resolve_video_description(
    *,
    video_prompt: str,
    start_state: str,
    action: str,
    end_state: str,
    camera_move: str,
    duration_sec: float,
) -> str:
    if video_prompt.strip():
        return video_prompt.strip()
    if not any(value.strip() for value in (start_state, action, end_state)):
        return ""
    return join_non_empty(
        [
            f"开场状态：{start_state}" if start_state.strip() else "",
            f"主要动作：{action}" if action.strip() else "",
            f"结束状态：{end_state}" if end_state.strip() else "",
            f"运镜：{camera_move}" if camera_move.strip() else "",
            f"镜头时长：{duration_sec:g} 秒",
        ]
    )


def _value(value: Any, fallback: str = "以素材资料与补充描述为准") -> str:
    if isinstance(value, list):
        result = "、".join(str(item).strip() for item in value if str(item).strip())
        return result or fallback
    result = str(value or "").strip()
    return result or fallback


def _profile_value(
    profile: dict[str, Any],
    *keys: str,
    fallback: str = "以素材资料与补充描述为准",
) -> str:
    for key in keys:
        if key in profile and _value(profile[key], ""):
            return _value(profile[key], "")
    return fallback


def _asset_detail(prompt: str, profile: dict[str, Any]) -> str:
    if prompt.strip():
        return prompt.strip()
    values = [_value(value, "") for value in profile.values() if _value(value, "")]
    return "；".join(values) or "以素材资料保持稳定一致"


def _scene_environment_text(value: Any, fallback: str) -> str:
    if isinstance(value, list):
        items = [_scene_environment_text(item, "") for item in value]
        return "、".join(item for item in items if item) or fallback
    text = _value(value, "")
    if not text:
        return fallback
    clauses = _SCENE_CLAUSE_PATTERN.findall(text)
    filtered = [
        clause.strip()
        for clause in clauses
        if not any(marker in clause for marker in _SCENE_NON_ENVIRONMENT_MARKERS)
        and _SCENE_PERSON_OWNED_OBJECT_PATTERN.search(clause) is None
    ]
    return "".join(filtered).strip() or fallback


def _scene_profile_value(
    profile: dict[str, Any],
    *keys: str,
    fallback: str,
) -> str:
    for key in keys:
        if key in profile:
            value = _scene_environment_text(profile[key], "")
            if value:
                return value
    return fallback


def resolve_asset_negative_prompt(asset_type: str, negative_prompt: str) -> str:
    defaults = {
        "character": "多人、身份漂移、脸型变化、发型变化、服装不一致、畸形手指、"
        "文字、水印、真人摄影、产品摄影、3D建模渲染",
        "prop": "完整人物、人物脸、多件物品、结构错误、比例错误、材质漂移、"
        "文字、水印、真人摄影、产品摄影、3D建模渲染",
        "scene": f"{_SCENE_REQUIRED_NEGATIVE_PROMPT}、空间结构矛盾、门窗家具漂移、"
        "视角冲突、文字、水印、真实建筑摄影、产品摄影、3D建模渲染",
    }
    default_prompt = defaults.get(
        asset_type,
        "文字、水印、真人摄影、产品摄影、3D建模渲染",
    )
    custom_prompt = negative_prompt.strip()
    if asset_type == "scene" and custom_prompt:
        if default_prompt in custom_prompt:
            return custom_prompt
        return f"{custom_prompt}、{default_prompt}"
    return custom_prompt or default_prompt


def _character_setting_sheet(
    *,
    name: str,
    profile: dict[str, Any],
    detail: str,
    consistency_keywords: list[str],
    composition: str,
    negative_prompt: str,
) -> str:
    concept = _profile_value(profile, "concept", "identity", fallback="核心角色")
    age = _profile_value(profile, "age", fallback="年龄以素材资料为准")
    gender = _profile_value(profile, "gender", fallback="性别以素材资料为准")
    identity = _profile_value(profile, "identity", fallback="核心角色")
    worldview = _profile_value(profile, "worldview", "world", fallback="本项目世界观")
    personality = _profile_value(profile, "personality", fallback="以素材资料为准")
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
    acting_traits = _profile_value(
        profile,
        "acting_traits",
        "expression_group",
        "personality",
        fallback="自然克制",
    )
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
    props = _profile_value(profile, "carried_item", "core_prop", fallback=immutable)
    style_tone = _profile_value(profile, "style_tone", fallback=detail)
    expression_group = _profile_value(
        profile,
        "expression_group",
        "acting_traits",
        fallback=acting_traits,
    )
    lighting = _profile_value(profile, "lighting", fallback="标准角色设定柔光")
    atmosphere = _profile_value(profile, "atmosphere", fallback=style_tone)
    return join_non_empty(
        [
            f"《{name}・{concept} 角色基础设定稿》",
            "核心人物设定：",
            f"{name}，一位{age}的{gender}角色，身份为{identity}，生活在{worldview}。",
            f"性格关键词：{personality}。",
            f"外貌：{hair}；{appearance}；{eyes}；常态表情{normal_expression}；身形{body}。",
            f"核心视觉识别点：{immutable}。",
            "服饰与造型设定：",
            f"服装：{costume}；主色{main_color}；点缀色{accent_color}；材质{material}。",
            f"上装{upper}；下装{lower}；外搭{outer}；鞋履{shoes}；配饰{accessories}；核心道具{props}。",
            f"整体造型呈现{style_tone}。",
            "版面：",
            "纯白背景，3:2 横向角色设定面板；仅一名角色。",
            "左上人物资料卡；左侧 40%~45% 完整全身主立绘；"
            f"右上正面/侧面/背面三视图；右下三个核心表情特写：{expression_group}。",
            "所有模块必须为同一角色，脸型、发型、服装、配饰、道具完全一致。",
            "一致性锚点：" + "、".join(consistency_keywords) if consistency_keywords else "",
            f"构图补充：{composition}" if composition else "",
            f"{lighting}；主色{main_color}、点缀色{accent_color}；氛围{atmosphere}。",
            "高质量二维动画角色设定图；不是真人摄影。",
            f"negative prompt: {negative_prompt}" if negative_prompt else "",
        ]
    )


def _prop_setting_sheet(
    *,
    name: str,
    profile: dict[str, Any],
    detail: str,
    consistency_keywords: list[str],
    composition: str,
    negative_prompt: str,
) -> str:
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
    scale = _profile_value(profile, "scale_reference", fallback="与成年人手掌的比例以素材资料为准")
    return join_non_empty(
        [
            f"《{name}・{concept} 完整道具设定稿》",
            "核心物品设定：",
            f"{name}，属于{worldview}中的{category}，由{owner}使用，用于{plot_function}。",
            f"整体轮廓{silhouette}，尺寸约{dimensions}，重量感{weight}。",
            f"核心视觉识别点：{special_features}。",
            "外观结构与细节：",
            f"正面{front}；侧面{side}；背面{back}；顶部/底部/内部{other}；"
            f"按钮、接口、纹样、把手等位于{key_position}。",
            f"主体材质{material}；辅助材质{secondary_material}；主色{color}，"
            f"点缀色{accent_color}；表面质感{surface}；固定磨损{wear}。",
            f"补充细节：{detail}。",
            "版面：",
            "纯白背景，3:2 横向专业道具设定面板；只展示一件物品。",
            "左上资料卡；左侧 40%~45% 四十五度略俯视主展示图；"
            f"右上正面/侧面/背面/顶部或底部四视图；右侧中部三个细节特写：{detail_zone}；"
            f"右下尺寸标注及与成年人手掌的比例参考：{scale}。",
            "所有模块的形状、尺寸、颜色、材质、纹样、磨损和部件位置必须一致。",
            "一致性锚点：" + "、".join(consistency_keywords) if consistency_keywords else "",
            f"构图补充：{composition}" if composition else "",
            "中性影棚柔光；高质量动画道具设定图；不出现完整人物或人物脸。",
            f"negative prompt: {negative_prompt}" if negative_prompt else "",
        ]
    )


def _scene_setting_sheet(
    *,
    name: str,
    profile: dict[str, Any],
    detail: str,
    consistency_keywords: list[str],
    composition: str,
    negative_prompt: str,
) -> str:
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
    color_quality = _scene_profile_value(
        profile, "color_quality", "color_mood", fallback=color_mood
    )
    main_view = _scene_profile_value(profile, "main_view", fallback="主场景透视图（无人空镜）")
    standard_view_1 = _scene_profile_value(
        profile, "standard_view_1", fallback="正向全景（无人空镜）"
    )
    standard_view_2 = _scene_profile_value(
        profile, "standard_view_2", fallback="反向或侧向全景（无人空镜）"
    )
    detail_zone = _scene_profile_value(profile, "detail_zone", fallback=furnishings)
    main_light = _scene_profile_value(
        profile, "lighting", "main_light", fallback="主光源以场景资料为准"
    )
    fill_light = _scene_profile_value(profile, "fill_light", fallback="辅光以场景资料为准")
    atmosphere = _scene_profile_value(profile, "atmosphere", "color_mood", fallback=color_mood)
    environment_detail = _scene_environment_text(detail, "按上述资料补充环境细节")
    environment_composition = _scene_environment_text(composition, "")
    environment_keywords = []
    for keyword in consistency_keywords:
        environment_keyword = _scene_environment_text(keyword, "")
        if environment_keyword:
            environment_keywords.append(environment_keyword)
    return join_non_empty(
        [
            f"《{name}・完整场景设定稿》",
            "【最高优先级：无人纯背景空镜】",
            "这是供后续分镜复用的纯环境场景资产，不是剧情分镜。所有视图必须为空场景，"
            "禁止出现人物、人群、路人、行人、人影、人形、剪影、倒影或镜面中的人物以及人体局部。",
            "下方资料中出现的任何人物姓名、人物动作、角色站位、人物视角或人物持有物，"
            "都只能用于理解空间尺度和动线，不得成为画面内容。",
            "核心场景设定：",
            f"{name}，位于{worldview}中的{place_type}。",
            f"空间关键词：{layout}；空间规模{scale}；建筑/室内风格{architecture}。",
            f"核心视觉识别点：{furnishings}。",
            "空间结构与区域关系：",
            f"以入口、主要空间轴线与主场景透视方向为统一方向基准。主要区域：{regions}。",
            f"入口{entrance}；窗户/自然光入口{windows}；"
            f"表演活动区按主要区域和动线预留为空置空间；动线{movement}。",
            f"固定陈设：{furnishings}。墙面{wall_material}；地面{floor_material}；"
            f"家具{furniture_material}；金属/玻璃{glass_metal}；特殊材质{special_material}。",
            f"主色{main_color}，点缀色{accent_color}，色彩气质{color_quality}。",
            f"环境补充细节：{environment_detail}。",
            "版面：",
            "纯白背景，16:9 横向专业场景设定面板；每个视图都是无人纯背景空镜。",
            "以下视角资料若原本涉及人物，只保留相同机位和环境构图，删除人物及其随身物。",
            f"左上场景资料卡；左侧 40%~45% 主场景透视图：{main_view}；"
            f"右上正向全景与反向/侧向全景：{standard_view_1}、{standard_view_2}；"
            f"右侧中部俯视平面布局图；右下三个关键区域细节：{detail_zone}。",
            "所有视图必须是同一个场景，门窗、家具、陈设、光线方向和空间朝向完全一致。",
            "一致性锚点：" + "、".join(environment_keywords) if environment_keywords else "",
            f"构图补充：{environment_composition}" if environment_composition else "",
            f"主光效果{main_light}，只表现光线结果，不绘制光源持有者；"
            f"辅光{fill_light}，营造{atmosphere}。",
            "最终检查：所有面板均不得出现人物、人影、剪影、倒影中的人物或人体局部。",
            "高质量动画场景设定图；不是真实建筑摄影。",
            f"negative prompt: {negative_prompt}" if negative_prompt else "",
        ]
    )


def compile_asset_prompt(
    *,
    style_prompt: str,
    aspect_ratio: str,
    asset_type: str,
    asset_name: str,
    profile: dict[str, Any],
    prompt: str,
    negative_prompt: str,
    consistency_keywords: list[str],
    composition: str,
) -> str:
    effective_negative_prompt = resolve_asset_negative_prompt(asset_type, negative_prompt)
    effective_aspect_ratio = resolve_visual_aspect_ratio(asset_type, aspect_ratio)
    detail = _asset_detail(prompt, profile)
    template = {
        "character": _character_setting_sheet,
        "prop": _prop_setting_sheet,
        "scene": _scene_setting_sheet,
    }.get(asset_type)
    if template is None:
        body = join_non_empty([detail, f"negative prompt: {effective_negative_prompt}"])
    else:
        body = template(
            name=asset_name,
            profile=profile,
            detail=detail,
            consistency_keywords=consistency_keywords,
            composition=composition,
            negative_prompt=effective_negative_prompt,
        )
    return apply_content_safety(
        join_non_empty([style_anchor(style_prompt, effective_aspect_ratio), body])
    )


def compile_keyframe_prompt(
    *,
    style_prompt: str,
    aspect_ratio: str,
    image_prompt: str,
    anchors: list[str],
    shot_context: dict[str, Any],
    negative_prompt: str = "",
    continuity_instruction: str = "",
) -> str:
    effective_aspect_ratio = resolve_visual_aspect_ratio("keyframe", aspect_ratio)
    opening_frame = _value(shot_context.get("opening_frame"), image_prompt)
    ending_frame = _value(shot_context.get("ending_frame"), "与下一镜衔接的稳定状态")
    characters = _value(shot_context.get("characters"), "当前分镜角色")
    props = _value(shot_context.get("props"), "当前分镜物品")
    positions = _value(shot_context.get("positions"), "")
    expressions = _value(shot_context.get("expressions"), "")
    dialogue = _value(shot_context.get("dialogue"), "")
    return apply_content_safety(
        join_non_empty(
            [
                style_anchor(style_prompt, effective_aspect_ratio),
                "【当前分镜素材参考图】",
                *anchors,
                "只使用以上当前分镜涉及的角色、场景和道具素材图锁定身份与空间，不得引入其他镜头的人物、场景或道具。"
                if anchors
                else "当前分镜没有可用绑定参考图，按文字描述生成并保持内部一致。",
                f"风格：{style_prompt}。",
                f"时间地点：{_value(shot_context.get('time_place'), '以当前场景资料为准')}。",
                f"首帧关键构图：{opening_frame}。",
                "用途：生成当前分镜唯一的分镜图，并作为图生视频严格第0秒起始帧。",
                "画面必须停在动作发生前约0.2-0.5秒或动作刚开始的稳定边界，不得提前呈现动作完成结果。",
                f"本镜剧情目标：{image_prompt}。该信息只用于预留动作和构图空间，不得画成动作中段或结束状态。",
                f"视频末帧边界：{ending_frame}。",
                f"人物：{characters}。",
                f"人物站位、朝向与视线：{positions}。" if positions else "",
                f"人物表情：{expressions}。" if expressions else "",
                (
                    f"对白与口型状态：{dialogue}。说话角色呈现与台词对应的自然开口表情，"
                    "非说话角色闭口；对白只作为表演依据，不得生成字幕或画面文字。"
                    if dialogue
                    else ""
                ),
                f"场景：{_value(shot_context.get('scene_name'), '当前分镜场景')}。",
                f"物品：{props}。",
                f"镜头：{_value(shot_context.get('shot_type'), '中景')}，"
                f"{_value(shot_context.get('camera_angle'), '平视')}，"
                f"{_value(shot_context.get('camera_move'), '稳定机位')}，"
                f"焦点{_value(shot_context.get('focus'), '当前主要动作')}。",
                f"视觉：{_value(shot_context.get('lighting'), '符合场景设定的光线')}，"
                f"{_value(shot_context.get('color_tone'), '统一色调')}，"
                f"{_value(shot_context.get('environment_details'), '场景固定环境细节')}。",
                continuity_instruction,
                f"negative prompt: {negative_prompt}" if negative_prompt.strip() else "",
            ]
        )
    )


def shot_template_context(
    current: dict[str, Any],
    asset_names: dict[str, str],
) -> dict[str, Any]:
    dialogue_parts: list[str] = []
    for item in current.get("dialogue", []):
        if not isinstance(item, dict):
            continue
        speaker = str(
            item.get("speaker") or item.get("character") or item.get("name") or "角色"
        ).strip()
        text = str(item.get("text") or item.get("content") or item.get("line") or "").strip()
        if text:
            dialogue_parts.append(f"{speaker}自然开口说：{text}")
    position_parts: list[str] = []
    for item in current.get("positions", []):
        if not isinstance(item, dict):
            continue
        character = str(
            item.get("character") or item.get("name") or item.get("speaker") or "角色"
        ).strip()
        details = [
            str(item.get("position") or item.get("location") or item.get("side") or "").strip(),
            str(item.get("facing") or item.get("direction") or "").strip(),
            str(item.get("gaze") or item.get("look") or item.get("eye_line") or "").strip(),
            str(item.get("action_direction") or "").strip(),
        ]
        details = list(dict.fromkeys(detail for detail in details if detail))
        if details:
            position_parts.append(f"{character}：{'，'.join(details)}")
    expression_parts: list[str] = []
    for item in current.get("expressions", []):
        if not isinstance(item, dict):
            continue
        character = str(
            item.get("character") or item.get("name") or item.get("speaker") or "角色"
        ).strip()
        expression = str(
            item.get("expression") or item.get("emotion") or item.get("mood") or ""
        ).strip()
        if expression:
            expression_parts.append(f"{character}：{expression}")
    return {
        "time_place": current.get("source_ref", ""),
        "previous_state": current.get("start_state", ""),
        "opening_frame": current.get("frame_description") or current.get("start_state", ""),
        "ending_frame": current.get("end_state") or current.get("next_hook", ""),
        "characters": [
            asset_names[item] for item in current.get("character_ids", []) if item in asset_names
        ],
        "scene_name": asset_names.get(current.get("scene_asset_id", ""), ""),
        "props": [asset_names[item] for item in current.get("prop_ids", []) if item in asset_names],
        "shot_type": current.get("shot_type", ""),
        "camera_angle": current.get("camera_angle", ""),
        "camera_move": current.get("camera_move", ""),
        "focus": current.get("action", ""),
        "composition": current.get("frame_description", ""),
        "lighting": current.get("frame_description", ""),
        "color_tone": "遵循项目统一色彩规范",
        "environment_details": current.get("frame_description", ""),
        "positions": "；".join(position_parts),
        "expressions": "；".join(expression_parts),
        "dialogue": "；".join(dialogue_parts),
    }


def compile_video_prompt(
    *,
    style_prompt: str,
    aspect_ratio: str,
    video_prompt: str,
    duration_sec: float,
    anchors: list[str],
    shot_context: dict[str, Any],
    negative_prompt: str = "",
) -> str:
    effective_aspect_ratio = resolve_visual_aspect_ratio("video", aspect_ratio)
    opening_frame = _value(shot_context.get("opening_frame"), "当前关键帧画面")
    ending_frame = _value(shot_context.get("ending_frame"), "为下一镜衔接的稳定状态")
    characters = _value(shot_context.get("characters"), "当前分镜角色")
    props = _value(shot_context.get("props"), "当前分镜物品")
    focus = _value(shot_context.get("focus"), "当前主要动作")
    composition = _value(shot_context.get("composition"), "构图保持稳定")
    lighting = _value(shot_context.get("lighting"), "符合场景设定的光线")
    color_tone = _value(shot_context.get("color_tone"), "统一色调")
    environment_details = _value(shot_context.get("environment_details"), "场景固定环境细节")
    dialogue = _value(shot_context.get("dialogue"), "角色表演自然克制，无额外对白")
    positions = _value(shot_context.get("positions"), "")
    expressions = _value(shot_context.get("expressions"), "")
    if _TIMELINE_PATTERN.search(video_prompt):
        execution_lines = [
            video_prompt,
            f"角色表演与对白：{dialogue}。",
        ]
    else:
        first_end = min(duration_sec, 2)
        execution_lines = [
            f"0-{first_end:g}秒：{video_prompt.rstrip('。；; ')}。{dialogue}。",
        ]
        if duration_sec > first_end:
            execution_lines.append(
                f"{first_end:g}-{duration_sec:g}秒：保持动作连续，围绕{focus}完成平缓镜头变化。"
            )
    return apply_content_safety(
        join_non_empty(
            [
                style_anchor(style_prompt, effective_aspect_ratio),
                "【参考】",
                f"{style_prompt}，{duration_sec:g}秒，{effective_aspect_ratio}。",
                *anchors,
                "【执行】",
                f"{_value(shot_context.get('previous_state'), '以当前关键帧作为上一镜衔接状态')}。",
                f"开场{opening_frame}。",
                f"人物站位、朝向与视线：{positions}。" if positions else "",
                f"人物表情：{expressions}。" if expressions else "",
                *execution_lines,
                f"结束状态：{ending_frame}，末0.5秒保持稳定，为下一镜衔接保留清楚状态。",
                "【镜头与声音】",
                f"{_value(shot_context.get('shot_type'), '中景')}，"
                f"{_value(shot_context.get('camera_angle'), '平视')}视角，"
                f"{_value(shot_context.get('camera_move'), '稳定机位')}。",
                f"焦点围绕{focus}进行一组平缓、连续的调度；{composition}，景别和光线方向保持稳定。",
                f"视觉：{lighting}，{color_tone}，{environment_details}。",
                "说话角色具有自然、克制的说话口型和轻微面部变化；非说话角色全程闭口，不抢话。",
                "生成画面时不添加可辨识对白声音，正式对白由后期配音添加。",
                f"环境与动作声音：{_value(shot_context.get('sound_effects'), '符合场景的环境声')}；"
                f"音乐：{_value(shot_context.get('music'), '克制的氛围音乐')}。",
                "【保持】",
                f"严格保持{characters}的身份、面部、发型、服装、体型、左右关系和起始站位。",
                f"全镜只保留分镜绑定的{props}，外观、数量和位置变化必须服从本镜动作。",
                "场景结构、主要陈设、光线方向和道具外观保持稳定。",
                "不新增角色、手臂、手指、道具或其他物体；不交换人物位置；不提前执行下一镜；"
                "不出现字幕、水印、文字、乱码、镜头抖动、画面闪烁、人物变形、手指畸形、"
                "道具穿模、物体瞬移、服装变化或光线跳变。",
                f"补充禁止事项：{negative_prompt}。" if negative_prompt.strip() else "",
            ]
        )
    )
