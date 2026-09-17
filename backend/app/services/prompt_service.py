import logging
import math
import re
from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import ValidationError

from app.dynamic_pipeline.prompt_ir import VisualStyleSpec
from app.dynamic_pipeline.visual_anchor import (
    compile_visual_bible,
    merge_visual_bible_negative,
    render_visual_style_spec,
)
from app.dynamic_pipeline.visual_validator import style_medium_of
from app.services.director_plan import (
    director_bundle_from_llm,
    render_director_video_prompt,
)
from app.services.natural_video_prompt import (
    ReferenceToken,
    parse_reference_anchors,
    render_natural_video_prompt,
)

logger = logging.getLogger(__name__)

STYLE_ANCHOR = "统一画风锚定（最高优先级）：全片所有画面严格采用下列画风要求"
VISUAL_PROMPT_TEMPLATE_VERSION = "visual-prompt-v3.1"
# 角色设定板面部特写数量兜底：表情组缺失或为空时按此数量排版。
_DEFAULT_EXPRESSION_COUNT = 3
# 素材档案无细节补充时的哨兵值：与真实细节区分，避免把兜底文案当成用户补充。
_PROFILE_NO_DETAIL_SENTINEL = "以素材资料保持稳定一致"
# 素材提示词负向段的统一标签（模板与内置两条路径共用，避免字面量漂移）。
_NEGATIVE_PROMPT_LABEL = "negative prompt: "
_FIXED_VISUAL_ASPECT_RATIOS = {
    "character": "3:2",
    "prop": "3:2",
    "scene": "16:9",
    "keyframe": "16:9",
    "video": "16:9",
}
# 用户/模型可控字段拼进提示词前的清洗：换行与制表符会切断版面段落结构，
# 书名号/尖括号/引号可被用来伪造标题或指令行；正常中文标点与圆括号保留。
_FIELD_LINE_BREAK_RE = re.compile(r"[\r\n\t\u2028\u2029]+")
_FIELD_UNSAFE_CHARS = "《》〈〉<>「」『』"
_FIELD_ROLE_LABEL_RE = re.compile(r"(?i)\b(?:system|assistant|user)\s*[:：]")
_NARRATION_CUE_PATTERN = re.compile(
    r"(?:画外旁白|画外音|旁白|内心独白|OS)"
    r"(?:低声)?(?:声音?|声)?(?:开始|起|响起|声中|中|收尾|同期收尾)?"
)
_SPEECH_CUE_PATTERN = re.compile(
    r"(?:开口(?:说话|说(?:出)?(?:台词)?)?|说出(?:台词)?|说道|回答|回应|反问|问道|"
    r"质问|喊道|喊|低声(?:说)?|高声(?:说)?|轻声(?:说)?|自语|低语|说话)"
)
# 方式副词类 cue（低声/高声/轻声）单独出现时不是完整动词，
# 裸 cue 成句时补「开口」构成自然谓语，避免「她低声，口型与…」这类断句。
_MANNER_CUE_OPENINGS = {"低声": "低声开口", "高声": "高声开口", "轻声": "轻声开口"}
_QUOTED_TEXT_PATTERN = re.compile(r"[“「\"](?P<text>.*?)[”」\"]")
_TIMELINE_CLAUSE_PATTERN = re.compile(
    r"^\s*(?P<time>\d+(?:\.\d+)?\s*[-–—~－至]\s*\d+(?:\.\d+)?\s*秒\s*[:：，,])"
    r"(?P<body>.*)$"
)
# source_ref 形如「scene:1|古寺门口」，前半截是程序内部占位符，
# 直接写进画面提示词只会变成无意义 token。
_SOURCE_REF_PREFIX_PATTERN = re.compile(r"^\s*(?:scene|场景)\s*[:：]?\s*\d+\s*[|｜]\s*")
# 分镜提取残留的「场景1：/场次1：动作＋旁白」类标签不属于可入画的地点表述。
_SCENE_LABEL_PREFIX_PATTERN = re.compile(r"^\s*(?:场景|场次)\s*\d+\s*[:：\s]*")
_NON_PLACE_JUNK_PATTERN = re.compile(
    r"动作|旁白|对白|台词|景别|机位|运镜|镜头|节拍|剧本|分镜|吐槽|改稿"
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


def _flatten_prompt_parts(value: Any) -> list[str]:
    """把提示词片段规整为文本列表；容器只展开内容，不泄漏 dict/list 的 repr。"""
    if not value:
        return []
    if isinstance(value, Mapping):
        return [text for item in value.values() for text in _flatten_prompt_parts(item)]
    if isinstance(value, list | tuple | set | frozenset):
        return [text for item in value for text in _flatten_prompt_parts(item)]
    text = str(value).strip()
    return [text] if text else []


def join_non_empty(parts: Iterable[Any]) -> str:
    return "\n".join(text for part in parts for text in _flatten_prompt_parts(part))


def _sanitize_prompt_field(value: Any) -> str:
    """清洗用户/模型可控字段后再拼进提示词。

    折叠换行与制表符、去掉可用来跳出标题包裹或伪造指令行的书名号/尖括号，
    以及 system/user 之类角色标签；正常中文与圆括号保留，不改变正常取值。
    """
    text = _FIELD_LINE_BREAK_RE.sub(" ", str(value or ""))
    text = text.translate({ord(char): None for char in _FIELD_UNSAFE_CHARS})
    text = _FIELD_ROLE_LABEL_RE.sub("", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def resolve_visual_aspect_ratio(target_type: str, fallback: str) -> str:
    return _FIXED_VISUAL_ASPECT_RATIOS.get(target_type, fallback)


def style_anchor(
    style_prompt: str,
    aspect_ratio: str,
    novel_style_bridge: str = "",
    visual_bible: dict[str, Any] | None = None,
) -> str:
    # 结构化画风约束：显式视觉圣经覆盖 + 画风原文关键词自动推导（二维/水墨类）。
    # 只渲染已设置字段；写实/3D 画风推导为空时不改变既有输出。
    structured = render_visual_style_spec(
        compile_visual_bible(style_prompt, visual_bible)
    )
    return join_non_empty(
        [
            "画面美术风格：",
            f"{STYLE_ANCHOR} 核心主题：{style_prompt.strip() or '项目既定画风'}",
            "统一画风锁定：上述核心主题是唯一画风来源，角色图、场景图、物品图、分镜图与"
            "视频画面全部严格遵循，不叠加、不混入任何其他画风描述。",
            f"小说风格桥接：{novel_style_bridge.strip()}。" if novel_style_bridge.strip() else "",
            "渲染方式恒定：人物脸部渲染方式、皮肤质感、头发表现、衣服材质抽象程度、"
            "场景细节密度、阴影软硬逻辑、景深逻辑、线稿强度与写实度全片保持不变；"
            "各镜只允许在光源、色温与剧情氛围上变化，光色变化不得改变渲染方式。",
            structured or "",
            f"画面比例：{aspect_ratio}。",
        ]
    )


def apply_content_safety(prompt: str) -> str:
    compiled = prompt
    for unsafe, replacement in _SAFETY_REPLACEMENTS.items():
        compiled = compiled.replace(unsafe, replacement)
    return compiled


_TRAILING_SENTENCE_PUNCT = "。，；、"


def _strip_sentence_end(value: str) -> str:
    """去掉句子末尾的句读符号，用于字段拼接前清理。"""
    return value.rstrip(_TRAILING_SENTENCE_PUNCT)


def _normalize_sentence_end(value: str) -> str:
    """确保字段以单个句号结尾；感叹号/问号结尾保持不变。"""
    stripped = _strip_sentence_end(value)
    if stripped.endswith(("！", "？")):
        return stripped
    return f"{stripped}。"


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


def _clean_source_ref(source_ref: str) -> str:
    """剥掉 source_ref 的「scene:1|」内部占位符，只保留有信息量的位置说明。"""
    return _SOURCE_REF_PREFIX_PATTERN.sub("", str(source_ref or "").strip()).strip()


def _clean_time_place(source_ref: str) -> str:
    """从 source_ref 提取可入画的地点表述，剥离「场景1：/场次1」占位标签与桥段描述残留。"""
    text = _SCENE_LABEL_PREFIX_PATTERN.sub("", _clean_source_ref(source_ref))
    if not text:
        return ""
    # 若包含 ｜ 或 | 分割的桥段备注（如「苏棠的公寓｜凌晨改稿与吐槽段」），仅保留地点部分
    if "｜" in text or "|" in text:
        parts = re.split(r"[|｜]", text)
        valid_parts = [
            p.strip()
            for p in parts
            if p.strip()
            and not p.strip().endswith(("段", "节拍", "戏", "回合"))
            and not _NON_PLACE_JUNK_PATTERN.search(p)
        ]
        text = "，".join(valid_parts)
    pieces = [piece.strip(" 。") for piece in re.split(r"[，,、；;]", text) if piece.strip(" 。")]
    kept = [piece for piece in pieces if not _NON_PLACE_JUNK_PATTERN.search(piece)]
    return "，".join(kept)


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
    return "；".join(values) or _PROFILE_NO_DETAIL_SENTINEL


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


# 与三维渲染表达相关的默认负向词：项目画风正向启用 PBR/3D 渲染时
# （VisualStyleSpec.material.pbr 为 True）必须剔除，否则负向会反向禁用正向画风。
_PBR_RENDER_NEGATIVE_TERMS = (
    "3D建模渲染",
    "写实PBR",
    "镜面反射",
    "透明折射",
    "逼真材质贴图",
)

# 真人写实画风的正向指纹是「真人实拍质感」，与默认负向中的真人摄影类
# 排除词直接互斥，必须按媒介剔除。
_PHOTOREAL_NEGATIVE_TERMS = (
    "真人摄影",
    "产品摄影",
    "真实建筑摄影",
)


def resolve_asset_negative_prompt(
    asset_type: str,
    negative_prompt: str,
    style_spec: VisualStyleSpec | None = None,
) -> str:
    # 视觉语义重解释治理（Phase 2）：三类默认负向统一排除写实材质渲染表达，
    # 材质/光影表达方式由项目统一画风决定。
    defaults = {
        "character": "多人、身份漂移、脸型变化、发型变化、服装不一致、畸形手指、"
        "除指定人物资料卡外的其他文字、数字、英文、标识、签名、水印、乱码或无意义符号、"
        "真人摄影、产品摄影、3D建模渲染、写实PBR、镜面反射、透明折射、逼真材质贴图",
        "prop": "完整人物、人物脸、多件物品、结构错误、比例错误、材质漂移、"
        "文字、水印、真人摄影、产品摄影、3D建模渲染、写实PBR、镜面反射、透明折射、"
        "逼真材质贴图",
        "scene": f"{_SCENE_REQUIRED_NEGATIVE_PROMPT}、空间结构矛盾、门窗家具漂移、"
        "视角冲突、文字、水印、招牌店名、门头文字、真实建筑摄影、产品摄影、"
        "3D建模渲染、写实PBR、镜面反射、透明折射、逼真材质贴图",
    }
    default_prompt = defaults.get(
        asset_type,
        "文字、水印、真人摄影、产品摄影、3D建模渲染",
    )
    custom_prompt = negative_prompt.strip()
    # 所有素材类型统一合并默认负向约束，避免自定义负向替换掉默认项；
    # 合并保持幂等：默认项已存在时不重复追加。兼容中文顿号与逗号分隔。
    custom_items = [item.strip() for item in re.split(r"[、，,]", custom_prompt) if item.strip()]
    default_items = [item.strip() for item in default_prompt.split("、") if item.strip()]
    merged_items = list(custom_items)
    for item in default_items:
        if item not in merged_items:
            merged_items.append(item)
    if style_spec is not None:
        medium = style_medium_of(style_spec)
        if medium == "3d":
            # 画风正向启用 PBR/3D 渲染：剔除会反向禁用三维渲染表达的默认负向词，
            # 风格排除（真人实拍、二维平涂等）由画风指纹与 forbidden_styles 承担。
            # 无自定义负向时同样生效，否则纯默认负向仍会与正向主画风互相否定。
            merged_items = [
                item for item in merged_items if item not in _PBR_RENDER_NEGATIVE_TERMS
            ]
        elif medium == "photoreal":
            # 真人写实画风：正向指纹要求真人实拍质感，剔除默认负向中的
            # 真人摄影类排除词，避免同一条提示词正负互斥。
            merged_items = [
                item for item in merged_items if item not in _PHOTOREAL_NEGATIVE_TERMS
            ]
    return "、".join(merged_items)


def _expression_items(value: Any) -> list[str]:
    """把表情组拆成条目，供角色设定板按真实数量写特写。"""
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item or "").strip()]
    return [part.strip() for part in str(value or "").split("、") if part.strip()]


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
    expression_count = len(expression_items) or _DEFAULT_EXPRESSION_COUNT
    anchors = "、".join(consistency_keywords)
    detail_clean = detail.rstrip("。") if detail else detail
    has_detail = bool(detail_clean) and detail_clean != _PROFILE_NO_DETAIL_SENTINEL
    paragraphs = [
        f"《{name}・{concept} 角色基础设定稿》",
        "角色身份设定：\n"
        f"{name}，一位{age}的{gender}角色，身份为{identity}，生活在{worldview}中。\n"
        f"性格关键词为{personality}。\n"
        f"人物核心：{core}。",
        "外貌锁定：\n"
        f"发型{hair}；外貌{appearance}；{eyes}；常态表情{normal_expression}；"
        f"身形{body}。\n"
        f"不可变视觉锚点：{immutable}。",
        "服装锁定：\n"
        f"角色穿着{costume}，主色调为{main_color}，辅以{accent_color}少量点缀，"
        f"材质为{material}。\n"
        f"上装为{upper}；下装为{lower}；外搭为{outer}；鞋履为{shoes}；"
        f"配饰{accessories}。\n"
        "角色采用标准无道具站姿：双臂自然下垂，双手自然放松，"
        "不持武器或其他大型道具；如有核心道具，另出独立物品设定图。\n"
        "整体造型与项目统一画风锚定完全一致，具有鲜明辨识度，"
        "同时适合后续AI漫剧分镜图和视频生成时保持角色一致性。",
        f"细节补充：{detail_clean}。" if has_detail else "",
        "版式结构：\n"
        "整张角色设定展板使用与项目统一画风协调的干净底色（默认纯白；水墨、国画"
        "或古风类画风使用宣纸米白或留白底色），背景干净、明亮、极简、通透。"
        "不要渐变背景，不要杂乱底纹，不要环境场景，不要室内或室外布景。\n"
        "所有人物模块均置于统一设定稿底色中，不出现任何环境、布景或场景元素。\n"
        "仅使用极细分隔线建立版面秩序，分隔线颜色与项目统一画风协调，"
        "不使用厚重边框、大面积色块、复杂花纹或抢眼装饰。\n"
        f"采用3:2横向宽幅、分块拼接式专业角色设定面板排版。画面只展示{name}同一名角色，"
        "不出现其他人物。\n"
        "面板只保留人物介绍、主人物立绘、三视图和核心面部特写四类信息，"
        "不加入服装拆解、配饰拆解、发型拆解、道具拆解、场景插图或意境参考图。\n"
        "整体呈现本项目统一画风下的官方角色设定面板质感。",
        "各模块内容：\n"
        "左上角叠放或邻接一个非常简洁的人物信息卡，不遮挡身体，"
        "与左侧主人物立绘形成一个完整区域。人物资料卡内容为：\n"
        f"角色姓名：{name}\n"
        f"身份定位：{identity}\n"
        f"人物核心：{core}\n"
        "人物资料卡采用现代官方设定稿式排版，标题、姓名和正文具有清晰的字号层级，"
        "文字区域留白舒适、排列整齐。中文文字尽量清晰、准确、可读，"
        "不要乱码、错字、重复文字或无意义符号。\n"
        f"主人物立绘位于画面左侧主体区域，并在该区域内部居中完整站立，占整张画面约42%。"
        "完整展示头部、五官、发型、服装、鞋履和随身配饰；"
        "人物从头顶到脚底全部处于画面内，不裁切头部、手臂、腿部或鞋履，"
        "不被人物资料卡和其他模块遮挡。\n"
        "右上约58%×55%区域规整排列三张标准全身视图，依次为正面全身、标准侧面全身、"
        "背面全身。三张视图人物比例稍小，但必须完整展示鞋子。"
        "三张视图采用统一自然站姿、统一身体比例、统一服装、统一发型和统一光照，"
        "清楚展示人物正面轮廓、侧面结构、背部造型和服装前后结构；"
        "每张视图均保持完整全身，不裁切、不遮挡、不采用夸张透视。\n"
        f"右下约58%×45%区域排列{expression_count}个面部特写，"
        f"分别为{expression_group}。"
        f"{expression_count}个面部特写必须保持完全一致的脸型、骨相、五官比例、年龄感、"
        "肤色、发型和发色，仅通过眼神、眉形、眼睑和嘴角的细微变化表现状态差异，"
        "表情自然细腻，不夸张、不低幼、不颜艺化。",
        "角色一致性要求：\n"
        f"主人物立绘、三视图和{expression_count}个面部特写必须严格表现为同一个{name}。"
        "所有模块保持完全一致的脸型、五官比例、年龄感、肤色、身体比例、发型结构、发色、"
        "服装款式、服装颜色、材质和核心视觉识别点。\n"
        "不得生成不同版本的角色，不得改变年龄、脸型、服装或发型，不得随机增加或删除配饰。"
        + f"三视图与特写小图必须严格保持主立绘的{costume}与{main_color}服装固有色，"
        "严禁任何模块更换为白色或其他颜色服装，严禁脱去外搭或露异色内衬，整张设定板服装颜色绝对统一。"
        + (f"一致性锚点：{anchors}。" if anchors else ""),
        f"【角色身份唯一性】整张设定板不是多个相似人物，而是同一人物{name}的多角度、"
        "多表情记录。所有模块共享同一角色身份编码。禁止因视角、表情或尺寸变化导致"
        "脸型重绘、年龄变化、美型程度变化、发量变化、发际线变化或服装重新设计。",
        f"构图补充：{composition.rstrip('。')}。" if composition else "",
        "灯光与色彩氛围：\n"
        "只陈述光源方向与时间等事实，不写布光、氛围、造型气质或妆容画法；"
        "光影表达方式完全由项目统一画风决定。"
        f"角色固有色以{main_color}和{accent_color}为核心。",
        "画质与成图要求：\n"
        "全画面保持清晰，材质以二维笔触与色块概括，人物比例与五官结构遵循设定板；"
        "发丝与织物以概括笔触表达，阴影通透干净，"
        "避免阴影死黑、画面脏灰或塑料感。无景深模糊，无低清缩略图感，无杂色噪点，无脏污背景。\n"
        "无其他人物，无不同版本角色，无重复头部，无多余肢体，无手脚缺失，无错误手指，"
        "无肢体粘连，无五官错位，无年龄漂移，无比例失衡，无服装变化，无发型变化，"
        "无服装穿模。版面模块对齐，间距统一，信息完整但不拥挤，留白舒适，视觉层级明确。",
        f"{_NEGATIVE_PROMPT_LABEL}{negative_prompt}" if negative_prompt else "",
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
    has_detail = bool(detail_clean) and detail_clean != _PROFILE_NO_DETAIL_SENTINEL
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
        "材质降维（只记录事实，不写画法）：\n"
        "金属、玻璃、塑料、屏幕等材质只记录结构事实：是否透明、是否发光、表面光滑或哑光、"
        "颜色与磨损。\n"
        "不使用真实PBR反射、镜面高光、透明折射、逼真材质贴图等渲染表达；"
        "材质表达方式完全由项目统一画风决定。",
        f"细节补充：{detail_clean}。" if has_detail else "",
        "版面背景与整体要求：\n"
        "整张物品设定展板使用与项目统一画风协调的干净底色（默认纯白；水墨、国画"
        "或古风类画风使用宣纸米白或留白底色），背景干净、明亮、极简、通透。"
        "不要渐变背景，不要杂乱底纹，不要完整环境场景。\n"
        "仅使用极细分隔线建立版面秩序，分隔线颜色与项目统一画风协调。"
        "整体呈现本项目统一画风下的官方道具设定面板质感。",
        "版面排布结构：\n"
        f"采用3:2横向宽幅、分块拼接式专业道具设定面板。画面只展示同一件{name}，"
        "不出现其他无关道具，不出现完整人物角色，不出现完整环境场景。\n"
        "面板只保留物品介绍、主物品展示图、标准多视图、三个关键细节"
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
        "资料卡采用官方道具设定稿式排版，标题与正文层级清晰，文字区域留白舒适。"
        "中文尽量清晰、准确、可读，不要乱码、错字或无意义符号。",
        "左侧主物品展示图：\n"
        f"左侧主体区域放置一张尺寸最大的{name}立体展示图，占整张画面约40%至45%。\n"
        "采用四十五度略微俯视视角，同时展示正面、顶部和侧面结构，完整表现物品轮廓、"
        "厚度和关键部件。\n"
        "物品不裁切、不遮挡、不变形，不使用夸张透视，是整张面板的视觉中心。"
        "主体材质的光滑度、反光与透明表现只按材质事实（光滑/哑光/透明/发光）受控呈现，"
        "不使用真实PBR反射、镜面高光、透明折射或逼真材质贴图，"
        "材质表达方式完全由项目统一画风决定。",
        "右上区域标准多视图：\n"
        "右上区域规整排列四个标准视图：正面视图、侧面视图、背面视图、顶部或底部视图。\n"
        "四个视图保持完全一致的尺寸比例、轮廓、结构、颜色、材质、磨损和部件位置，"
        "不得出现不同型号或不同版本。",
        f"右侧中部关键细节：\n三个局部细节：{detail_zone}。",
        "右下区域尺寸与使用比例：\n"
        f"展示物品标准尺寸标注与使用比例参考：{scale}。"
        "所有比例参考中的物品外观必须保持完全一致。",
        "物品一致性要求：\n"
        f"主物品图、多视图、关键细节和比例参考必须严格表现为同一件{name}。"
        "所有模块保持完全一致的形状、尺寸比例、颜色、材质、结构、磨损和划痕。\n"
        "不得生成不同型号，不得随机改变颜色，不得增减部件，不得改变磨损痕迹的位置、"
        "方向或长度。" + (f"一致性锚点：{anchors}。" if anchors else ""),
        f"构图补充：{composition.rstrip('。')}。" if composition else "",
        "灯光与画质：\n"
        "灯光只用于清楚展示物品轮廓、厚度与材质等事实，光影表达方式完全由项目统一画风决定，"
        "不指定主光、轮廓光等布光方案。\n"
        "画面整体清晰通透，材质以二维笔触与色块概括（金属、木质、皮革与晶石只用概括的线条"
        "与色块表达质感），结构边缘受控明确，避免脏暗与死黑。"
        "全画面无景深模糊，无低清区域，无多余物品，无不同型号，无重复部件，"
        "无结构错位，无悬浮零件，无错误透视，无材质混乱，无随机文字或品牌标志。"
        "整张设定面板背景底色保持统一（默认纯白；水墨、国画或古风类画风使用"
        "宣纸米白或留白底色），版面模块对齐，间距统一，留白舒适。",
        f"{_NEGATIVE_PROMPT_LABEL}{negative_prompt}" if negative_prompt else "",
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
        "不得随机增加无关陈设、装饰或复杂文字标识。"
        "任何招牌、门头、指示牌或屏幕只保留色块与材质，不出现店名、logo、英文或符号。",
        "材质与色彩设定：\n"
        f"固有色：主色{main_color}，点缀色{accent_color}（只记录墙体、家具与陈设的固有颜色事实，"
        "如「门为暗朱红」）。"
        "色彩表达方式完全由项目统一画风决定，不使用杂乱无规律的颜色。",
        f"环境补充细节：{environment_detail}。" if environment_detail else "",
        "版面背景与整体要求：\n"
        "整张场景设定展板使用与项目统一画风协调的干净底色（默认纯白；水墨、国画"
        "或古风类画风使用宣纸米白或留白底色），背景干净、明亮、极简、通透。"
        "不要渐变底色，不要杂乱底纹，不要将完整环境延伸为面板背景。\n"
        "仅使用极细分隔线建立版面秩序，分隔线颜色与项目统一画风协调。"
        "整体呈现本项目统一画风下的官方场景设定面板质感。",
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
        "资料卡采用官方场景设定稿式排版，标题与正文层级清晰，"
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
        "不得出现镜像颠倒或空间结构冲突。" + (f"一致性锚点：{anchors}。" if anchors else ""),
        f"构图补充：{environment_composition.rstrip('。')}。" if environment_composition else "",
        "灯光与时间事实：\n"
        f"只陈述时间{time_of_day}、天气{weather}与光源方向{light_direction}等受控事实；"
        "不写主光效果、辅光、氛围或色彩气质等自由艺术指导，"
        "光影与色彩表达方式完全由项目统一画风决定。\n"
        "各视图保持一致的光照方向，画面清楚展示空间结构和材质事实，"
        "不让暗部吞没陈设结构。",
        "画质与成图要求：\n"
        "全画面清晰通透，以二维笔触与色块表现空间纵深；"
        "木材、石材、金属、玻璃等材质只以笔触与色块概括，环境光过渡自然，"
        "暗部保留结构细节，避免画面脏暗或阴影死黑。"
        "全画面无景深模糊，无低清缩略图感，无脏污背景，无人物，无人体残影，"
        "无错误透视，无弯曲墙面，无悬浮家具。\n"
        "最终检查：所有面板均不得出现人物、人影、剪影、倒影中的人物或人体局部。"
        "版面模块对齐，间距统一，信息完整但不拥挤，留白舒适，视觉层级明确。",
        f"{_NEGATIVE_PROMPT_LABEL}{negative_prompt}" if negative_prompt else "",
    ]
    return "\n\n".join(paragraph for paragraph in paragraphs if paragraph.strip())


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
    use_template: bool = True,
    visual_bible: dict[str, Any] | None = None,
) -> str:
    # 素材名与用户补充细节由用户/模型可控，拼进提示词前先清洗，
    # 避免换行或书名号把内容顶出标题、伪造指令行（模板与内置两条路径共用清洗结果）。
    asset_name = _sanitize_prompt_field(asset_name)
    prompt = _sanitize_prompt_field(prompt)
    style_spec = compile_visual_bible(style_prompt, visual_bible)
    effective_negative_prompt = merge_visual_bible_negative(
        resolve_asset_negative_prompt(asset_type, negative_prompt, style_spec),
        style_spec,
    )
    effective_aspect_ratio = resolve_visual_aspect_ratio(asset_type, aspect_ratio)
    detail = _asset_detail(prompt, profile)
    if use_template and asset_type in {"character", "prop", "scene"}:
        # 设定图正文优先由落盘模板渲染；内置函数保留为 use_template=False 的回退路径。
        # 延迟导入避免与 prompt_template_renderer 顶部的 prompt_service 导入成环。
        from app.services.prompt_template_renderer import render_asset_body

        body = render_asset_body(
            asset_type=asset_type,
            asset_name=asset_name,
            profile=profile,
            prompt=prompt,
            negative_prompt=effective_negative_prompt,
            consistency_keywords=consistency_keywords,
            composition=composition,
        )
        return apply_content_safety(
            join_non_empty(
                [
                    style_anchor(
                        style_prompt,
                        effective_aspect_ratio,
                        visual_bible=visual_bible,
                    ),
                    body,
                ]
            )
        )
    template = {
        "character": _character_setting_sheet,
        "prop": _prop_setting_sheet,
        "scene": _scene_setting_sheet,
    }.get(asset_type)
    if template is None:
        body = join_non_empty(
            [detail, f"{_NEGATIVE_PROMPT_LABEL}{effective_negative_prompt}"]
        )
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
        join_non_empty(
            [
                style_anchor(style_prompt, effective_aspect_ratio, visual_bible=visual_bible),
                body,
            ]
        )
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
    visual_bible: dict[str, Any] | None = None,
) -> str:
    style_spec = compile_visual_bible(style_prompt, visual_bible)
    effective_negative_prompt = merge_visual_bible_negative(negative_prompt, style_spec)
    effective_aspect_ratio = resolve_visual_aspect_ratio("keyframe", aspect_ratio)
    opening_frame = _value(shot_context.get("opening_frame"), image_prompt)
    characters = _value(shot_context.get("characters"), "当前分镜角色")
    props = _value(shot_context.get("props"), "")
    positions = _value(shot_context.get("positions"), "")
    expressions = _value(shot_context.get("expressions"), "")
    dialogue = _value(shot_context.get("dialogue"), "")
    voice_over = _value(shot_context.get("voice_over"), "")
    shot_type = _value(shot_context.get("shot_type"), "中景")
    camera_angle = _value(shot_context.get("camera_angle"), "平视")
    # 无人空镜：本镜未绑定角色、也没有对白/画外音（此时画面确实无人），
    # 或景别明确标记「空镜」时，所有人物相关要求（人物体量占比、表情、
    # 双手与手持道具等）一律不适用，避免与空镜定义冲突。
    empty_shot = "空镜" in shot_type or not bool(
        _value(shot_context.get("characters"), "")
        or _value(shot_context.get("dialogue"), "")
        or _value(shot_context.get("voice_over"), "")
    )
    # frame_description 只作静态构图参考；动作时间轴（action）属于视频提示词，
    # 不得作为关键帧的「焦点」内容。
    composition = _value(shot_context.get("composition"), "")
    environment_details = str(shot_context.get("environment_details") or "").strip()
    # 完成精度句按画风媒介分支：真人写实/3D 项目不重复罗列反写实排除词，
    # 避免与画风指纹互相打架；风格排除交给画风指纹与负向词。
    style_medium = style_medium_of(style_spec)
    if style_medium == "2d":
        completion_precision_line = (
            "人物、场景与物品的完成精度保持统一，不呈现真人摄影、影视剧照、三次元演员、"
            "写实实拍或过度三维游戏CG质感。"
        )
    elif style_medium == "photoreal":
        subject = "场景与物品" if empty_shot else "人物、场景与物品"
        completion_precision_line = (
            f"{subject}的完成精度保持统一，画面质感与渲染方式以【参考】段画风指纹为准，"
            "不叠加二维手绘或三维CG渲染质感。"
        )
    else:
        # 3D 画风与未知媒介：不罗列任何排除词，只要求精度与画风指纹恒定。
        subject = "场景与物品" if empty_shot else "人物、场景与物品"
        completion_precision_line = (
            f"{subject}的完成精度保持统一，画面质感与渲染方式以【参考】段画风指纹为准，"
            "不叠加其他渲染质感。"
        )
    raw_time_place = str(shot_context.get("time_place") or "").strip()
    raw_scene_name = _value(shot_context.get("scene_name"), "当前分镜场景")
    if (
        raw_time_place
        and raw_time_place != raw_scene_name
        and raw_time_place not in {"以当前场景资料为准", "以当前场景为准", "无", "未提供"}
    ):
        time_place_clause = f"时间地点：{raw_time_place}。\n"
    elif not shot_context.get("scene_name"):
        time_place_clause = f"时间地点：{_value(raw_time_place, '当前场景')}。\n"
    else:
        time_place_clause = ""

    wide_shot_body_rule = (
        f"\n本镜为{shot_type}，人物仍需保持清晰可辨识的体量，约占画面高度的1/5至1/4，"
        "可明确识别其发型、服装与手持道具，不得缩成远景小黑点。"
        if not empty_shot
        and ("全景" in shot_type or "远景" in shot_type)
        and "大远景" not in shot_type
        else ""
    )
    character_costumes = shot_context.get("character_costumes") or {}
    costume_clauses: list[str] = []
    if isinstance(character_costumes, dict):
        for char_name, desc in character_costumes.items():
            if desc:
                costume_clauses.append(f"{char_name}（{desc}）")
    costume_line = (
        f"\n服装锁定：{'；'.join(costume_clauses)}。画面中人物必须严格穿着指定的服装，"
        "主色调与款式保持一致，严禁出现异色服装、脱去外搭或服装变色。"
        if costume_clauses
        else ""
    )
    paragraphs = [
        "【参考】\n"
        + style_anchor(style_prompt, effective_aspect_ratio, visual_bible=visual_bible)
        + "\n"
        + "\n".join(anchors)
        + (
            "\n只使用以上当前分镜涉及的角色、场景和道具素材图锁定身份与空间，"
            "不得引入其他镜头的人物、场景或道具。"
            if anchors
            else "\n当前分镜没有可用绑定参考图，按文字描述生成并保持内部一致。"
        )
        + (
            "\n参考图职责隔离：角色图、场景图与物品图只提供身份、服装、空间结构与"
            "道具外观等事实信息；参考图自带的渲染质感、笔触、材质与光影风格"
            "不得覆盖【参考】段画风指纹，最终画面严格按画风指纹渲染。"
            if anchors
            else ""
        )
        + (
            "\n【角色参考图基准】：若绑定的角色素材图为包含三视图或表情特写的设定展板，"
            "必须以左侧/主体区域的完整主立绘服装与固有颜色为唯一基准；"
            "严禁参考特写小图、局部视图或底色中的浅色/异色服装，人物服装款式与颜色必须严格保持一致。"
            if any(
                "角色" in anchor or "character" in anchor or "五官" in anchor or "发型" in anchor
                for anchor in anchors
            )
            else ""
        ),
        "【剧情时刻】\n"
        f"{image_prompt.rstrip('。')}。\n"
        "本镜画面完全由【首帧关键构图】确定，剧情时刻中的完整行为链（先…再…、随后、然后、正在…等）只作为剧情上下文，不得进入画面；"
        + (
            "精确呈现人物现在在哪里、现在什么姿势、现在看哪里与此刻神态，时间过程留给后续视频。"
            if not empty_shot
            else "精确呈现场景此刻的空间结构、陈设与光线状态。"
        ),
        "【人物与动作】\n"
        + (
            f"人物：{characters}。"
            + costume_line
            + (
                f"\n站位、朝向与视线：{positions}。画面中人物的左右位置关系、朝向与视线落点"
                "必须与上述描述一致，不得左右颠倒或镜像翻转。"
                if positions
                else ""
            )
            + (
                f"\n对白与口型状态：本镜对白为「{dialogue}」。"
                "本帧锁定对白开始前的短暂停顿瞬间，画面中所有人物均闭口，不呈现说话口型。"
                if dialogue
                else ""
            )
            + (
                f"\n画外音（后期配音，画面人物闭口）：{voice_over.rstrip('。')}。"
                "本镜台词来自录音或旁白，画面中所有人物全程闭口，不产生说话口型。"
                if voice_over
                else ""
            )
            if not empty_shot
            else "本镜为无人空镜：画面中不出现任何人物、路人、行人、人影或人体局部，"
            "人物站位、朝向、表情、口型与手持道具等要求一律不适用。"
        ),
        "【场景与空间关系】\n"
        + time_place_clause
        + f"场景：{raw_scene_name}。"
        + (
            "空间结构、门窗位置、家具摆放与陈设布局沿用绑定参考图，"
            "门窗开启方向、铰链侧、门锁侧与室内—走廊/室外的方位关系必须和参考图完全一致；"
            "不得随机改变房间朝向、移动陈设、增加无关物品或其他人物，不得将场景镜像翻转。"
            if any("锁定场景空间结构" in anchor for anchor in anchors)
            else "空间结构、门窗位置、家具摆放与陈设布局按本提示词文字设定生成，"
            "并保证各模块内部一致；不得随机改变房间朝向、移动陈设、增加无关物品或其他人物，"
            "不得将场景镜像翻转。"
        ),
        (
            "【关键物品】\n"
            f"物品：{props}。"
            "有绑定参考图的物品严格沿用参考图的外观、尺寸、颜色与磨损痕迹；"
            "未绑定参考图的物品按描述生成，采用普通无品牌外观，不出现Logo、文字、标签。"
            "物品不放大、不缩小、不变形、不漂浮、不穿模、不遮挡人物关键动作。"
            if props
            else ""
        ),
        "【镜头构图】\n"
        f"景别{shot_type}，机位{camera_angle}。\n"
        + (
            f"构图：{composition.rstrip('。')}。\n"
            if composition and composition.rstrip("。") not in image_prompt
            else ""
        )
        + f"首帧关键构图：{opening_frame.rstrip('。')}。\n"
        "本帧只呈现首帧关键构图所描述的、已经稳定达成的这一个静态状态，不得提前呈现动作完成结果。"
        f"{wide_shot_body_rule}\n"
        "用途：生成当前分镜唯一的分镜图，并作为图生视频严格第0秒起始帧。",
        (
            "【人物神态与情绪】\n"
            + (
                f"{expressions}。表情自然细腻、克制真实，不使用夸张颜艺，"
                "不瞪大双眼，不张大嘴，情绪强度适中。"
                if expressions
                else "人物表情自然克制，情绪强度适中，不使用夸张颜艺。"
            )
            if not empty_shot
            else ""
        ),
        "【灯光与色彩】\n"
        f"{_value(shot_context.get('lighting'), '符合场景设定的光线')}，"
        f"{_value(shot_context.get('color_tone'), '统一色调')}"
        + (f"，环境细节：{environment_details}" if environment_details else "")
        + "。\n"
        "光线符合场景设定的真实光源逻辑，"
        + (
            "清晰照亮人物五官、双手与关键物品，"
            if not empty_shot
            else "清晰照亮场景结构与关键陈设，"
        )
        + "不让暗部吞没剧情信息，不产生无来源的强光或过度戏剧化阴影。\n"
        "光源与色温允许随剧情变化，但渲染方式、皮肤质感、线稿强度、阴影软硬与"
        "景深逻辑等画风指纹保持不变；色温变化不等于画风变化。",
        "【画面美术质感】\n"
        "严格按照【参考】段画风指纹渲染，各镜指纹恒定，不因场景、灯光或新增角色参考图"
        "而改变；"
        + (
            "面部和手部结构准确，场景空间清晰，材质区分明确，光影符合场景逻辑。\n"
            if not empty_shot
            else "场景空间清晰，材质区分明确，光影符合场景逻辑。\n"
        )
        + (
            "人物双手与关键手持道具清晰可见，手指结构完整，不被衣袖、道具或画面边缘裁切。\n"
            if not empty_shot
            else ""
        )
        + completion_precision_line,
        f"【连续性要求】\n{continuity_instruction}" if continuity_instruction.strip() else "",
        "【成图限制】\n"
        "画面只表现当前一个剧情瞬间，不制作多格漫画，不制作设定展板，"
        "不制作动作分解图，不在同一画面重复展示同一角色，"
        "不做角色海报、概念插画封面或设定展示页构图。\n"
        + (
            "只出现本镜绑定的角色，不出现背景行人、看客、路人或模糊人影。\n"
            if not empty_shot
            else "本镜为无人空镜，不出现任何人物、行人、看客、路人或模糊人影。\n"
        )
        + "无人物漂浮，无物品漂浮，无穿模，无物品尺寸异常，无家具移动，无门窗错位，"
        "无场景镜像翻转，无错误透视，无服装变色，无不同颜色服装。\n"
        "画面中不出现字幕、对白、对话框、旁白文字、水印、品牌标志、"
        "文件文字、乱码或无意义符号。"
        + (
            f"\n{_NEGATIVE_PROMPT_LABEL}{effective_negative_prompt}"
            if effective_negative_prompt.strip()
            else ""
        ),
    ]
    return apply_content_safety(
        "\n\n".join(paragraph for paragraph in paragraphs if paragraph.strip())
    )


def shot_template_context(
    current: dict[str, Any],
    asset_names: dict[str, str],
    prev_end_state: str = "",
    character_costumes: dict[str, str] | None = None,
) -> dict[str, Any]:
    dialogue_parts: list[str] = []
    dialogue_entries: list[dict[str, str]] = []
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
            # speaker 已表明来源，text 若重复带同名前缀则剥掉，避免「旁白：旁白：正文」
            deduped = text
            for prefix in (f"{speaker}：", "旁白：", "画外音：", "画外旁白："):
                while deduped.startswith(prefix):
                    deduped = deduped[len(prefix) :].lstrip()
            voice_over_parts.append(f"{speaker}：{deduped}" if deduped else f"{speaker}：{text}")
        else:
            dialogue_entries.append({"speaker": speaker, "text": text})
            dialogue_parts.append(f"{speaker}：{text}")
    if current.get("narration"):
        narration = str(current.get("narration") or "").strip()
        # narration 内容常已自带「旁白：」「画外音：」前缀，模板外层还会套
        # 「画外音（后期配音，画面人物闭口）：」，此处剥掉内层重复前缀，
        # 避免叠成「画外音（…）：画外旁白：旁白：正文」三层标签
        for prefix in ("画外旁白：", "画外音：", "旁白：", "OS：", "内心独白："):
            while narration.startswith(prefix):
                narration = narration[len(prefix) :].lstrip()
        if narration:
            voice_over_parts.append(narration)
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
        details = [
            d
            for d in details
            if d
            and d not in {
                "无",
                "未提供",
                "none",
                "None",
                "以素材为准",
                "暂无",
                "无明确方向",
                "以当前场景为准",
            }
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
        "time_place": _clean_time_place(current.get("source_ref", "")),
        # previous_state 保持原语义（本镜起始状态），供其他模板段落使用；
        # 上一镜结束状态单独放在 prev_end_state，仅作前情交代
        "previous_state": current.get("start_state", ""),
        "prev_end_state": prev_end_state,
        # 衔接类型决定【上一镜结束状态】段的措辞，以及是否下发上一镜末帧
        "transition_from_prev": current.get("transition_from_prev") or "scene_change",
        # 关键帧必须以动作开始前的静态状态为准，不得把动作中段或结果画进首帧
        "opening_frame": current.get("start_state") or current.get("frame_description", ""),
        "ending_frame": current.get("end_state") or current.get("next_hook", ""),
        "characters": [
            asset_names[item] for item in current.get("character_ids", []) if item in asset_names
        ],
        "character_costumes": character_costumes or {},
        "scene_name": asset_names.get(current.get("scene_asset_id", ""), ""),
        "props": [
            asset_names[item] for item in current.get("prop_ids", []) if item in asset_names
        ],
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
        "dialogue_entries": dialogue_entries,
        "voice_over": "；".join(voice_over_parts),
        "sound_effects": str(current.get("sound_effects") or ""),
        # 环境动态只按需下发：不写时视频渲染器不添加通用环境微动（树叶轻摆等），
        # 避免背景元素全员微动带来的 AI 插值感。
        "environment_motion": str(current.get("environment_motion") or ""),
    }


def _clean_speech_prefix(prefix: str, dialogue_speakers: set[str]) -> str:
    """保留说话片段中的可视动作，去掉会把代词误当说话人的前缀。"""
    cleaned = prefix.strip(" ，,；;")
    timeline_match = _TIMELINE_CLAUSE_PATTERN.match(cleaned)
    if timeline_match:
        body = timeline_match.group("body").strip(" ，,；;")
        if body in dialogue_speakers or body in {"他", "她", "其", "人物", "角色"}:
            body = ""
        body = re.sub(
            r"(?:平静|自然|低声|轻声|高声|压低声音|一字一句|缓慢)地$",
            "",
            body,
        ).rstrip(" ，,")
        return f"{timeline_match.group('time')}{body}"
    return cleaned


def _join_prompt_clauses(*parts: str) -> str:
    cleaned = [part.strip(" ，,；;") for part in parts if part.strip(" ，,；;")]
    if len(cleaned) == 2 and cleaned[0].endswith(("：", ":")):
        return f"{cleaned[0]}{cleaned[1]}"
    return "；".join(cleaned)


def _strip_narration_clause(text: str) -> str:
    """从动作时间轴中移除旁白文本，同时保留旁白前后的可视动作。"""
    match = _NARRATION_CUE_PATTERN.search(text)
    if not match:
        return text.strip()

    before = text[: match.start()].strip(" ，,")
    after = text[match.end() :].lstrip()
    if after.startswith(("：", ":")):
        after = after[1:].lstrip()
        quoted = _QUOTED_TEXT_PATTERN.match(after)
        if quoted:
            after = after[quoted.end() :].lstrip(" ，,。；;")
        else:
            after = ""
    elif after.startswith(("“", "「", '"')):
        quoted = _QUOTED_TEXT_PATTERN.match(after)
        after = after[quoted.end() :].lstrip(" ，,。；;") if quoted else ""
    elif after.startswith(("（", "(")):
        closing = "）" if after.startswith("（") else ")"
        closing_index = after.find(closing, 1)
        after = after[closing_index + 1 :].lstrip(" ，,。；;") if closing_index >= 0 else ""
    elif after.startswith(("，", ",")):
        after = after[1:].lstrip()

    if before in {"与此同时", "同时", "同期", "此时"}:
        before = ""
    return _join_prompt_clauses(before, after)


def _squash_dialogue_text(value: str) -> str:
    return re.sub(r"\s+", "", value)


def _match_dialogue_by_quote(
    quoted_text: str,
    dialogue_entries: list[dict[str, str]],
    unconsumed: set[int],
) -> int | None:
    """用时间轴中的引号原文精确匹配未消费的对白条目，防止说话顺序被调整时错位。"""
    needle = _squash_dialogue_text(quoted_text)
    if not needle:
        return None
    for index in sorted(unconsumed):
        if _squash_dialogue_text(dialogue_entries[index]["text"]) == needle:
            return index
    return None


def _replace_speech_clause(
    text: str,
    dialogue_entries: list[dict[str, str]],
    unconsumed: set[int],
) -> str:
    """用结构化对白序号替换自由文本中的说话人/引号，避免角色串台。

    口型编号优先绑定引号原文能精确命中的未消费条目；匹配不到时退化为
    按出现顺序取下一个未消费条目。
    """
    speech_match = _SPEECH_CUE_PATTERN.search(text)
    quoted = _QUOTED_TEXT_PATTERN.search(text)
    if not speech_match and not quoted:
        return text.strip()

    if (
        not dialogue_entries
        and speech_match
        and any(marker in text[: speech_match.start()] for marker in ("背景音", "环境声", "声音"))
    ):
        return f"{text[:speech_match.start()]}环境声{text[speech_match.end() :]}"

    boundary = speech_match.start() if speech_match else quoted.start()
    prefix = _clean_speech_prefix(text[:boundary], {item["speaker"] for item in dialogue_entries})
    suffix_start = quoted.end() if quoted and quoted.start() >= boundary else (
        speech_match.end() if speech_match else boundary
    )
    suffix = text[suffix_start:].strip(" ，,；;")

    selected: int | None = None
    if quoted:
        selected = _match_dialogue_by_quote(quoted.group("text"), dialogue_entries, unconsumed)
    if selected is None and unconsumed:
        selected = min(unconsumed)
    if selected is None:
        replacement = "人物保持闭口，不生成台词"
    else:
        unconsumed.discard(selected)
        replacement = f"按第{selected + 1}句对白执行对应口型"
    if not quoted and speech_match and selected is not None:
        # 裸说话动词（无引号原文）：把动词本身保留成自然的开口动作，直接接在
        # 主语前缀之后、其余动作用逗号衔接；否则分号会把句子切成
        # 「她；口型与第1句对白同步；话」这类互相独立的碎片指令。
        cue = _MANNER_CUE_OPENINGS.get(speech_match.group(0), speech_match.group(0))
        suffix = re.sub(r"^[道话][，,]?", "", suffix)
        result = f"{prefix}{cue}，{replacement}" if prefix else f"{cue}，{replacement}"
        if suffix:
            result = f"{result}，{suffix}"
        return result
    if prefix.endswith(("：", ":")):
        result = f"{prefix}{replacement}"
    elif prefix:
        result = f"{prefix}；{replacement}"
    else:
        result = replacement
    if suffix:
        result = f"{result}；{suffix}"
    return result


def _sanitize_video_prompt_speech(
    video_prompt: str,
    dialogue_entries: list[dict[str, str]],
) -> str:
    """只保留视觉动作时间轴，实际对白统一由结构化对白段落提供。

    时间轴中的口型编号按「引号原文精确匹配 → 出现顺序兜底」绑定到具体对白，
    避免模型调整说话顺序时把编号错配到相邻句子。
    """
    clauses = re.split(r"[；;]", str(video_prompt or "").strip())
    cleaned_clauses: list[str] = []
    unconsumed = set(range(len(dialogue_entries)))
    for clause in clauses:
        current = _strip_narration_clause(clause.strip())
        if not current:
            continue
        current = _replace_speech_clause(
            current,
            dialogue_entries,
            unconsumed,
        )
        if current:
            cleaned_clauses.append(current)
    return "；".join(cleaned_clauses)


# 这些视频模型只接受整数秒时长（且最短 6 秒）；预览与实际入队必须使用同一
# 有效时长编译时间轴，否则页面展示与提交给模型的提示词对不上。
_INTEGER_DURATION_VIDEO_MODELS = {
    "grok-imagine-1.5-video-apimart",
    "grok-imagine-1.5-video-ext",
}
_MIN_INTEGER_VIDEO_DURATION_SEC = 6


def effective_video_duration(model: str, duration_sec: float) -> float | int:
    if model.strip().lower() in _INTEGER_DURATION_VIDEO_MODELS:
        return max(_MIN_INTEGER_VIDEO_DURATION_SEC, math.ceil(duration_sec))
    return duration_sec


def _render_with_director_plan(
    *,
    style_prompt: str,
    aspect_ratio: str,
    director_plan: dict[str, Any],
    shot_context: dict[str, Any],
    anchors: list[str],
    negative_prompt: str,
    visual_bible: dict[str, Any] | None = None,
    audio_driven: bool = False,
) -> str:
    """用导演计划渲染 10 段最终提示词；时间轴为空或结构非法时返回空串由调用方回退。"""

    try:
        bundle = director_bundle_from_llm(director_plan)
    except (ValidationError, ValueError, TypeError) as exc:
        # 存储中的导演计划若结构非法（如时间轴段 end<=start、时间值不可解析），
        # 回退旧版渲染而不是让分镜列表整体 500；降级原因记日志便于排查。
        logger.warning("导演计划解析失败，已回退自然语言渲染：%s", exc)
        return ""
    if not bundle.timeline:
        return ""
    initial_state = _value(shot_context.get("opening_frame"), "")
    end_state = _value(shot_context.get("ending_frame"), "")
    shot_type = _value(shot_context.get("shot_type"), "中景")
    hard_constraints: list[str] = []
    cleaned_negative = re.sub(
        r"^禁止出现\s*[:：]\s*",
        "",
        (negative_prompt or "").strip(),
    ).rstrip("。")
    if cleaned_negative:
        hard_constraints.append(cleaned_negative)
    return render_director_video_prompt(
        style_prompt=style_prompt,
        aspect_ratio=aspect_ratio,
        director_plan=bundle.director_plan,
        timeline=bundle.timeline,
        dialogue_bindings=bundle.dialogue_bindings,
        initial_state=initial_state,
        end_state=end_state,
        shot_type=shot_type,
        anchors=anchors,
        hard_constraints=hard_constraints,
        visual_bible=visual_bible,
        audio_driven=audio_driven,
        transition_from_prev=_value(
            shot_context.get("transition_from_prev"), "scene_change"
        ),
        prev_end_state=_value(shot_context.get("prev_end_state"), ""),
        scene_name=_value(shot_context.get("scene_name"), ""),
        # 单角色镜头不渲染「其余角色保持聆听」，避免诱导模型补出第二个倾听者。
        multi_character=len(shot_context.get("characters") or []) > 1,
    )


def compile_video_prompt(
    *,
    style_prompt: str,
    aspect_ratio: str,
    video_prompt: str,
    duration_sec: float,
    anchors: list[str],
    shot_context: dict[str, Any],
    negative_prompt: str = "",
    director_plan: dict[str, Any] | None = None,
    reference_tokens: list[dict[str, Any]] | None = None,
    visual_bible: dict[str, Any] | None = None,
    audio_driven: bool = False,
) -> str:
    """生成视频提示词。

    有导演计划时渲染导演设计层结构；否则用自然语言生成器把结构化分镜事实
    渲染成导演口吻的自然语言提示词（不再拼接旧版 12 段【xxx】模板）。
    素材引用由调用方通过 anchors / reference_tokens 先行确定编号，
    生成器只把既有引用自然嵌入句子，不新增、不删除、不重排。

    audio_driven：视频 Provider 支持音频参考输入（对白口型由配音驱动）时为
    True，允许提示词要求口型自然配合；否则要求嘴部动作克制，避免模型凭空
    表演说话导致与后期配音错位。
    """
    style_spec = compile_visual_bible(style_prompt, visual_bible)
    effective_negative_prompt = merge_visual_bible_negative(negative_prompt, style_spec)
    effective_aspect_ratio = resolve_visual_aspect_ratio("video", aspect_ratio)

    if director_plan:
        rendered = _render_with_director_plan(
            style_prompt=style_prompt,
            aspect_ratio=effective_aspect_ratio,
            director_plan=director_plan,
            shot_context=shot_context,
            anchors=anchors,
            negative_prompt=effective_negative_prompt,
            visual_bible=visual_bible,
            audio_driven=audio_driven,
        )
        if rendered:
            return apply_content_safety(rendered)

    tokens: list[ReferenceToken] = parse_reference_anchors(
        reference_tokens if reference_tokens is not None else anchors
    )
    dialogue_entries = [
        item
        for item in shot_context.get("dialogue_entries", [])
        if isinstance(item, dict) and item.get("speaker") and item.get("text")
    ]
    focus = _value(shot_context.get("focus"), "当前主要动作")
    sanitized_video_prompt = _sanitize_video_prompt_speech(video_prompt, dialogue_entries)
    if not sanitized_video_prompt:
        sanitized_video_prompt = focus
    compiled = render_natural_video_prompt(
        style_prompt=style_prompt,
        aspect_ratio=effective_aspect_ratio,
        duration_sec=duration_sec,
        action_flow=sanitized_video_prompt,
        shot_context=shot_context,
        dialogue_entries=dialogue_entries,
        tokens=tokens,
        negative_prompt=effective_negative_prompt,
        visual_bible=visual_bible,
        audio_driven=audio_driven,
    )
    return apply_content_safety(compiled)
