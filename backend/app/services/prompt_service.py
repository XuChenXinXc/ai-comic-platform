import re
from collections.abc import Iterable
from typing import Any

STYLE_ANCHOR = "[STYLE_ANCHOR]"
VISUAL_PROMPT_TEMPLATE_VERSION = "visual-prompt-v3.0"
_FIXED_VISUAL_ASPECT_RATIOS = {
    "character": "3:2",
    "prop": "3:2",
    "scene": "16:9",
    "keyframe": "16:9",
    "video": "16:9",
}
_TIMELINE_PATTERN = re.compile(
    r"(?:^|[；;\n])\s*(\d+(?:\.\d+)?)\s*[-–—~－至]\s*(\d+(?:\.\d+)?)\s*秒"
)
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
            f"剪辑使用时长：{duration_sec:g} 秒（生成时长以供应商有效时长为准，"
            "时间轴不足部分自动保持末帧稳定）",
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
    if not custom_prompt:
        return default_prompt
    # 所有素材类型统一合并默认负向约束，避免自定义负向替换掉默认项；
    # 合并保持幂等：默认项已存在时不重复追加。兼容中文顿号与逗号分隔。
    custom_items = [
        item.strip()
        for item in re.split(r"[、，,]", custom_prompt)
        if item.strip()
    ]
    default_items = [item.strip() for item in default_prompt.split("、") if item.strip()]
    merged_items = list(custom_items)
    for item in default_items:
        if item not in merged_items:
            merged_items.append(item)
    return "、".join(merged_items)


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
        fallback=normal_expression,
    )
    lighting = _profile_value(profile, "lighting", fallback="标准角色设定柔光")
    atmosphere = _profile_value(profile, "atmosphere", fallback=style_tone)
    anchors = "、".join(consistency_keywords)
    detail_clean = detail.rstrip("。") if detail else detail
    has_detail = bool(detail_clean) and detail_clean != "以素材资料保持稳定一致"
    paragraphs = [
        f"《{name}・{concept} 角色基础设定稿》",
        "核心人物设定：\n"
        f"{name}，一位{age}的{gender}角色，身份为{identity}，生活在{worldview}中。"
        f"性格关键词为{personality}。\n"
        f"外貌：{hair}；{appearance}；{eyes}；常态表情{normal_expression}；身形{body}。\n"
        f"核心视觉识别点包括：{immutable}。",
        "服饰与造型设定：\n"
        f"角色穿着{costume}，主色调为{main_color}，辅以{accent_color}少量点缀，"
        f"材质为{material}，清晰表现不同材质之间的纹理、厚度和光泽差异。\n"
        f"上装为{upper}；下装为{lower}；外搭为{outer}；鞋履为{shoes}；"
        f"配饰{accessories}；核心道具{props}。\n"
        f"整体造型呈现{style_tone}的气质，具有鲜明辨识度，"
        "同时适合后续AI漫剧分镜图和视频生成时保持角色一致性。",
        f"细节补充：{detail_clean}。" if has_detail else "",
        "版面背景与整体要求：\n"
        "整张角色设定展板使用标准纯白色背景，背景干净、明亮、极简、通透。"
        "不要浅灰背景，不要米白背景，不要渐变背景，不要彩色背景，不要环境场景，"
        "不要室内或室外布景。\n"
        "仅使用极细浅灰色分隔线和少量低饱和冷蓝灰色细线建立版面秩序，"
        "不使用厚重边框、大面积色块、复杂花纹或抢眼装饰。\n"
        "整体呈现高规格动画、漫画或AI漫剧项目的官方角色设定面板质感，"
        "专业、精致、高级、规整、清晰。",
        "版面排布结构：\n"
        f"采用3:2横向宽幅、分块拼接式专业角色设定面板排版。画面只展示{name}同一名角色，"
        "不出现其他人物。\n"
        "面板只保留人物介绍、主人物立绘、三视图和核心面部特写四类信息，"
        "不加入服装拆解、配饰拆解、发型拆解、道具拆解、场景插图或意境参考图。",
        "左上角人物介绍区：\n"
        "在画面左上角设置简洁、高级、规整的人物资料卡，与左侧主人物立绘形成一个完整区域。\n"
        "人物资料卡内容为：\n"
        f"角色姓名：{name}\n"
        f"角色意境或代号：{concept}\n"
        f"身份定位：{identity}\n"
        f"世界观或背景：{worldview}\n"
        f"性格关键词：{personality}\n"
        f"视觉识别点：{immutable}\n"
        "人物资料卡采用现代官方动画设定稿式排版，标题、姓名和正文具有清晰的字号层级，"
        "文字区域留白舒适、排列整齐。中文文字尽量清晰、准确、可读，"
        "不要乱码、错字、重复文字或无意义符号。",
        "左侧主人物立绘：\n"
        f"左侧主体区域放置一张尺寸最大的{name}全身精致立绘，占整张画面约40%至45%。\n"
        "完整展示头部、清晰五官、发型、服装、鞋履和随身配饰。"
        "人物从头顶到脚底全部处于画面内，不裁切头部、手臂、腿部或鞋履，"
        "不被人物资料卡和其他模块遮挡。",
        "右上区域三视图：\n"
        "右上区域规整排列三张标准全身视图，依次为正面全身、标准侧面全身、背面全身。\n"
        "三张视图采用统一自然站姿、统一身体比例、统一服装、统一发型和统一影棚光照。"
        "清楚展示人物正面轮廓、侧面结构、背部造型和服装前后结构。"
        "每张视图均保持完整全身，不裁切、不遮挡、不采用夸张透视。",
        "右下区域核心面部特写：\n"
        f"右下区域排列三个高精度面部特写，分别为{expression_group}。\n"
        "三个面部特写必须保持完全一致的脸型、骨相、五官比例、年龄感、肤色、发型和发色，"
        "仅通过眼神、眉形、眼睑和嘴角的细微变化表现状态差异，"
        "表情自然细腻，不夸张、不低幼、不颜艺化。",
        "角色一致性要求：\n"
        f"主人物立绘、三视图和三个面部特写必须严格表现为同一个{name}。"
        "所有模块保持完全一致的脸型、五官比例、年龄感、肤色、身体比例、发型结构、发色、"
        "服装款式、服装颜色、材质和核心视觉识别点。\n"
        "不得生成不同版本的角色，不得改变年龄、脸型、服装或发型，"
        "不得随机增加或删除配饰。"
        + (f"一致性锚点：{anchors}。" if anchors else ""),
        f"构图补充：{composition.rstrip('。')}。" if composition else "",
        "画面美术风格：\n"
        "整体采用半写实二维国漫动画风，精致干净的角色线稿，细腻克制的二维动画上色，"
        "高质量AI漫剧角色设定图质感。\n"
        "严格保持插画、动漫和二维动画媒介，不转换为真人摄影、影视剧照、三次元演员、"
        "真人模特或写实实拍风格。",
        "灯光与色彩氛围：\n"
        f"{lighting}，角色整体配色以{main_color}和{accent_color}为核心，"
        f"营造{atmosphere}的氛围。",
        "画质与成图要求：\n"
        "高精度视觉效果，人物发丝、眼眸、服装纹理和配饰细节清楚可见。"
        "全画面保持高清晰度，无景深模糊，无低清缩略图感，无杂色噪点，无脏污背景。\n"
        "无其他人物，无不同版本角色，无重复头部，无多余肢体，无手脚缺失，无错误手指，"
        "无肢体粘连，无五官错位，无年龄漂移，无比例失衡，无服装变化，无发型变化，"
        "无服装穿模。版面模块对齐，间距统一，信息完整但不拥挤，留白舒适，视觉层级明确。",
        f"negative prompt: {negative_prompt}" if negative_prompt else "",
    ]
    return "\n\n".join(paragraph for paragraph in paragraphs if paragraph.strip())


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
    anchors = "、".join(consistency_keywords)
    detail_clean = detail.rstrip("。") if detail else detail
    has_detail = bool(detail_clean) and detail_clean != "以素材资料保持稳定一致"
    paragraphs = [
        f"《{name}・{concept} 完整道具设定稿》",
        "核心物品设定：\n"
        f"{name}，属于{worldview}中的{category}，主要由{owner}使用，用于{plot_function}。\n"
        f"整体轮廓{silhouette}，尺寸约{dimensions}，重量感{weight}。\n"
        f"核心视觉识别点包括：{special_features}。",
        "外观结构与细节：\n"
        f"正面{front}；侧面{side}；背面{back}；顶部/底部/内部{other}；"
        f"按钮、接口、纹样、把手等位于{key_position}。\n"
        "所有结构的位置符合真实使用逻辑，物品可以被正常握持、佩戴或摆放，"
        "不出现无法操作或结构矛盾的部分。",
        "材质与色彩设定：\n"
        f"主体材质{material}；辅助材质{secondary_material}；主色{color}，"
        f"点缀色{accent_color}；表面质感{surface}；固定磨损{wear}。\n"
        "所有纹样、接缝、按钮、接口、结构、磨损位置和配色必须固定，"
        "便于后续AI漫剧分镜图与视频生成保持一致。",
        f"细节补充：{detail_clean}。" if has_detail else "",
        "版面背景与整体要求：\n"
        "整张物品设定展板使用标准纯白色背景，背景干净、明亮、极简、通透。"
        "不要浅灰背景，不要米白背景，不要渐变背景，不要完整环境场景。\n"
        "仅使用极细浅灰色分隔线和少量低饱和冷蓝灰色细线建立版面秩序。"
        "整体呈现高规格动画、漫画或AI漫剧项目的官方道具设定面板质感，"
        "专业、精致、高级、规整、清晰。",
        "版面排布结构：\n"
        f"采用3:2横向宽幅、分块拼接式专业道具设定面板。画面只展示同一件{name}，"
        "不出现其他无关道具，不出现完整人物角色，不出现完整环境场景。\n"
        "面板只保留物品介绍、主物品精致展示图、标准多视图、三个关键细节"
        "和尺寸使用比例参考。",
        "左上角物品介绍区：\n"
        "在画面左上角设置简洁、规整的物品资料卡。\n"
        "物品资料卡内容为：\n"
        f"物品名称：{name}\n"
        f"物品意象：{concept}\n"
        f"物品类别：{category}\n"
        f"所属角色：{owner}\n"
        f"主要用途：{plot_function}\n"
        f"核心材质：{material}\n"
        f"视觉识别点：{special_features}\n"
        "资料卡采用官方动画道具设定稿式排版，标题与正文层级清晰，文字区域留白舒适。"
        "中文尽量清晰、准确、可读，不要乱码、错字或无意义符号。",
        "左侧主物品展示图：\n"
        f"左侧主体区域放置一张尺寸最大的{name}精致立体展示图，占整张画面约40%至45%。\n"
        "采用四十五度略微俯视视角，同时展示正面、顶部和侧面结构，完整表现物品轮廓、"
        "厚度和关键部件。\n"
        "物品不裁切、不遮挡、不变形，不使用夸张透视，是整张面板的视觉中心。"
        "主体材质只产生细腻克制的边缘反光，不呈现镜面高亮。",
        "右上区域标准多视图：\n"
        "右上区域规整排列四个标准视图：正面视图、侧面视图、背面视图、顶部或底部视图。\n"
        "四个视图保持完全一致的尺寸比例、轮廓、结构、颜色、材质、磨损和部件位置，"
        "不得出现不同型号或不同版本。",
        "右侧中部关键细节：\n"
        f"三个高精度局部细节：{detail_zone}。",
        "右下区域尺寸与使用比例：\n"
        f"展示物品标准尺寸标注与使用比例参考：{scale}。"
        "所有比例参考中的物品外观必须保持完全一致。",
        "物品一致性要求：\n"
        f"主物品图、多视图、关键细节和比例参考必须严格表现为同一件{name}。"
        "所有模块保持完全一致的形状、尺寸比例、颜色、材质、结构、磨损和划痕。\n"
        "不得生成不同型号，不得随机改变颜色，不得增减部件，不得改变磨损痕迹的位置、"
        "方向或长度。"
        + (f"一致性锚点：{anchors}。" if anchors else ""),
        f"构图补充：{composition.rstrip('。')}。" if composition else "",
        "画面美术风格：\n"
        "整体采用半写实二维国漫动画道具设定风，精致清晰的设计线稿，"
        "细腻克制的二维动画上色与材质表现。\n"
        "严格保持动画道具设定图质感，不转换为真人产品摄影、电商白底摄影、"
        "三维写实工业渲染或真实品牌商品宣传图。",
        "灯光与画质：\n"
        "采用中性偏冷影棚柔光，正面使用干净柔和的主光，右后方使用细腻轮廓光，"
        "清晰展示物品轮廓、厚度、材质纹理、接缝、按钮和固定磨损。\n"
        "高精度视觉效果，无景深模糊，无低清区域，无多余物品，无不同型号，无重复部件，"
        "无结构错位，无悬浮零件，无错误透视，无材质混乱，无随机文字或品牌标志。"
        "整张设定面板背景始终保持标准纯白色，版面模块对齐，间距统一，留白舒适。",
        f"negative prompt: {negative_prompt}" if negative_prompt else "",
    ]
    return "\n\n".join(paragraph for paragraph in paragraphs if paragraph.strip())


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
    anchors = "、".join(environment_keywords)
    paragraphs = [
        f"《{name}・完整场景设定稿》",
        "【最高优先级：无人纯背景空镜】\n"
        "这是供后续分镜复用的纯环境场景资产，不是剧情分镜。所有视图必须为空场景，"
        "禁止出现人物、人群、路人、行人、人影、人形、剪影、倒影或镜面中的人物以及人体局部。\n"
        "下方资料中出现的任何人物姓名、人物动作、角色站位、人物视角或人物持有物，"
        "都只能用于理解空间尺度和动线，不得成为画面内容。",
        "核心场景设定：\n"
        f"{name}，位于{worldview}中的{place_type}。\n"
        f"空间关键词为{layout}；空间规模{scale}；建筑与室内风格为{architecture}。\n"
        f"核心视觉识别点包括：{furnishings}。",
        "空间结构与区域关系：\n"
        "以入口、主要空间轴线与主场景透视方向为统一方向基准。\n"
        f"主要区域：{regions}。\n"
        f"入口{entrance}；窗户/自然光入口{windows}；"
        f"表演活动区按主要区域和动线预留为空置空间；动线{movement}。\n"
        "所有通道宽度合理，符合真实使用逻辑。",
        "建筑、家具与陈设：\n"
        f"墙面{wall_material}；地面{floor_material}；家具{furniture_material}；"
        f"金属/玻璃{glass_metal}；特殊材质{special_material}。\n"
        "家具尺寸、门窗高度、桌椅比例和空间尺度符合真实使用逻辑。"
        "所有核心陈设的位置、外观、颜色和数量固定，"
        "不得随机增加无关陈设、装饰或复杂文字标识。",
        "材质与色彩设定：\n"
        f"主色{main_color}，点缀色{accent_color}，色彩气质{color_quality}。"
        "整体色彩陈旧、克制、低饱和，不过度鲜艳，不使用杂乱无规律的颜色。",
        f"环境补充细节：{environment_detail}。" if environment_detail else "",
        "版面背景与整体要求：\n"
        "整张场景设定展板使用标准纯白色背景，背景干净、明亮、极简、通透。"
        "不要浅灰背景，不要米白背景，不要渐变底色，不要将完整环境延伸为面板背景。\n"
        "仅使用极细浅灰色分隔线和少量低饱和冷蓝灰色细线建立版面秩序。"
        "整体呈现高规格动画、漫画或AI漫剧项目的官方场景设定面板质感，"
        "专业、精致、高级、规整、清晰。",
        "版面排布结构：\n"
        f"采用16:9横向宽幅、分块拼接式专业场景设定面板。画面只展示{name}这一个场景，"
        "不出现其他无关场景，不出现人物角色或人体残影。\n"
        "面板只保留场景介绍、主场景图、两个标准观察角度、俯视平面布局图"
        "和三个关键区域细节，不加入剧情分镜、人物动作、无关意境插图或大量装饰性小图。",
        "左上角场景介绍区：\n"
        "在画面左上角设置简洁、规整的场景资料卡。\n"
        "场景资料卡内容为：\n"
        f"场景名称：{name}\n"
        f"场景定位：{place_type}\n"
        f"时代与世界观：{worldview}\n"
        f"空间关键词：{layout}\n"
        f"核心视觉识别点：{furnishings}\n"
        "资料卡采用官方动画场景设定稿式排版，标题与正文层级清晰，"
        "文字区域留白舒适、排列整齐。中文尽量清晰、准确、可读，"
        "不要乱码、错字或无意义符号。",
        "左侧主场景图：\n"
        f"左侧主体区域放置一张尺寸最大的完整场景透视图，占整张画面约40%至45%。\n"
        f"构图：{main_view}。\n"
        "使用适度广角全景，不使用夸张超广角，不产生严重透视畸变，不遮挡关键区域。"
        "主场景图不出现人物。",
        "右上区域标准观察角度：\n"
        f"第一个视角：{standard_view_1}。\n"
        f"第二个视角：{standard_view_2}。\n"
        "两个视角必须表现为完全相同的场景，保持一致的门窗位置、家具摆放、装饰数量、"
        "材质、颜色、空间比例和光照方向。",
        "右侧中部俯视平面布局图：\n"
        "放置一张简洁清晰的俯视平面布局图，使用简洁箭头标示主要通行路线与角色常用站位。"
        "平面图风格简洁专业，不做复杂装饰，不改变实际空间结构。",
        "右下区域关键区域细节：\n"
        f"三个关键区域细节：{detail_zone}。\n"
        "所有细节必须与主场景图完全一致，不得额外增加、删除或移动陈设。",
        "场景一致性要求：\n"
        "主场景图、两个观察角度、俯视平面布局图和三个关键区域细节必须严格表现为同一个场景。"
        "所有模块保持完全一致的空间尺寸、门窗结构、家具造型、家具位置、装饰数量、颜色、"
        "材质、磨损痕迹和核心视觉识别点。\n"
        "不得生成不同版本的房间，不得改变空间朝向，不得让门窗或家具在不同视图中随机移动，"
        "不得出现镜像颠倒或空间结构冲突。"
        + (f"一致性锚点：{anchors}。" if anchors else ""),
        f"构图补充：{environment_composition.rstrip('。')}。" if environment_composition else "",
        "画面美术风格：\n"
        "整体采用半写实二维国漫动画场景风，精致清晰的动画背景线稿，"
        "细腻克制的二维动画上色与材质表现，与项目角色设定图保持相同的动画媒介"
        "和世界观审美。\n"
        "严格保持插画、动漫或二维动画场景设定风格，不转换为真人摄影、影视实拍、"
        "建筑摄影、三维写实渲染或写实样板间。",
        "灯光与色彩氛围：\n"
        f"主光效果{main_light}，只表现光线结果，不绘制光源持有者；"
        f"辅光{fill_light}，营造{atmosphere}的氛围。\n"
        "灯光清晰展示空间结构和材质，不让暗部吞没陈设细节。",
        "画质与成图要求：\n"
        "高精度视觉效果，建筑结构、家具边缘、材质纹理、墙面痕迹和金属细节清晰可见。"
        "全画面无景深模糊，无低清缩略图感，无脏污背景，无人物，无人体残影，"
        "无错误透视，无弯曲墙面，无悬浮家具。\n"
        "最终检查：所有面板均不得出现人物、人影、剪影、倒影中的人物或人体局部。"
        "版面模块对齐，间距统一，信息完整但不拥挤，留白舒适，视觉层级明确。",
        f"negative prompt: {negative_prompt}" if negative_prompt else "",
    ]
    return "\n\n".join(paragraph for paragraph in paragraphs if paragraph.strip())


def _character_clean_reference(
    *,
    name: str,
    profile: dict[str, Any],
    detail: str,
    consistency_keywords: list[str],
    composition: str,
    negative_prompt: str,
) -> str:
    """干净角色参考图：无文字、无边框、单一主体，专供图生图和视频引用。"""
    age = _profile_value(profile, "age", fallback="年龄以素材资料为准")
    gender = _profile_value(profile, "gender", fallback="性别以素材资料为准")
    hair = _profile_value(profile, "hair", fallback="发型以素材资料为准")
    appearance = _profile_value(profile, "appearance", "face", fallback="脸部特征以素材资料为准")
    costume = _profile_value(profile, "costume", fallback="服装以素材资料为准")
    main_color = _profile_value(profile, "main_color", "color", fallback="服装主色以素材资料为准")
    immutable = _profile_value(profile, "immutable_features", fallback="固定识别特征以素材资料为准")
    expression = _profile_value(
        profile,
        "normal_expression",
        "acting_traits",
        fallback="自然克制",
    )
    return join_non_empty(
        [
            f"《{name}・干净角色参考图》",
            "用途：仅作为图生图与视频生成的参考图，不包含任何文字、资料卡、"
            "边框、宫格、多视图或标注。",
            f"画面内容：{name}，一位{age}的{gender}角色，{hair}，{appearance}；"
            f"服装{costume}，主色{main_color}；表情{expression}。",
            f"固定识别特征：{immutable}。",
            f"细节补充：{detail}。" if detail and detail != "以素材资料保持稳定一致" else "",
            "构图：全身立绘，正面微侧，人物完整居中，四肢与道具无遮挡；背景为纯白色，无任何环境陈设。",
            "画风：与项目设定稿完全一致的二维动画角色，不是真人摄影。",
            f"negative prompt: {negative_prompt}" if negative_prompt else "",
        ]
    )


def _prop_clean_reference(
    *,
    name: str,
    profile: dict[str, Any],
    detail: str,
    consistency_keywords: list[str],
    composition: str,
    negative_prompt: str,
) -> str:
    """干净道具参考图：无文字、无边框、单件物品，专供图生图和视频引用。"""
    description = _profile_value(profile, "description", fallback="外观以素材资料为准")
    special_features = _profile_value(
        profile, "special_features", fallback="关键结构以素材资料为准"
    )
    material = _profile_value(profile, "material", fallback="主体材质以素材资料为准")
    color = _profile_value(profile, "color", "main_color", fallback="主色以素材资料为准")
    scale = _profile_value(profile, "scale_reference", fallback="与成年人手掌的比例以素材资料为准")
    return join_non_empty(
        [
            f"《{name}・干净道具参考图》",
            "用途：仅作为图生图与视频生成的参考图，不包含任何文字、资料卡、"
            "边框、尺寸标注、比例参考或多视图。",
            f"画面内容：单件物品{name}，{description}；关键结构{special_features}；"
            f"主体材质{material}，主色{color}；大小{scale}。",
            f"细节补充：{detail}。" if detail and detail != "以素材资料保持稳定一致" else "",
            "构图：物品完整居中，四十五度略俯视，无人物手部入镜；背景为纯白色，无任何环境陈设。",
            "画风：与项目设定稿完全一致的二维动画道具，不是产品摄影。",
            f"negative prompt: {negative_prompt}" if negative_prompt else "",
        ]
    )


def _scene_clean_reference(
    *,
    name: str,
    profile: dict[str, Any],
    detail: str,
    consistency_keywords: list[str],
    composition: str,
    negative_prompt: str,
) -> str:
    """干净场景参考图：无文字、无边框、无人空镜，专供图生图和视频引用。"""
    layout = _scene_profile_value(
        profile,
        "space_keywords",
        "layout",
        fallback="空间布局以素材资料为准",
    )
    architecture = _scene_profile_value(
        profile, "architecture", fallback="建筑/室内风格以素材资料为准"
    )
    furnishings = _scene_profile_value(profile, "furnishings", fallback="固定陈设以素材资料为准")
    color_mood = _scene_profile_value(profile, "color_mood", fallback="主色与点缀色以素材资料为准")
    environment_detail = _scene_environment_text(detail, "")
    return join_non_empty(
        [
            f"《{name}・干净场景参考图》",
            "用途：仅作为图生图与视频生成的参考图，不包含任何文字、资料卡、"
            "平面布局图、标注、边框或多视图。",
            "【最高优先级：无人纯背景空镜】禁止出现人物、人群、路人、行人、人影、"
            "人形、剪影、倒影中的人物或人体局部。",
            f"画面内容：{name}，{architecture}，空间{layout}，固定陈设{furnishings}；"
            f"配色{color_mood}。",
            f"环境补充细节：{environment_detail}。" if environment_detail else "",
            "构图：主场景全景透视图，机位与项目设定稿一致，画面完整居中；"
            "背景为画面自身场景，不添加边框或文字。",
            "画风：与项目设定稿完全一致的二维动画场景，不是真实建筑摄影。",
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
    reference_mode: bool = False,
) -> str:
    effective_negative_prompt = resolve_asset_negative_prompt(asset_type, negative_prompt)
    effective_aspect_ratio = resolve_visual_aspect_ratio(asset_type, aspect_ratio)
    detail = _asset_detail(prompt, profile)
    if reference_mode:
        reference_template = {
            "character": _character_clean_reference,
            "prop": _prop_clean_reference,
            "scene": _scene_clean_reference,
        }.get(asset_type)
        if reference_template is not None:
            body = reference_template(
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
    voice_over = _value(shot_context.get("voice_over"), "")
    paragraphs = [
        "【参考】\n"
        + style_anchor(style_prompt, effective_aspect_ratio)
        + "\n"
        + "\n".join(anchors)
        + (
            "\n只使用以上当前分镜涉及的角色、场景和道具素材图锁定身份与空间，"
            "不得引入其他镜头的人物、场景或道具。"
            if anchors
            else "\n当前分镜没有可用绑定参考图，按文字描述生成并保持内部一致。"
        ),
        "【剧情时刻】\n"
        f"{image_prompt.rstrip('。')}。\n"
        "画面只呈现该剧情时刻对应的单一关键瞬间，不制作多格漫画，不制作设定展板，"
        "不制作动作分解图，不在同一画面重复展示同一角色，"
        "不在同一画面重复呈现起始动作和完成动作。",
        "【人物与动作】\n"
        f"人物：{characters}。"
        + (f"\n站位、朝向与视线：{positions}。" if positions else "")
        + (
            f"\n对白与口型状态：{dialogue.rstrip('。')}。说话角色呈现与台词对应的自然开口表情，"
            "非说话角色闭口；对白只作为表演依据，不得生成字幕或画面文字。"
            if dialogue
            else ""
        )
        + (
            f"\n画外音（后期配音，画面人物闭口）：{voice_over.rstrip('。')}。"
            "本镜台词来自录音或旁白，画面中所有人物全程闭口，不产生说话口型；"
            "对白由后期配音添加，不得生成字幕或画面文字。"
            if voice_over
            else ""
        ),
        "【场景与空间关系】\n"
        f"时间地点：{_value(shot_context.get('time_place'), '以当前场景资料为准')}。\n"
        f"场景：{_value(shot_context.get('scene_name'), '当前分镜场景')}。"
        "空间结构、门窗位置、家具摆放与陈设布局沿用绑定参考图，"
        "不得随机改变房间朝向、移动陈设、增加无关物品或其他人物，不得将场景镜像翻转。",
        "【关键物品】\n"
        f"物品：{props}。"
        "外观、尺寸、颜色与磨损痕迹沿用绑定参考图，"
        "不放大、不缩小、不变形、不漂浮、不穿模、不遮挡人物关键动作。",
        "【镜头构图】\n"
        f"景别{_value(shot_context.get('shot_type'), '中景')}，"
        f"机位{_value(shot_context.get('camera_angle'), '平视')}，"
        f"运镜{_value(shot_context.get('camera_move'), '稳定机位')}，"
        f"焦点{_value(shot_context.get('focus'), '当前主要动作')}。\n"
        f"首帧关键构图：{opening_frame.rstrip('。')}。\n"
        "用途：生成当前分镜唯一的分镜图，并作为图生视频严格第0秒起始帧。\n"
        "画面必须停在动作发生前约0.2-0.5秒或动作刚开始的稳定边界，"
        "不得提前呈现动作完成结果。\n"
        f"视频末帧边界：{ending_frame.rstrip('。')}。",
        "【人物神态与情绪】\n"
        + (
            f"{expressions}。表情自然细腻、克制真实，不使用夸张颜艺，"
            "不瞪大双眼，不张大嘴，情绪强度适中。"
            if expressions
            else "人物表情自然克制，情绪强度适中，不使用夸张颜艺。"
        ),
        "【灯光与色彩】\n"
        f"{_value(shot_context.get('lighting'), '符合场景设定的光线')}，"
        f"{_value(shot_context.get('color_tone'), '统一色调')}，"
        f"环境细节{_value(shot_context.get('environment_details'), '场景固定环境细节')}。\n"
        "光线符合场景设定的真实光源逻辑，清晰照亮人物五官、双手与关键物品，"
        "不让暗部吞没剧情信息，不产生无来源的强光或过度戏剧化阴影。",
        "【画面美术质感】\n"
        f"保持{style_prompt.strip() or '项目既定动画风格'}的动画/插画质感，"
        "人物线条干净，面部和手部结构准确，场景空间清晰，材质区分明确，光影自然克制。\n"
        "人物、场景与物品的完成精度保持统一，不呈现真人摄影、影视剧照、三次元演员、"
        "写实实拍或过度三维游戏CG质感。",
        f"【连续性要求】\n{continuity_instruction}" if continuity_instruction.strip() else "",
        "【成图限制】\n"
        "画面只表现当前一个剧情瞬间，不制作多格漫画，不制作设定展板，"
        "不制作动作分解图，不在同一画面重复展示同一角色。\n"
        "无人物漂浮，无物品漂浮，无穿模，无物品尺寸异常，无家具移动，无门窗错位，"
        "无场景镜像翻转，无错误透视。\n"
        "画面中不出现字幕、对白、对话框、旁白文字、水印、品牌标志、"
        "文件文字、乱码或无意义符号。"
        + (f"\nnegative prompt: {negative_prompt}" if negative_prompt.strip() else ""),
    ]
    return apply_content_safety(
        "\n\n".join(paragraph for paragraph in paragraphs if paragraph.strip())
    )


def shot_template_context(
    current: dict[str, Any],
    asset_names: dict[str, str],
) -> dict[str, Any]:
    dialogue_parts: list[str] = []
    voice_over_parts: list[str] = []
    for item in current.get("dialogue", []):
        if not isinstance(item, dict):
            continue
        speaker = str(
            item.get("speaker") or item.get("character") or item.get("name") or "角色"
        ).strip()
        text = str(item.get("text") or item.get("content") or item.get("line") or "").strip()
        if not text:
            continue
        # 录音笔、旁白、画外音等来源属于后期配音内容，画面人物必须全程闭口
        if any(marker in speaker for marker in ("录音", "画外音", "旁白", "OS", "内心")):
            voice_over_parts.append(f"{speaker}：{text}")
        else:
            dialogue_parts.append(f"{speaker}自然开口说：{text}")
    if current.get("narration"):
        narration = str(current.get("narration") or "").strip()
        if narration:
            voice_over_parts.append(f"画外旁白：{narration}")
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
        # 关键帧必须以动作开始前的静态状态为准，不得把动作中段或结果画进首帧
        "opening_frame": current.get("start_state") or current.get("frame_description", ""),
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
        # frame_description 只用作构图参考，不再重复充当光线与环境细节
        "composition": current.get("frame_description", ""),
        "lighting": "",
        "color_tone": "遵循项目统一色彩规范",
        "environment_details": "",
        "positions": "；".join(position_parts),
        "expressions": "；".join(expression_parts),
        "dialogue": "；".join(dialogue_parts),
        "voice_over": "；".join(voice_over_parts),
    }


def _camera_line(camera_move: Any) -> str:
    """机位描述二选一：固定机位与连续调度不共存。

    仅以「固定/不动/静止」等机位语义词判定固定机位；
    「稳定」多为防抖描述（如"稳定横移"），不视为固定。
    """
    raw = str(camera_move or "").strip()
    fixed_markers = ("固定", "不动", "静止", "机位不动", "无运镜")
    is_fixed = any(marker in raw for marker in fixed_markers) or not raw
    if is_fixed:
        return "固定机位，画面变化由人物动作推动，摄影机不移动。"
    return f"{raw or '缓慢跟随'}；缓慢跟随人物运动，不切换机位。"


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
    voice_over = _value(shot_context.get("voice_over"), "")
    # 判断是否有真实画面内对白：dialogue 变量带 fallback 文本，须以原始上下文为准
    has_dialogue = bool(str(shot_context.get("dialogue") or "").strip())
    positions = _value(shot_context.get("positions"), "")
    expressions = _value(shot_context.get("expressions"), "")
    if _TIMELINE_PATTERN.search(video_prompt):
        execution_lines = [
            video_prompt,
            f"角色表演与对白：{dialogue}" if has_dialogue else "",
            f"画外音（后期配音，画面人物闭口）：{voice_over}" if voice_over else "",
        ]
        # 时间轴末段不足生成时长时，补足稳定收尾段，避免生成时长短于请求时长导致尾部无调度
        timeline_ends = [
            float(match.group(2))
            for match in _TIMELINE_PATTERN.finditer(video_prompt)
            if match.group(2)
        ]
        if timeline_ends:
            last_end = max(timeline_ends)
            if last_end < duration_sec:
                execution_lines.append(
                    f"{last_end:g}-{duration_sec:g}秒：保持上一段结束状态稳定，"
                    "人物与机位不新增动作，为剪辑衔接保留画面。"
                )
    else:
        first_end = min(duration_sec, 2)
        dialogue_parts = []
        if has_dialogue:
            dialogue_parts.append(dialogue)
        if voice_over:
            dialogue_parts.append("画外音（后期配音，画面人物闭口）：" + voice_over)
        dialogue_line = "；".join(dialogue_parts)
        execution_lines = [
            f"0-{first_end:g}秒：{video_prompt.rstrip('。；; ')}。{dialogue_line}",
        ]
        if duration_sec > first_end:
            execution_lines.append(
                f"{first_end:g}-{duration_sec:g}秒：保持动作连续，围绕{focus}完成平缓镜头变化。"
            )
    return apply_content_safety(
        join_non_empty(
            [
                style_anchor(style_prompt, effective_aspect_ratio),
                "【最高优先级】",
                "优先级从高到低：①画风锁定 ②人物/场景/道具绑定 ③核心动作 ④镜头 ⑤连续性 ⑥禁止项。"
                "冲突时按此顺序裁决。",
                "【参考】",
                f"{style_prompt}，{duration_sec:g}秒，{effective_aspect_ratio}。",
                *anchors,
                "若参考图与文字描述冲突，以参考图中的人物外观、服装、道具和空间关系为准。"
                if anchors
                else "",
                "【执行】",
                f"{_value(shot_context.get('previous_state'), '以当前关键帧作为上一镜衔接状态')}。",
                f"第0秒以关键帧画面为准：{opening_frame}。",
                f"人物站位、朝向与视线：{positions}。" if positions else "",
                f"人物表情：{expressions}。" if expressions else "",
                *execution_lines,
                f"结束状态：{ending_frame.rstrip('。；; ')}，末0.5秒保持稳定，"
                "为下一镜衔接保留清楚状态。",
                "【镜头与声音】",
                f"{_value(shot_context.get('shot_type'), '中景')}，"
                f"{_value(shot_context.get('camera_angle'), '平视')}视角，"
                f"{_camera_line(shot_context.get('camera_move'))}",
                f"焦点围绕{focus}进行一组平缓、连续的调度；{composition}，景别和光线方向保持稳定。",
                f"视觉：{lighting}，{color_tone}，{environment_details}。",
                (
                    "说话角色嘴唇轻微开合、表情自然，不生成夸张口型；非说话角色全程闭口，不抢话。"
                    if has_dialogue
                    else "画面内无台词对白：所有人物全程闭口，不产生说话口型。"
                ),
                "生成画面时不添加可辨识对白声音，正式对白与画外音由后期配音添加。",
                "环境与动作声音："
                f"{_value(shot_context.get('sound_effects'), '符合场景的环境声')}。",
                "【保持】",
                f"严格保持{characters}的身份、面部、发型、服装、体型、左右关系和起始站位。",
                (
                    f"全镜只保留分镜绑定的{props}；重点道具（{props}）的外观、尺寸、颜色、"
                    "位置保持一致，不得消失、变形或穿模。"
                    if props and props != "当前分镜物品"
                    else ""
                ),
                "背景建筑、道路布局、光源方向、天气状态全程保持一致；"
                "雨势、灯光、昼夜不得突变，场景结构、主要陈设和道具外观保持稳定。",
                "禁止出现：人物变形、服装变化、新增人物、字幕文字、物体瞬移。"
                "缴费单、笔记、招牌等画面文字由后期叠加，生成画面本身不绘制任何文字。",
                f"补充禁止事项：{negative_prompt}。" if negative_prompt.strip() else "",
            ]
        )
    )
