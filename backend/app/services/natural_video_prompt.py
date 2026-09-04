"""自然语言视频提示词生成器。

把结构化分镜事实（shot_template_context 输出 + 已净化的动作时间轴）渲染成
导演口吻的自然语言视频提示词，替代旧版 12 段【xxx】规则说明书式模板。

设计约束：

1. 只负责「表达层」：不改写剧情事实、不决定素材编号。
2. ``@ImageN`` 引用由调用方在进入本模块之前确定（编号即参考图数组下标），
   本模块只把既有引用自然嵌入句子，不新增、不删除、不重排。
3. 动作时间轴中的精确秒数默认去除，改按「开头 / 随后 / 接近结尾时」等
   粗粒度阶段表达，保留动作顺序。
4. 环境动态与约束从实际字段推导（有无角色、有无道具、室内/室外），
   不再无条件拼接通用规则。
5. 输出为少量自然段，不出现固定【XXX】标题。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

_IMAGE_TOKEN_RE = re.compile(r"@Image(\d+)")
_ANCHOR_LINE_RE = re.compile(r"^\s*@Image(\d+)\s*[:：]\s*(.*?)\s*$")
_TIMELINE_MARK_RE = re.compile(
    r"(?:^|[。；;，,]\s*)(\d+(?:\.\d+)?)\s*[-–—~－至]\s*(\d+(?:\.\d+)?)\s*秒\s*[:：]"
)
_LIP_SYNC_MARK_RE = re.compile(r"按第(\d+)句对白执行对应口型")
_CAMERA_INTRO_MARKS = (
    "机位",
    "镜头",
    "景别",
    "平视",
    "俯视",
    "仰视",
    "视角",
    "中景",
    "近景",
    "特写",
    "全景",
    "远景",
    "中近景",
    "中全景",
    "双人中景",
    "跟移",
    "横移",
    "摇镜",
    "推进",
)
_OPENING_PLACEHOLDER_PREFIXES = (
    "承接上一镜结束状态。",
    "承接上一镜结束状态",
    "接上一镜：",
    "接上一镜，",
    "同上一镜结束状态。",
    "同上一镜结束状态",
    "同上一镜。",
    "同上一镜",
)
_POSTURE_LEAD = (
    "站",
    "坐",
    "蹲",
    "躺",
    "跪",
    "靠",
    "背对",
    "面向",
    "侧对",
    "正对",
    "面朝",
    "背朝",
    "位于",
    "处在",
    "处于",
    "半蹲",
)
_OUTDOOR_SCENE_MARKS = (
    "户外",
    "室外",
    "街道",
    "路边",
    "马路",
    "公园",
    "庭院",
    "花园",
    "树林",
    "森林",
    "山顶",
    "山间",
    "湖",
    "海边",
    "天空",
    "天台",
    "楼顶",
    "楼外",
    "楼下",
    "门外",
    "树下",
    "广场",
    "校门口",
    "门口",
    "巷道",
    "巷口",
)
_INDOOR_SCENE_MARKS = (
    "室内",
    "办公",
    "房间",
    "厅",
    "室",
    "店",
    "公寓",
    "电梯",
    "走廊",
    "楼道",
    "宿舍",
    "病房",
    "教室",
)

_DEFAULT_STYLE = "项目既定画风"

# 旧模板的机器化措辞，自然化后不得再出现（用于测试兜底断言）。
_FORBIDDEN_TEMPLATE_WORDS = (
    "严格保持",
    "全程保持",
    "不得变化",
    "不得突变",
    "不得重构",
    "忽略其他描述",
    "连续覆盖",
    "唯一时间轴",
    "本镜只执行",
    "必须完整继承",
    "本镜唯一开始状态",
    "全程闭口",
)

_MECHANICAL_TIMELINE_RE = re.compile(r"\d+(?:\.\d+)?\s*[-–—~－至]\s*\d+(?:\.\d+)?\s*秒")


def validate_natural_style(prompt: str) -> list[str]:
    """确定性检查提示词是否仍是模板/机械写法，返回问题列表。

    用于优化结果验收：自然语言化后的提示词不应再出现【】段落标题、
    0-2秒 式机械时间轴和规则说明书词汇；出现问题即拒绝采用，
    避免「优化提示词」把提示词改回旧模板。
    """
    issues: list[str] = []
    if "【" in prompt:
        issues.append("仍包含【】模板段落标题")
    if _MECHANICAL_TIMELINE_RE.search(prompt or ""):
        issues.append("仍包含机械时间轴（N-M秒 分段）")
    for word in _FORBIDDEN_TEMPLATE_WORDS:
        if word in prompt:
            issues.append(f"仍包含机器化措辞「{word}」")
            break
    return issues


@dataclass(frozen=True)
class ReferenceToken:
    """进入生成器之前的素材引用：编号由程序决定，生成器只读使用。"""

    token: str
    role: str = "reference"
    label: str = ""
    asset_id: str = ""
    keyframe_id: str = ""
    shot_id: str = ""


@dataclass
class ReferenceValidation:
    """确定性素材绑定校验结果；issues 为空表示通过。"""

    issues: list[str] = field(default_factory=list)
    tokens_in_prompt: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues


def _role_from_label(label: str) -> str:
    if "上一镜结束帧" in label:
        return "prev_end_frame"
    if "当前分镜关键帧" in label:
        return "keyframe"
    if "角色身份" in label:
        return "character_identity"
    if "场景" in label:
        return "scene_layout"
    if "物品" in label or "道具" in label:
        return "prop_identity"
    if "上一镜" in label:
        return "previous_keyframe"
    return "reference"


def parse_reference_anchors(anchors: Sequence[str | dict[str, Any]]) -> list[ReferenceToken]:
    """把调用方生成的 @ImageN 锚点文本解析成只读引用描述。

    支持两种输入：程序生成的锚点字符串（如
    ``@Image1：当前分镜关键帧，锁定构图、人物左右关系和起始站位。``），
    或已结构化的 dict（token/role/label/asset_id 等字段）。
    """
    tokens: list[ReferenceToken] = []
    for anchor in anchors or []:
        if isinstance(anchor, dict):
            token = str(anchor.get("token") or "").strip()
            if not token:
                continue
            tokens.append(
                ReferenceToken(
                    token=token,
                    role=str(anchor.get("role") or "reference"),
                    label=str(anchor.get("label") or ""),
                    asset_id=str(anchor.get("asset_id") or ""),
                    keyframe_id=str(anchor.get("keyframe_id") or ""),
                    shot_id=str(anchor.get("shot_id") or ""),
                )
            )
            continue
        match = _ANCHOR_LINE_RE.match(str(anchor))
        if not match:
            continue
        label = match.group(2).strip("。")
        tokens.append(
            ReferenceToken(
                token=f"@Image{match.group(1)}",
                role=_role_from_label(label),
                label=label,
            )
        )
    return tokens


def _asset_name_from_label(token: ReferenceToken) -> str:
    """素材锚点的 label 形如「徐尘心，锁定角色身份与服装」，取逗号前的素材名。"""
    if token.role in {"keyframe", "prev_end_frame", "reference", "previous_keyframe"}:
        return ""
    name = token.label.split("，")[0].split(",")[0].strip()
    return name


def _match_tokens_by_names(
    tokens: Sequence[ReferenceToken],
    role: str,
    names: Sequence[str],
) -> dict[str, ReferenceToken]:
    matched: dict[str, ReferenceToken] = {}
    pool = [token for token in tokens if token.role == role]
    for name in names:
        if not name:
            continue
        for token in pool:
            if token.token in {item.token for item in matched.values()}:
                continue
            label_name = _asset_name_from_label(token)
            if label_name and (label_name == name or name in label_name or label_name in name):
                matched[name] = token
                break
    return matched


def _split_person_parts(value: Any) -> list[tuple[str, str]]:
    """把「X：细节；Y：细节」拆成 (人物, 细节) 列表。"""
    parts: list[tuple[str, str]] = []
    raw = str(value or "").strip()
    if not raw:
        return parts
    for clause in re.split(r"[；;]", raw):
        clause = clause.strip().rstrip("。")
        if not clause:
            continue
        if "：" in clause:
            person, details = clause.split("：", 1)
        elif ":" in clause:
            person, details = clause.split(":", 1)
        else:
            continue
        person = person.strip()
        details = details.strip(" ，,；;")
        if details:
            parts.append((person, details))
    return parts


def _position_sentence(parts: list[tuple[str, str]]) -> str:
    rendered: list[str] = []
    for person, details in parts:
        if details.startswith(_POSTURE_LEAD):
            rendered.append(f"{person}{details}")
        else:
            rendered.append(f"{person}在{details}")
    return "；".join(rendered)


def _expression_sentence(parts: list[tuple[str, str]]) -> str:
    rendered: list[str] = []
    for person, details in parts:
        if details.startswith(("神情", "表情", "面带", "脸上")):
            rendered.append(f"{person}{details}")
        else:
            rendered.append(f"{person}的神情为{details}")
    return "；".join(rendered)


def _clean_opening(opening: str) -> str:
    """去掉起始状态里的承接占位前缀，避免「开始时，接上一镜：」这类冗余。"""
    text = opening.strip()
    for prefix in _OPENING_PLACEHOLDER_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix) :].strip(" ，,。；;")
            break
    if text in {"承接上一镜结束状态", "同上一镜"}:
        return ""
    return text


def _strip_trailing_punct(value: str) -> str:
    return value.strip().rstrip("。，；、 ")


def _ensure_sentence_end(value: str) -> str:
    stripped = _strip_trailing_punct(value)
    if stripped.endswith(("！", "？")):
        return stripped
    return f"{stripped}。"


def _strip_camera_intro(segment: str) -> str:
    """去掉动作段开头重复的机位描述句（机位由摄影机段落统一承担）。"""
    parts = re.split(r"(?<=。)", segment)
    while parts and any(mark in parts[0] for mark in _CAMERA_INTRO_MARKS):
        parts.pop(0)
    return "".join(parts) if parts else segment


def naturalize_timeline_flow(action_flow: str, duration_sec: float) -> str:
    """把可能带 0-2秒 前缀的动作流改写成自然连续表达，保留动作顺序。

    时间标记可能出现在句首，也可能出现在描述句之后（提取模型常先写一句
    机位/场景铺垫再写时间轴），因此按整段文本扫描时间标记切分。
    口径与原模板一致：末段结束时间不足总时长时补一段自然收尾。
    """
    text = str(action_flow or "").strip()
    if not text:
        return ""
    text = _LIP_SYNC_MARK_RE.sub(r"口型与第\1句对白同步", text)

    matches = list(_TIMELINE_MARK_RE.finditer(text))
    if not matches:
        return f"{_strip_camera_intro(text).rstrip('。')}。"

    segments: list[str] = []
    first = matches[0]
    intro = text[: first.start()].strip().strip("。；;，, ")
    if intro:
        segments.append(_strip_camera_intro(intro))
    last_end = 0.0
    for index, match in enumerate(matches):
        last_end = float(match.group(2))
        body_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end() : body_end].strip().strip("。；;，, ")
        if body:
            segments.append(body)

    if not segments:
        return text

    if last_end and last_end < float(duration_sec) - 0.1:
        segments.append("保持当前状态自然收尾")

    count = len(segments)
    if count == 1:
        return f"{segments[0]}。"
    if count == 2:
        return f"先{segments[0]}，随后{segments[1]}。"
    if count == 3:
        return f"开头{segments[0]}，随后{segments[1]}，接近结尾时{segments[2]}。"
    middle = "；".join(segments[1:-1])
    return f"开头{segments[0]}，随后{middle}，接近结尾时{segments[-1]}。"


def _normalize_timeline_flow(action_flow: str, duration_sec: float) -> str:
    return naturalize_timeline_flow(action_flow, duration_sec)


def _camera_sentence(camera_move: Any) -> str:
    raw = str(camera_move or "").strip()
    fixed_markers = ("固定", "不动", "静止", "机位不动", "无运镜")
    move_markers = ("推近", "推进", "横移", "摇镜", "跟随", "缓推", "移动", "升降", "环绕")
    is_fixed = any(marker in raw for marker in fixed_markers) or not raw
    has_move = any(marker in raw for marker in move_markers)
    if is_fixed and has_move:
        move_word = next(marker for marker in move_markers if marker in raw)
        return f"机位基本固定，只在{move_word}方向上做幅度很小的调整。"
    if is_fixed:
        return "机位固定，画面变化由人物动作推动。"
    if "跟随" in raw:
        return f"镜头{raw}人物运动。"
    return f"镜头{raw}，并跟随人物的运动自然调整。"


def _scene_environment_sentence(scene_name: str) -> str:
    if any(mark in scene_name for mark in _OUTDOOR_SCENE_MARKS):
        return "树叶、草叶等自然物受轻风影响产生克制而自然的小幅摆动。"
    if any(mark in scene_name for mark in _INDOOR_SCENE_MARKS):
        return "室内光线与阴影允许细微的自然变化，窗帘、纸张等轻质陈设可有微弱环境动态。"
    return ""


def _continuity_sentence(transition: str, prev_ending: str, scene_name: str) -> str:
    if transition == "continuous":
        base = _ensure_sentence_end(prev_ending) if prev_ending else ""
        if base:
            return (
                f"这一镜直接接在上一镜之后：{base}"
                "人物仍保持上一镜结束时的位置、姿态与朝向，动作从这里自然开始。"
            )
        return "这一镜直接接在上一镜之后，人物的位置、姿态与朝向保持连续。"
    if transition == "elliptical":
        return "上一镜结束后省略了一段过程，本镜重新建立画面；人物的服装与随身物品保持连续。"
    if transition == "same_scene_cut":
        location = f"{scene_name}内" if scene_name else "同一场景"
        return (
            f"本镜与上一镜同处{location}，属于独立机位（对切或景别切换），"
            "不承接上一镜的画面构图；人物的空间位置与持有物承接上一镜，"
            "构图以本镜起始画面为准。"
        )
    return "本镜是全新的画面，不承接上一镜的构图。"


def _negative_line(negative_prompt: str) -> str:
    base = "画面中不出现：人物变形或服装变化、新增人物、画面内文字或字幕、道具瞬移。"
    extra = re.sub(
        r"^禁止出现\s*[:：]\s*",
        "",
        str(negative_prompt or "").strip(),
    ).rstrip("。")
    extra = extra.strip(" ，、；;")
    if not extra:
        return base
    # 去掉与基础约束重复的词，避免越写越长
    covered = ("人物变形", "服装变化", "新增人物", "文字", "字幕", "道具瞬移", "水印")
    clauses = [item.strip() for item in re.split(r"[，,；;、]", extra) if item.strip()]
    kept = [item for item in clauses if not any(word in item for word in covered)]
    if not kept:
        return base
    return base.rstrip("。") + "；" + "、".join(kept) + "。"


def render_natural_video_prompt(
    *,
    style_prompt: str,
    aspect_ratio: str,
    duration_sec: float,
    action_flow: str,
    shot_context: dict[str, Any],
    dialogue_entries: Sequence[dict[str, str]],
    tokens: Sequence[ReferenceToken] = (),
    negative_prompt: str = "",
) -> str:
    """把结构化事实渲染成自然语言视频提示词（少量自然段）。"""
    scene_name = str(shot_context.get("scene_name") or "").strip()
    characters = [str(item).strip() for item in (shot_context.get("characters") or []) if item]
    props = [str(item).strip() for item in (shot_context.get("props") or []) if item]
    opening = str(shot_context.get("opening_frame") or "").strip()
    ending = str(shot_context.get("ending_frame") or "").strip()
    transition = str(shot_context.get("transition_from_prev") or "scene_change")
    prev_ending = str(shot_context.get("prev_end_state") or "").strip()
    shot_type = str(shot_context.get("shot_type") or "").strip()
    camera_angle = str(shot_context.get("camera_angle") or "").strip()
    camera_move = shot_context.get("camera_move")
    composition = str(
        shot_context.get("composition") or shot_context.get("frame_description") or ""
    ).strip()
    sound_effects = str(shot_context.get("sound_effects") or "").strip()
    voice_over = str(shot_context.get("voice_over") or "").strip()

    token_list = list(tokens)
    keyframe = next((item for item in token_list if item.role == "keyframe"), None)
    prev_end_frame = next((item for item in token_list if item.role == "prev_end_frame"), None)
    prev_keyframe = next(
        (item for item in token_list if item.role == "previous_keyframe"), None
    )
    scene_token = next((item for item in token_list if item.role == "scene_layout"), None)
    character_tokens = _match_tokens_by_names(token_list, "character_identity", characters)
    prop_tokens = _match_tokens_by_names(token_list, "prop_identity", props)
    extra_tokens = [
        item
        for item in token_list
        if item.role
        in {"scene_master", "reference"}
        or (
            item.role == "character_identity"
            and item.token not in {matched.token for matched in character_tokens.values()}
        )
        or (
            item.role == "prop_identity"
            and item.token not in {matched.token for matched in prop_tokens.values()}
        )
    ]

    paragraphs: list[str] = []

    # 第一段：场景、时间与整体画面气质
    opening_lines = [
        _ensure_sentence_end(f"这一镜发生在{scene_name}") if scene_name else "本镜画面。",
        (
            _ensure_sentence_end(
                f"整体延续{_strip_trailing_punct(str(style_prompt or _DEFAULT_STYLE).strip())}"
                "的风格"
            )
            + f"画面比例 {aspect_ratio}，时长约 {duration_sec:g} 秒。"
        ),
    ]
    if scene_token:
        opening_lines.append(
            f"场景结构、陈设与光线沿用 {scene_token.token} 的参考素材。"
        )
    paragraphs.append("".join(opening_lines))

    # 第二段：人物初始状态与起始画面
    start_lines: list[str] = []
    if prev_end_frame and keyframe and prev_end_frame.token < keyframe.token:
        start_lines.append(
            f"画面从 {prev_end_frame.token}（上一镜的结束画面）自然进入，"
            f"向 {keyframe.token} 展示的目标画面发展。"
        )
    elif keyframe:
        subject = "、".join(characters) if characters else "人物"
        start_lines.append(
            f"以 {keyframe.token} 作为这一镜的起始画面，"
            f"沿用其中{subject}的位置、朝向、服装以及人物与场景之间的空间关系。"
        )
        if prev_end_frame:
            start_lines.append(
                f"上一镜的结束画面（{prev_end_frame.token}）仅用于衔接连贯，"
                "不改变本镜的起始构图。"
            )
    elif prev_end_frame:
        start_lines.append(f"起始状态承接 {prev_end_frame.token}（上一镜的结束画面）。")
    if prev_keyframe:
        start_lines.append(f"人物与空间的关系沿用 {prev_keyframe.token}（上一镜的画面）。")
    cleaned_opening = _clean_opening(opening)
    if cleaned_opening:
        start_lines.append(_ensure_sentence_end(f"这一镜开始时，{cleaned_opening}"))
    if characters:
        start_lines.append(_ensure_sentence_end(f"画面中的角色只有{'、'.join(characters)}"))
    position_parts = _split_person_parts(shot_context.get("positions"))
    if position_parts:
        start_lines.append(_ensure_sentence_end(f"开始时，{_position_sentence(position_parts)}"))
    expression_parts = _split_person_parts(shot_context.get("expressions"))
    if expression_parts:
        start_lines.append(_expression_sentence(expression_parts) + "。")
    for name in characters:
        token = character_tokens.get(name)
        if token is not None:
            start_lines.append(
                f"{name}的外貌、发型和当天服装沿用 {token.token} 的参考素材，"
                "镜头过程中不发生变化。"
            )
    if start_lines:
        paragraphs.append("".join(start_lines))

    # 第三段：动作发展
    flow = _normalize_timeline_flow(action_flow, duration_sec)
    if flow:
        action_lines = [f"从这一起始状态出发，{flow}"]
        if characters:
            action_lines.append(
                "动作过程中，人物的重心转移、视线与手部动作自然连贯，"
                "头发和衣摆带出轻微的自然惯性。"
            )
        if props:
            prop_note = _ensure_sentence_end(f"本镜道具：{'、'.join(props)}")
            if prop_tokens:
                prop_note += "；".join(
                    f"{name}沿用 {token.token} 的外观与材质" for name, token in prop_tokens.items()
                ) + "。"
            prop_note += (
                "道具被拿取、移动或放下时位置随动作自然变化，其余时间保持原位。"
            )
            action_lines.append(prop_note)
        paragraphs.append("".join(action_lines))

    # 第四段：摄影机与构图
    camera_lines: list[str] = []
    if shot_type or camera_angle:
        framing = "、".join(
            item for item in (f"景别为{shot_type}" if shot_type else "", camera_angle) if item
        )
        camera_lines.append(_ensure_sentence_end(framing))
    camera_lines.append(_camera_sentence(camera_move))
    if composition:
        camera_lines.append(_ensure_sentence_end(f"构图方面，{composition}"))
    if scene_name and composition:
        camera_lines.append(
            f"背景保持{scene_name}的环境细节，前景随人物动作自然形成层次。"
        )
    if camera_lines:
        paragraphs.append("".join(camera_lines))

    # 第五段：对白、声音、环境动态、连续性与结束状态
    tail_lines: list[str] = []
    dialogue_entries = list(dialogue_entries)
    if dialogue_entries:
        tail_lines.append("本镜对白按以下顺序进行：")
        tail_lines.extend(
            _ensure_sentence_end(f"第{index}句，{item['speaker']}说：“{item['text']}”")
            for index, item in enumerate(dialogue_entries, start=1)
        )
        tail_lines.append(
            "说话人开口时口型自然配合，其余角色在该时段保持安静，不抢话、不代说。"
        )
    else:
        no_dialogue = "本镜没有画内对白，人物全程不开口"
        no_dialogue += (
            "；旁白与画外音由后期合成，画面人物保持闭口。" if voice_over else "。"
        )
        tail_lines.append(no_dialogue)
    if sound_effects:
        tail_lines.append(_ensure_sentence_end(f"环境与动作声：{sound_effects}"))
    env_motion = _scene_environment_sentence(scene_name)
    if env_motion:
        tail_lines.append(env_motion)
    tail_lines.append(
        "光线与阴影随人物和镜头的移动产生自然的细微变化。"
    )
    tail_lines.append(_continuity_sentence(transition, prev_ending, scene_name))
    if ending:
        tail_lines.append(
            f"接近结尾时，{_strip_trailing_punct(ending)}。"
            "人物与道具的最终位置都停留在这个状态，便于下一镜衔接。"
        )
    if extra_tokens:
        tail_lines.append(
            "；".join(f"{item.token} 作为画面参考" for item in extra_tokens) + "。"
        )
    tail_lines.append(_negative_line(negative_prompt))
    paragraphs.append("".join(tail_lines))

    compiled = "\n\n".join(part for part in paragraphs if part.strip())
    return compiled


def validate_prompt_references(
    prompt: str,
    tokens: Sequence[ReferenceToken],
    *,
    require_keyframe: bool = True,
) -> ReferenceValidation:
    """确定性校验最终提示词中的 @ImageN 引用。

    检查项：

    - 提示词中出现的每个 @ImageN 是否都有对应绑定（禁止引用不存在的 token）；
    - 必需的关键帧引用是否丢失（compile 路径必须保留）；
    - 同一 token 是否被绑定到多个素材（由 token 列表本身保证，这里查重）。
    """
    issues: list[str] = []
    found = _IMAGE_TOKEN_RE.findall(prompt or "")
    found_tokens = [f"@Image{number}" for number in found]
    known = {token.token for token in tokens}
    # token 列表内同一编号出现两次 = 同一编号指向多个素材
    seen: dict[str, str] = {}
    for token in tokens:
        if token.token in seen and seen[token.token] != (token.role, token.label):
            issues.append(
                f"引用 {token.token} 被绑定到多个素材：{seen[token.token]} 与 "
                f"{(token.role, token.label)}"
            )
        seen.setdefault(token.token, (token.role, token.label))
    for token in found_tokens:
        if token not in known:
            issues.append(f"提示词引用了不存在的参考素材 {token}")
    if require_keyframe:
        keyframe = next((item for item in tokens if item.role == "keyframe"), None)
        if keyframe is not None and keyframe.token not in found_tokens:
            issues.append(f"当前分镜关键帧引用 {keyframe.token} 缺失")
    return ReferenceValidation(issues=issues, tokens_in_prompt=found_tokens)


__all__ = [
    "ReferenceToken",
    "ReferenceValidation",
    "naturalize_timeline_flow",
    "parse_reference_anchors",
    "render_natural_video_prompt",
    "validate_natural_style",
    "validate_prompt_references",
    "_FORBIDDEN_TEMPLATE_WORDS",
]
