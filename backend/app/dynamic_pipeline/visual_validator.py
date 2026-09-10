"""确定性视觉一致性校验器与视频时间锁协议。

用途：在 Prompt 编译出口与旧链路出口扫描下游生成提示词中的越权美术描述，
把「项目画风唯一来源」从文字约束升级为可执行的确定性检查。

口径：
- 项目画风原文（style_prompt / VisualBible）是唯一事实来源，不参与校验；
  同一词汇若已出现在画风原文中，不重复判定。
- 校验对象是下游生成提示词（角色/场景/道具/分镜/视频）的正向内容，
  负向提示词中的禁词是期望表达，不参与扫描。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Literal

# 禁词表：写实化 / 电影化 / 三维化的美术越权词。
# 中文词保留精确匹配；英文词按整词边界匹配。
FORBIDDEN_LATIN_TERMS: tuple[str, ...] = (
    "PBR",
    "photorealistic",
    "photo-realistic",
    "realistic skin",
    "cinematic",
    "cinematic lighting",
    "dramatic lighting",
    "ray tracing",
    "raytracing",
    "volumetric lighting",
    "octane render",
    "unreal engine",
    "realistic reflection",
    "physically accurate",
    "physically based",
    "soft depth of field",
    "realistic rain",
    "realistic fire",
    "realistic water",
    "realistic hair",
)
FORBIDDEN_CJK_TERMS: tuple[str, ...] = (
    "真实反射",
    "镜面高光",
    "透明折射",
    "电影感布光",
    "电影光影",
    "电影级灯光",
    "电影级质感",
    "体积光",
    "物理精确",
    "物理模拟",
    "真实材质",
    "逼真材质",
    "真实发丝",
    "每根发丝清晰可见",
    "背景柔和虚化",
    "真实皮肤",
    "真实布料",
    "写实渲染",
    "三维写实",
    "3D建模渲染",
    "真实物理",
    "流体模拟",
    "摄影级反射",
)
_FORBIDDEN_LATIN_PATTERN = re.compile(
    r"\b(?:"
    + "|".join(
        r"\s+".join(re.escape(part) for part in term.split())
        for term in sorted(FORBIDDEN_LATIN_TERMS, key=len, reverse=True)
    )
    + r")\b",
    re.IGNORECASE,
)

# 同级语义冲突对：双方同时出现在下游内容中即判定冲突（若一方来自画风原文，
# 另一方出现在下游内容同样判定冲突）。
_SEMANTIC_CONFLICT_PAIRS: tuple[tuple[str, str], ...] = (
    ("粗笔", "精细轮廓"),
    ("粗笔", "细腻线条"),
    ("粗笔", "纤细轮廓"),
    ("写意", "每根发丝"),
    ("二维", "真实空间体积感"),
    ("无景深", "背景虚化"),
    ("无景深", "浅景深"),
    ("低饱和", "高饱和"),
    ("墨色明暗", "真实光影"),
    ("绘画概括", "逐根发丝"),
    ("克制配色", "色彩绚丽"),
)

# 视频 Motion / Temporal Style Lock 协议文本（咨询建议 16）。
# 必须放在渲染器尾部注入，不能放在导演计划自由文本中：
# shot_video_optimize.md 会删除「锁定/保持/稳定」类措辞。
MOTION_STYLE_LOCK_TEXT = (
    "运动风格锁：视频仅对现有二维绘画画面进行运动延展，全程保持原始关键帧的二维绘画属性；"
    "不得将人物、建筑、服装、器物、水体、雨水或光影重解释为真实摄影、三维渲染或 PBR 材质；"
    "运动过程中线条粗细、墨色浓度、上色方式与笔触质感保持稳定；"
    "人物面部不得因转头、眨眼或嘴部运动而提高写实程度；"
    "雨、水、烟、火、光等动态元素继续采用二维写意动画表达，不做真实物理模拟。"
)

# 视频负向补充：反动态写实化（追加到全局负向规则）。
# 仅适用于二维绘画类画风；三维CG/真人写实项目中 PBR、真实反射、摄影级景深
# 是正向要求，注入该组条款会反向禁用正向画风（见 dynamic_negative_rules_for）。
DYNAMIC_REALISM_NEGATIVE_RULES = (
    "写实化动态",
    "真实物理模拟",
    "流体模拟",
    "PBR 材质",
    "真实反射",
    "摄影级景深",
)

# 三维CG项目的运动风格锁：与二维版同构，但锁定对象是三维渲染属性，
# 防止模型把三维渲染帧漂移回二维手绘。
MOTION_STYLE_LOCK_TEXT_3D = (
    "运动风格锁：视频仅对现有三维渲染画面进行运动延展，全程保持原始关键帧的三维CG属性；"
    "不得将人物、建筑、服装、器物、水体、雨水或光影重解释为二维手绘、平涂动漫或赛璐璐；"
    "运动过程中模型轮廓、PBR材质、反射与三维光影关系保持稳定；"
    "人物面部不得因转头、眨眼或嘴部运动而改变写实程度；"
    "雨、水、烟、火、光等动态元素继续采用三维渲染表达，不做二维手绘式贴图或赛璐璐明暗。"
)

# 真人写实项目的运动风格锁：锁定写实影像属性，排除二维手绘与三维CG漂移。
MOTION_STYLE_LOCK_TEXT_PHOTOREAL = (
    "运动风格锁：视频仅对现有写实影像画面进行运动延展，全程保持原始关键帧的真人实拍属性；"
    "不得将人物、建筑、服装、器物、水体、雨水或光影重解释为二维手绘、三维CG或卡通渲染；"
    "运动过程中皮肤质感、面料纹理、光线与反射属性保持稳定；"
    "人物面部不得因转头、眨眼或嘴部运动而失真；"
    "雨、水、烟、火、光等动态元素继续采用真实摄影质感表达，不做二维手绘或CG特效。"
)

# 视频动画节奏锁协议（咨询建议：去视频 AI 味）。
# 与 Motion style lock 同属渲染器出口注入的约束文本，不在导演计划自由文本中：
# shot_video_optimize.md 会删除「锁定/保持/稳定」类措辞。
# 全部采用正向表述：点名缺陷（如「不做平滑插值」）会按负向召唤口径适得其反。
ANIMATION_CADENCE_LOCK_TEXT = (
    "动画节奏锁：运动采用手绘二维动画节奏（一拍二），关键姿势之间快速过渡，"
    "姿势与姿势之间保留持帧停顿，画面呈现清晰的张数顿挫感；"
    "快速动作的速度感交给速度线、涂抹帧与背景滚动表达，人物面部与身体保持关键帧画法；"
    "动作遵循快出、慢收、短暂停住的节奏，头发与衣摆等附属物在主体停住后短暂过冲再回稳；"
    "除叙事必需元素外，背景与无戏元素保持静止持帧。"
)

# 三维CG项目的动画节奏锁：保留关键姿势的节奏感（去视频AI味），但不强制
# 一拍二持帧与涂抹帧——三维CG + 写实题材的动作大量依赖眼神、肩颈、步伐等
# 细微连续运动，逐帧顿挫会造成走路顿、转头跳、眼神卡；
# 改为「快出慢收 + 短暂停顿 + 自然连续」，并在需要力度时靠关键姿势与
# 运动模糊强化，明确排除二维涂抹线/手绘残影/赛璐璐速度表现。
ANIMATION_CADENCE_LOCK_TEXT_3D = (
    "动画节奏锁：动画节奏以清晰关键姿势为主，动作遵循快出、慢收、短暂停顿；"
    "人物步行、转头、视线变化与细微肢体动作保持自然连续，不做机械匀速运动，"
    "也不强制逐帧持帧。需要强调动作力度时可适度强化关键姿势与运动模糊，"
    "但不得出现二维涂抹线、手绘残影或赛璐璐式速度表现；"
    "头发与衣摆等附属物在主体停住后短暂过冲再回稳；"
    "除叙事必需元素外，背景与无戏元素保持相对静止。"
)

# 半写实电影感二维项目的动画节奏锁：保留关键姿势顿挫感与运动模糊表达，
# 但不使用「手绘二维」「速度线」等措辞，避免与写实电影布光语义冲突。
ANIMATION_CADENCE_LOCK_TEXT_2D_CINEMATIC = (
    "动画节奏锁：运动采用关键姿势动画节奏（一拍二），关键姿势之间快速过渡，"
    "姿势与姿势之间保留持帧停顿，画面呈现清晰的张数顿挫感；"
    "快速动作的速度感交给运动模糊、涂抹帧与背景滚动表达，人物面部与身体保持关键帧画法；"
    "动作遵循快出、慢收、短暂停住的节奏，头发与衣摆等附属物在主体停住后短暂过冲再回稳；"
    "除叙事必需元素外，背景与无戏元素保持静止持帧。"
)

# 真人写实项目不使用一拍二抽帧节奏：动作按真实物理规律过渡。
ANIMATION_CADENCE_LOCK_TEXT_PHOTOREAL = (
    "动画节奏锁：运动节奏遵循真实物理规律，关键动作之间过渡自然；"
    "动作遵循快出、慢收的节奏，头发与衣摆等附属物在主体停住后短暂过冲再回稳；"
    "除叙事必需元素外，背景与无戏元素保持静止。"
)

StyleMedium = Literal["2d", "3d", "photoreal", "unknown"]


def style_medium_of(spec: Any) -> StyleMedium:
    """按结构化画风判定渲染媒介：``3d`` / ``photoreal`` / ``2d`` / ``unknown``。

    判定依据是推导分支写入的结构化指纹与材质方法：
    - 三维CG分支：``material.pbr`` 为 True；
    - 真人写实分支：``render_fingerprint`` 以「画面为真人实拍」开头；
    - 指纹或材质方法已推导（二维/国漫/水墨/半写实等）按二维绘画处理；
    - 指纹与材质方法均为空（未命中任何推导关键词、用户也未设置结构化字段）
      返回 ``unknown``：不套用任何二维/写实预置锁，避免误伤三渲二、像素、
      皮影等未识别画风。
    """
    material = getattr(spec, "material", None)
    if getattr(material, "pbr", None) is True:
        return "3d"
    fingerprint = str(getattr(spec, "render_fingerprint", "") or "")
    if fingerprint.startswith("画面为真人实拍"):
        return "photoreal"
    if fingerprint.startswith("画面为三维建模渲染"):
        return "3d"
    method = str(getattr(material, "method", "") or "").strip() if material else ""
    if fingerprint.strip() or method:
        return "2d"
    return "unknown"


def _cinematic_two_d(spec: Any) -> bool:
    """二维媒介中的电影感变体（半写实电影感）：写实电影布光 + 景深启用。"""
    lighting = getattr(spec, "lighting", None)
    return getattr(lighting, "realism", "") == "写实电影布光"


def motion_style_lock_for(spec: Any) -> str:
    """按画风媒介选择运动风格锁文本；unknown 不注入。"""
    medium = style_medium_of(spec)
    if medium == "3d":
        return MOTION_STYLE_LOCK_TEXT_3D
    if medium == "photoreal":
        return MOTION_STYLE_LOCK_TEXT_PHOTOREAL
    if medium == "unknown":
        return ""
    return MOTION_STYLE_LOCK_TEXT


def animation_cadence_lock_for(spec: Any) -> str:
    """按画风媒介选择动画节奏锁文本；unknown 不注入。

    三维CG 使用自然连续版节奏锁（关键姿势 + 快出慢收 + 自然连续，不强制
    一拍二持帧/涂抹帧），避免写实题材细微动作被逐帧顿挫；
    半写实电影感二维保留关键姿势顿挫版本，但不带「手绘二维、速度线」措辞。
    """
    medium = style_medium_of(spec)
    if medium == "3d":
        return ANIMATION_CADENCE_LOCK_TEXT_3D
    if medium == "photoreal":
        return ANIMATION_CADENCE_LOCK_TEXT_PHOTOREAL
    if medium == "unknown":
        return ""
    if _cinematic_two_d(spec):
        return ANIMATION_CADENCE_LOCK_TEXT_2D_CINEMATIC
    return ANIMATION_CADENCE_LOCK_TEXT


def dynamic_negative_rules_for(spec: Any) -> tuple[str, ...]:
    """反动态写实化负向条款只注入二维绘画项目。

    三维CG/真人写实项目中 PBR、真实反射、摄影级景深是正向要求，
    注入该组条款会与正向画风指纹直接冲突。
    半写实电影感变体正向启用景深时，剔除「摄影级景深」避免同镜正负互斥。
    """
    if style_medium_of(spec) != "2d":
        return ()
    rules = list(DYNAMIC_REALISM_NEGATIVE_RULES)
    if getattr(getattr(spec, "depth_of_field", None), "enabled", None) is True:
        rules = [item for item in rules if item != "摄影级景深"]
    return tuple(rules)

_VALIDATOR_DIAGNOSTIC_CODES = {
    "visual_conflict_forbidden_term",
    "visual_conflict_semantic",
    "visual_conflict_glow",
}

_GLOW_TERMS = ("光晕", "辉光", "泛光", "bloom")


def _style_disables_glow(style_source: str) -> bool:
    """画风是否声明禁用辉光（「禁用电影级辉光」或「辉光…禁用/关闭」）。"""
    if not style_source:
        return False
    if "禁用电影级辉光" in style_source:
        return True
    return bool(
        re.search(r"辉光.{0,6}(?:禁用|关闭|禁止)|(?:禁用|关闭|禁止).{0,6}辉光", style_source)
    )


def _style_exemption_text(style: Any) -> str:
    """收集风格来源的全部文本（含结构化字段与禁止风格列表）作为豁免来源。

    VisualBible 自己声明的内容（包括 forbidden_styles 中的 PBR/3D 等词）
    不属于下游越权，必须豁免；否则渲染出的风格块会被自检误报。
    注意：只收集正向/启用的声明，排除 global_negative_rules 负向排除规则，
    避免负向要排除的禁词被反向当成正向豁免。
    """
    if style is None:
        return ""
    if isinstance(style, Mapping):
        payload = dict(style)
    elif hasattr(style, "model_dump"):
        payload = style.model_dump(mode="python")
    else:
        return str(getattr(style, "style_prompt", "") or "").strip()

    parts: list[str] = []

    def collect(value: Any, key: str = "") -> None:
        if key in ("global_negative_rules", "negative_prompt", "negative_rules"):
            return
        if isinstance(value, str):
            if value.strip():
                parts.append(value.strip())
        elif isinstance(value, Mapping):
            for sub_k, sub_v in value.items():
                collect(sub_v, sub_k)
        elif isinstance(value, list | tuple | set | frozenset):
            for item in value:
                collect(item, key)

    for k, v in payload.items():
        collect(v, k)

    # 显式启用的结构化三态特征，作为正向画风已声明特征豁免
    material = payload.get("material") if isinstance(payload, Mapping) else None
    if isinstance(material, Mapping):
        if material.get("photographic_reflection") is True:
            parts.append("摄影级反射")
        if material.get("pbr") is True:
            parts.append("PBR")
    elif hasattr(style, "material"):
        mat = style.material
        if getattr(mat, "photographic_reflection", None) is True:
            parts.append("摄影级反射")
        if getattr(mat, "pbr", None) is True:
            parts.append("PBR")

    lighting = payload.get("lighting") if isinstance(payload, Mapping) else None
    if isinstance(lighting, Mapping):
        if lighting.get("volumetric") is True:
            parts.append("体积光")
        if lighting.get("cinematic_glow") is True:
            parts.append("电影级辉光")
    elif hasattr(style, "lighting"):
        lt = style.lighting
        if getattr(lt, "volumetric", None) is True:
            parts.append("体积光")
        if getattr(lt, "cinematic_glow", None) is True:
            parts.append("电影级辉光")

    shading = payload.get("shading") if isinstance(payload, Mapping) else None
    if isinstance(shading, Mapping):
        if shading.get("realistic_shading") is True:
            parts.append("写实明暗")
    elif hasattr(style, "shading"):
        sh = style.shading
        if getattr(sh, "realistic_shading", None) is True:
            parts.append("写实明暗")

    return "\n".join(parts)


def _cut_protocol_sections(prompt: str) -> str:
    """协议段（负向、运动风格锁、动画节奏锁）是约束文本而非内容，不参与正向扫描。"""
    body = prompt
    for label in ("Negative", "Motion style lock", "Animation cadence lock"):
        match = re.search(rf"^\s*{label}\s*:", body, re.MULTILINE)
        if match:
            body = body[: match.start()]
    return body


def _positive_body(prompt: str) -> str:
    return _cut_protocol_sections(prompt or "")


_NEGATION_PREFIXES = (
    "无",
    "不",
    "禁",
    "非",
    "避免",
    "排除",
    "禁用",
    "关闭",
    "no",
    "not",
    "without",
)
_NEGATION_SUFFIXES = ("禁用", "关闭", "禁止")


def _is_negated(text: str, start: int, end: int) -> bool:
    """判断命中词是否处于否定语境（如「无体积光」「PBR 禁用」「禁用电影级辉光」）。"""
    raw_before = text[max(0, start - 10) : start]
    last_punct_before = max(
        raw_before.rfind("。"),
        raw_before.rfind("；"),
        raw_before.rfind(";"),
        raw_before.rfind("！"),
        raw_before.rfind("？"),
        raw_before.rfind("\n"),
    )
    before = raw_before[last_punct_before + 1 :] if last_punct_before >= 0 else raw_before
    if any(prefix in before for prefix in _NEGATION_PREFIXES):
        return True

    raw_after = text[end : min(len(text), end + 10)]
    punct_candidates = [
        idx
        for idx in (
            raw_after.find("。"),
            raw_after.find("；"),
            raw_after.find(";"),
            raw_after.find("！"),
            raw_after.find("？"),
            raw_after.find("\n"),
        )
        if idx >= 0
    ]
    first_punct_after = min(punct_candidates) if punct_candidates else len(raw_after)
    after = raw_after[:first_punct_after]
    if any(suffix in after for suffix in _NEGATION_SUFFIXES):
        return True
    return False


def _forbidden_hits(text: str) -> list[str]:
    hits: list[str] = []
    for match in _FORBIDDEN_LATIN_PATTERN.finditer(text):
        if _is_negated(text, match.start(), match.end()):
            continue
        term = match.group(0)
        if term not in hits:
            hits.append(term)
    for term in FORBIDDEN_CJK_TERMS:
        start = 0
        while True:
            index = text.find(term, start)
            if index < 0:
                break
            if not _is_negated(text, index, index + len(term)) and term not in hits:
                hits.append(term)
            start = index + len(term)
    return hits


def _conflict_hits(content: str, style_source: str) -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []
    for first, second in _SEMANTIC_CONFLICT_PAIRS:
        first_in_style = bool(style_source) and first in style_source
        second_in_style = bool(style_source) and second in style_source
        if first_in_style and second_in_style:
            continue
        if first in content and second in content:
            hits.append((first, second))
            continue
        if first_in_style and second in content:
            hits.append((first, second))
            continue
        if second_in_style and first in content:
            hits.append((first, second))
    return hits


_GENERIC_STYLE_UNITS = {
    "材质",
    "光影",
    "线条",
    "色彩",
    "画面",
    "风格",
    "质感",
    "光线",
    "灯光",
    "渲染",
}
_GENERIC_TERM_PREFIXES = {"背景", "画面", "材质", "真实", "光线", "光影", "渲染"}


def _style_exempt_units(style_source: str) -> list[str]:
    """画风原文中的概念单元，用于豁免下游复用的更长表达（如画风写「电影感」，下游写「电影感布光」）。

    - 按标点/空白切分出的单元；
    - 每个中文禁词的词根（前 2~3 字）若出现在画风原文中，视为画风已声明该概念。
    过于通用的词（材质/光影/线条/背景/真实等）不做子串豁免，避免误放行真实冲突。
    """
    units = [
        unit
        for unit in re.split(r"[\s，,。；;、]+", style_source)
        if len(unit) >= 2 and unit not in _GENERIC_STYLE_UNITS
    ]
    roots = set()
    for term in FORBIDDEN_CJK_TERMS:
        root = term[:3] if len(term) >= 4 else term[:2]
        if root in _GENERIC_TERM_PREFIXES:
            continue
        if root in style_source:
            roots.add(root)
    return [*units, *roots]


def validate_visual_prompt(
    prompt: str,
    *,
    style: Any = None,
    path: str = "prompt",
) -> list[dict[str, Any]]:
    """校验一份下游生成提示词，返回诊断列表。

    返回项为 dict：``{"level", "code", "message", "path", "details"}``，
    便于调用方直接构造 Diagnostic；本模块不依赖 pydantic，避免服务层导入环。
    """
    content = _positive_body(prompt)
    style_source = _style_exemption_text(style)
    diagnostics: list[dict[str, Any]] = []

    hits = _forbidden_hits(content)
    folded_source = style_source.casefold()
    folded_units = [unit.casefold() for unit in _style_exempt_units(style_source)]
    exempt = {
        term
        for term in hits
        if folded_source
        and (
            term.casefold() in folded_source
            or any(unit in term.casefold() for unit in folded_units)
        )
    }
    violations = [term for term in hits if term.casefold() not in {e.casefold() for e in exempt}]
    if violations:
        diagnostics.append(
            {
                "level": "blocking",
                "code": "visual_conflict_forbidden_term",
                "message": f"提示词含与项目画风冲突的写实化/电影化词汇：{', '.join(violations)}",
                "path": path,
                "details": {"terms": violations, "exempt_in_style": sorted(exempt)},
            }
        )

    for first, second in _conflict_hits(content, style_source):
        diagnostics.append(
            {
                "level": "warning",
                "code": "visual_conflict_semantic",
                "message": f"提示词同时出现同级语义冲突：「{first}」与「{second}」",
                "path": path,
                "details": {"terms": [first, second]},
            }
        )

    # 画风禁用电影级辉光时，正文出现发光词会被生成模型理解为 bloom/glow，
    # 与负向约束直接冲突：提示改为「高亮逆光区域 / 明亮背景」等中性描述。
    if _style_disables_glow(style_source):
        folded_content = content.casefold()
        glow_hits: list[str] = []
        for term in _GLOW_TERMS:
            start = 0
            while True:
                index = folded_content.find(term.casefold(), start)
                if index < 0:
                    break
                if not _is_negated(content, index, index + len(term)):
                    if term not in glow_hits:
                        glow_hits.append(term)
                start = index + len(term)
        if glow_hits:
            diagnostics.append(
                {
                    "level": "warning",
                    "code": "visual_conflict_glow",
                    "message": (
                        f"画风禁用电影级辉光，但正文出现发光词：{'、'.join(glow_hits)}，"
                        "请改为「高亮逆光区域 / 明亮背景」等中性描述"
                    ),
                    "path": path,
                    "details": {"terms": glow_hits},
                }
            )
    return diagnostics


def validate_visual_prompt_lenient(
    prompt: str,
    *,
    style: Any = None,
    path: str = "prompt",
) -> list[dict[str, Any]]:
    """旧链路只读暴露：全部降级为 warning，不阻断既有流程。"""
    return [
        {**item, "level": "warning"}
        for item in validate_visual_prompt(prompt, style=style, path=path)
    ]


def is_visual_validator_code(code: str) -> bool:
    return code in _VALIDATOR_DIAGNOSTIC_CODES


__all__ = [
    "ANIMATION_CADENCE_LOCK_TEXT",
    "ANIMATION_CADENCE_LOCK_TEXT_3D",
    "ANIMATION_CADENCE_LOCK_TEXT_PHOTOREAL",
    "DYNAMIC_REALISM_NEGATIVE_RULES",
    "FORBIDDEN_CJK_TERMS",
    "FORBIDDEN_LATIN_TERMS",
    "MOTION_STYLE_LOCK_TEXT",
    "MOTION_STYLE_LOCK_TEXT_3D",
    "MOTION_STYLE_LOCK_TEXT_PHOTOREAL",
    "animation_cadence_lock_for",
    "dynamic_negative_rules_for",
    "is_visual_validator_code",
    "motion_style_lock_for",
    "style_medium_of",
    "validate_visual_prompt",
    "validate_visual_prompt_lenient",
]
