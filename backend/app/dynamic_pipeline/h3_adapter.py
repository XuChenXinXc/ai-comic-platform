"""MiniMax H3 出口适配层。

上游编译产物（完整规范版中文提示词）保留给业务门禁与存档使用；本模块在其出口派生
可直接送 H3 的两种最终形态：

1. compact_chinese_prompt(): 确定性精简压缩。
   原则：一个事实只定义一次、一个变化只描述一次、起草者指令不进入模型指令。
   并在压缩前执行送模型预检（preflight），当前覆盖：
   - storyboard_negative_action_conflict：禁止项禁止的动作恰是核心动作要求的终点；
   - 场景无关低价值负面（缴费单、温度标识等）自动剔除并计入统计。

2. build_ref2va_rewrite_request() / validate_ref2va_sections()：
   按官方 prompt-writing skill（github.com/MiniMax-AI/MiniMax-H3）构造受控改写请求：
   六段英文结构、对白保留中文并写成 <d>[Chinese] ...</d>、说话人标注 (S1) 且首次
   出场带声音身份、对白逐字落时间点不裁剪、运镜按「运动类型+幅度+速度」三要素、
   结尾截断用 <cutoff>、@ImageN 与 <Picture N> 同号映射、retention_analysis 使用
   官方标记词汇。请求交平台已配置的 text provider 执行，输出用
   validate_ref2va_sections() 校验（六段顺序、对白逐字、引用标签一致性、切镜时间
   不超时长），失败时调用方回退原提示词。

已知边界：互斥检测基于词面重叠（动作终点短语出现在禁止项中）；“符文全部亮起
vs 依次亮至剑尖”这类程度型互斥不由本层覆盖，由上游模板措辞修复保证。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar

from pydantic import BaseModel, Field

__all__ = [
    "CompactResult",
    "H3PromptBundle",
    "PreflightWarning",
    "Ref2VARewriteRequest",
    "Ref2VATextOutput",
    "build_ref2va_rewrite_request",
    "compact_chinese_prompt",
    "convert_compiled_prompt",
    "detect_preflight_warnings",
    "extract_dialogue_lines",
    "validate_ref2va_sections",
]

# ---------------------------------------------------------------------------
# 段落解析
# ---------------------------------------------------------------------------

# 节头必须独占一行：正文中的"机位以【镜头】段落为准"这类行内引用不得再被误切
_SECTION_RE = re.compile(r"^【([^【】\n]{1,12})】[ \t]*$", re.MULTILINE)
_SECTION_ORDER = (
    "全局风格",
    "角色锁定",
    "物品锁定",
    "场景锁定",
    "上一镜结束状态",
    "本镜唯一开始状态",
    "对白与口型",
    "核心动作",
    "镜头",
    "声音",
    "结束状态",
    "禁止项",
)

# 导演设计层重构后的 10 段最终提示词（v2）。
_SECTION_ORDER_V2 = (
    "画面风格",
    "本镜叙事意图",
    "初始状态",
    "镜头与构图",
    "人物表演",
    "唯一时间轴",
    "环境动态",
    "对白与声音",
    "结束状态",
    "硬约束",
)


# v2 中与 v1 不同的段落标题，用于判定 10 段格式（「结束状态」是两版共享，不能作为判据）。
_SECTION_ORDER_V2_UNIQUE = (
    "画面风格",
    "本镜叙事意图",
    "初始状态",
    "镜头与构图",
    "人物表演",
    "唯一时间轴",
    "环境动态",
    "对白与声音",
    "硬约束",
)


def _active_section_order(sections: Mapping[str, str]) -> tuple[str, ...]:
    """按实际存在的段落标题选择 v1（12 段）或 v2（10 段）顺序。"""

    if any(name in sections for name in _SECTION_ORDER_V2_UNIQUE):
        return _SECTION_ORDER_V2
    return _SECTION_ORDER

# 起草者约束类整行剔除（这些话约束的是提取 Agent，不是视频模型）
_DRAFTER_LINE_PREFIXES = (
    "人物站位必须",
    "起始朝向不得",
    "初始帧画面内",
    "时间轴中的动作、表情",
)
_ENDSTATE_META_MARK = "结束状态必须明确"
# 禁止项中的场景无关低价值实体（按需在项目词典扩展）
_LOWVALUE_NEGATIVE_TOKENS = ("缴费单", "温度标识", "笔记", "招牌")
_TEXT_FAMILY_MARK = re.compile(r"文字|字幕|水印")
_CONSOLIDATED_TEXT_RULE = "全片无任何文字、字幕或水印。"
_DURATION_RE = re.compile(r"连续覆盖\s*0\s*到\s*([0-9.]+)\s*秒")
_TOTAL_RE = re.compile(r"总时长[：:]\s*([0-9.]+)\s*秒")
_DESTINATION_RE = re.compile(r"(?:走到|行至|停在|抵达)([^，。；\n]{2,14})")


def _split_sections(text: str) -> dict[str, str]:
    marks = [(m.group(1), m.start(), m.end()) for m in _SECTION_RE.finditer(text)]
    sections: dict[str, str] = {}
    for index, (name, _start, end) in enumerate(marks):
        body_end = marks[index + 1][1] if index + 1 < len(marks) else len(text)
        sections.setdefault(name, text[end:body_end].strip())
    return sections


# ---------------------------------------------------------------------------
# 预检告警
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreflightWarning:
    """送模型前的词面级冲突提示；只提示，不改写正文。"""

    code: str
    message: str
    detail: str = ""


def detect_preflight_warnings(source: str) -> list[PreflightWarning]:
    """扫描完整规范版的核心动作与禁止项，返回词面互斥类预检告警。"""

    sections = _split_sections(source)
    action = sections.get("核心动作") or sections.get("唯一时间轴", "")
    forbidden = sections.get("禁止项") or sections.get("硬约束", "")
    if not action or not forbidden:
        return []

    destinations: list[str] = []
    for found in _DESTINATION_RE.findall(action):
        if found not in destinations:
            destinations.append(found)

    def _normalize(item: str) -> str:
        return item.replace("已经", "").replace("已", "").replace("立刻", "").strip()

    clauses = [_normalize(item) for item in re.split(r"[；;\n]", forbidden) if item.strip()]

    warnings: list[PreflightWarning] = []
    seen_pairs: set[tuple[str, str]] = set()
    for destination in destinations:
        head = destination[:4]
        if len(head) < 4:
            continue
        for clause in clauses:
            if head in clause and (destination, clause) not in seen_pairs:
                seen_pairs.add((destination, clause))
                warnings.append(
                    PreflightWarning(
                        code="storyboard_negative_action_conflict",
                        message="禁止项禁止的动作与核心动作要求的目标终点互斥",
                        detail=f"{destination}（来自核心动作）⇄ {clause[:28]}（来自禁止项）",
                    )
                )
                break
    return warnings


# ---------------------------------------------------------------------------
# 精简压缩器（出口 A）
# ---------------------------------------------------------------------------


@dataclass
class CompactResult:
    text: str
    original_length: int
    warnings: list[PreflightWarning] = field(default_factory=list)

    @property
    def length(self) -> int:
        return len(self.text)

    @property
    def shrink_ratio(self) -> float:
        if self.original_length <= 0:
            return 0.0
        return round(1 - self.length / self.original_length, 4)


def _compact_start_state(body: str) -> str:
    kept: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if any(stripped.startswith(prefix) for prefix in _DRAFTER_LINE_PREFIXES):
            continue
        kept.append(stripped)
    return "\n".join(kept)


def _compact_dialogue(body: str) -> str:
    stripped = body.strip()
    if not stripped:
        return ""
    first_line = stripped.splitlines()[0]
    if "没有画内对白" in first_line:
        return "本镜没有画内对白；所有人物全程闭口，不生成台词、旁白或画外音。"
    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    spoken = [line for line in lines if re.match(r"^第\d+句[：:]", line)]
    enforcement = "对白只在上述时段执行口型；只有对应说话人开口，其余角色全程闭口。"
    if spoken:
        return "\n".join([*spoken, enforcement])
    return stripped


def _compact_action(body: str, fallback_duration: float | None) -> str:
    duration_value: float | str | None = None
    duration_match = _DURATION_RE.search(body)
    if duration_match is not None:
        duration_value = duration_match.group(1)
    elif fallback_duration is not None:
        duration_value = f"{fallback_duration:g}"
    timeline_match = re.search(r"0[-–]\d+(?:\.\d+)?秒", body)
    timeline = body[timeline_match.start():].strip() if timeline_match else body.strip()
    header = "本镜唯一时间轴"
    if duration_value is not None:
        header += f"（须连续覆盖 0 到 {duration_value} 秒，忽略其他时间描述）"
    return header + "：\n" + timeline


def _compact_camera(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _compact_end_state(body: str) -> str:
    cut = body.find(_ENDSTATE_META_MARK)
    cleaned = body if cut < 0 else body[:cut]
    return "\n".join(line.strip() for line in cleaned.splitlines() if line.strip())


def _compact_forbidden(body: str) -> str:
    clauses_in = [item.strip() for item in re.split(r"[；;\n]", body) if item.strip()]
    kept: list[str] = []
    seen: set[str] = set()
    had_text_family = False
    for item in clauses_in:
        if _TEXT_FAMILY_MARK.search(item):
            had_text_family = True
        if any(token in item for token in _LOWVALUE_NEGATIVE_TOKENS):
            continue
        if _TEXT_FAMILY_MARK.search(item):
            continue
        if item in seen:
            continue
        seen.add(item)
        kept.append(item)
    if had_text_family:
        # 仅当源存在文字/字幕/水印约束时才追加合并规则，避免向源注入
        # 未声明的「全片无文字」硬约束（如场景本就要求出现招牌等文字）。
        kept.append(_CONSOLIDATED_TEXT_RULE)
    return "；".join(kept) + "。"


def compact_chinese_prompt(source: str) -> CompactResult:
    """把完整规范版压缩为精简执行版；纯文本变换，不依赖任何模型调用。

    自然语言版提示词（无【】段落标题）已经是导演口吻的精简执行文本，
    原样透传，不做任何截断或改写。
    """

    sections = _split_sections(source)
    warnings = detect_preflight_warnings(source)

    if not sections:
        return CompactResult(text=source.strip(), original_length=len(source), warnings=warnings)

    # v2（导演设计层 10 段）已经是渲染好的结果，直接透传，不再做起草者约束压缩。
    if _active_section_order(sections) is _SECTION_ORDER_V2:
        parts = [f"【{name}】\n{sections[name]}" for name in _SECTION_ORDER_V2 if name in sections]
        text = "\n\n".join(parts)
        return CompactResult(text=text, original_length=len(source), warnings=warnings)

    total_match = _TOTAL_RE.search(sections.get("全局风格", ""))
    fallback_duration = float(total_match.group(1)) if total_match else None

    parts: list[str] = []
    for name in _SECTION_ORDER:
        if name not in sections:
            continue
        if name == "本镜唯一开始状态":
            body = _compact_start_state(sections[name])
        elif name == "对白与口型":
            body = _compact_dialogue(sections[name])
        elif name == "核心动作":
            body = _compact_action(sections[name], fallback_duration)
        elif name == "镜头":
            body = _compact_camera(sections[name])
        elif name == "结束状态":
            body = _compact_end_state(sections[name])
        elif name == "禁止项":
            body = _compact_forbidden(sections[name])
        else:
            body = sections[name]
        if not body:
            continue
        parts.append(f"【{name}】\n{body}")

    text = "\n\n".join(parts)
    return CompactResult(text=text, original_length=len(source), warnings=warnings)


# ---------------------------------------------------------------------------
# Ref2VA 受控重写请求（出口 B）
# ---------------------------------------------------------------------------

_REF2VA_SYSTEM_PROMPT = (
    "You rewrite ONE shot of a Chinese comic-drama storyboard into the MiniMax H3 "
    "full-reference (Ref2VA) native prompt. Output EXACTLY six sections in this order: "
    "subject_definitions:, summary:, retention_analysis:, detailed_description:, "
    "overall_soundscape:, non_diegetic_music:. Prose in English; dialogue stays Chinese "
    "wrapped as <d>[Chinese] ...</d>. A physically speaking subject is labelled (S1), "
    "(S2), ... by order of first vocal event in the target video; when a speaker first "
    "appears, establish a stable voice identity from the source (age, gender, timbre, "
    "pitch, speaking rate, accent) so H3 voices the line natively. Write every supplied "
    "dialogue line verbatim at its source timeline point with matching mouth movement "
    "and performance, in order; never trim, merge, shorten, or judge dialogue density. "
    "If speech is truncated by the video end, mark it <cutoff>. Map each @ImageN token "
    "from the source's reference map to <Picture N> with the same N (Picture 1 anchors "
    "the first attached image, normally the keyframe at 0.00s); define each label once "
    "in subject_definitions and reuse it consistently. retention_analysis uses official "
    "markers: fully_preserved, partially_preserved, attribute_transfer, weak_reference; "
    "one line per label at left margin. Begin summary with the supplied task-type "
    "prefix. detailed_description is a playback-ordered single-shot [Shot 1] timeline "
    "(150-300 words) ending with the shot's final state plus negative guards (no "
    "readable text, no character drift, no prop teleport). Write camera motion as "
    "natural English using one motion type (Zoom In/Out, Push In/Pull Out, Pan "
    "Left/Right, Truck Left/Right, Tilt Up/Down, Pedestal Up/Down, Arc Shot, Tracking "
    "Shot, Static Shot, Shake Slightly/Strongly, POV, Roll Clockwise/Counterclockwise) "
    "plus amplitude (small/large) and speed (slow/fast) when meaningful. Diegetic "
    "ambience goes to overall_soundscape; score goes to non_diegetic_music (N/A when "
    "none). Match the requested duration exactly."
)


@dataclass
class Ref2VARewriteRequest:
    system_prompt: str
    user_payload: str
    fallback_text: str


class Ref2VATextOutput(BaseModel):
    """text provider 纯文本输出契约：六段改写结果原样返回。"""

    plain_text_output_field: ClassVar[str] = "prompt"

    prompt: str = Field(min_length=1)


_DIALOGUE_ENTRY_RE = re.compile(
    r"^第\d+句(?:对白)?[，,：:]\s*(.+?)说[：:]\s*[“\"](.+?)[”\"]\s*$",
    re.MULTILINE,
)


def extract_dialogue_lines(source: str) -> list[tuple[str, str]]:
    """从 12 段规范版提示词提取画内对白（第N句：X说：“原文”）。

    动态链路有结构化 dialogue cues 时应由调用方直接传入；本函数只做旧链路
    文本回退，避免改写模型自行猜测说话人与台词。
    """

    lines: list[tuple[str, str]] = []
    for match in _DIALOGUE_ENTRY_RE.finditer(source):
        speaker = match.group(1).strip()
        text = match.group(2).strip()
        if speaker and text:
            lines.append((speaker, text))
    return lines


def build_ref2va_rewrite_request(
    source: str,
    *,
    reference_labels: Mapping[str, str] | None = None,
    dialogue_lines: Sequence[tuple[str, str] | tuple[str, str, tuple[float, float]]] = (),
    duration_sec: float | None = None,
    task_prefix: str = "keyframe completion + reference generation",
) -> Ref2VARewriteRequest:
    """构造一次受控的 H3 原生格式改写请求（实际改写由 text provider 执行）。

    dialogue_lines 每项为 (说话人, 台词) 或 (说话人, 台词, (开始秒, 结束秒))；
    说话人 ID 按首次出声顺序稳定分配，同一说话人多句共用同一 ID。
    """

    sections = _split_sections(source)
    if duration_sec is None:
        total_match = _TOTAL_RE.search(sections.get("全局风格", ""))
        duration_sec = float(total_match.group(1)) if total_match else None
    labels = {
        "Picture 1": "current keyframe / composition anchor",
        **dict(reference_labels or {}),
    }
    speakers: dict[str, str] = {}
    dialogue_payload: list[dict[str, Any]] = []
    for item in dialogue_lines:
        speaker, text = item[0], item[1]
        time_range = item[2] if len(item) > 2 else None
        # 说话人 ID 按出场顺序连续编号（S1, S2, ...）：首个出现的说话人固定
        # 拿到 S1，与它出现在第几行无关。
        if speaker not in speakers:
            speakers[speaker] = f"S{len(speakers) + 1}"
        dialogue_payload.append(
            {
                "speaker": speaker,
                "text": text,
                "time_range_sec": (
                    [float(time_range[0]), float(time_range[1])] if time_range else None
                ),
                "required_markup": f"<d>[Chinese] {text}</d>",
                "speaker_tag": speakers[speaker],
            }
        )
    payload = {
        "task_type_prefix": task_prefix,
        "target_duration_sec": duration_sec,
        "reference_labels": labels,
        "speakers": speakers,
        "source_text": source,
        "source_sections_zh": {
            key: sections[key] for key in _active_section_order(sections) if key in sections
        },
        "dialogue_lines": dialogue_payload,
        "requirements": [
            "exactly six sections, official order, English prose",
            "define every <Subject N>/<Picture N> once; reuse consistently",
            "one retention line per label using official markers",
            "playback-ordered [Shot 1] timeline; end state included",
            "every dialogue line verbatim at its source time point; no trimming",
            "camera motion as natural English: one motion type + optional amplitude/speed",
            "speaker voice identity (timbre/pitch/pace) at first appearance",
            "<cutoff> when speech is truncated by the video end",
            "map @ImageN to <Picture N> with the same N",
            "no plot summary outside described audiovisual events",
        ],
    }
    compact = compact_chinese_prompt(source)
    return Ref2VARewriteRequest(
        system_prompt=_REF2VA_SYSTEM_PROMPT,
        user_payload=json.dumps(payload, ensure_ascii=False, indent=2),
        fallback_text=compact.text,
    )


_REF2VA_HEADER_RE = re.compile(
    r"^(subject_definitions|summary|retention_analysis|detailed_description|"
    r"overall_soundscape|non_diegetic_music):\s*$",
    re.MULTILINE,
)
_REF2VA_ORDER = (
    "subject_definitions",
    "summary",
    "retention_analysis",
    "detailed_description",
    "overall_soundscape",
    "non_diegetic_music",
)
_REF_LABEL_RE = re.compile(r"<(?:Subject|Picture|Video|Audio)\s+\d+>")
_TIMECODE_RE = re.compile(r"At\s+(\d{2}):(\d{2})\.(\d{3})")


def _defined_labels(output: str) -> set[str]:
    """subject_definitions 段内出现过的引用标签（含 Subject 定义内引用的来源）。"""

    if "subject_definitions:" not in output:
        return set()
    head = output.split("subject_definitions:", 1)[1]
    if "summary:" in head:
        head = head.split("summary:", 1)[0]
    return set(_REF_LABEL_RE.findall(head))


def _referenced_labels(output: str) -> set[str]:
    """subject_definitions 段之外引用的标签；未定义的引用属于改写缺陷。"""

    if "subject_definitions:" not in output:
        return set(_REF_LABEL_RE.findall(output))
    _, _, after = output.partition("subject_definitions:")
    _, _, rest = after.partition("summary:")
    return set(_REF_LABEL_RE.findall(rest))


def validate_ref2va_sections(
    output: str,
    *,
    expect_dialogue: bool = False,
    expect_dialogue_count: int | None = None,
    expected_dialogue_texts: Sequence[str] = (),
    expected_duration_sec: float | None = None,
) -> list[str]:
    """校验模型改写结果是否符合官方六段契约；返回错误清单，空列表表示通过。"""

    errors: list[str] = []
    found = [match.group(1) for match in _REF2VA_HEADER_RE.finditer(output)]
    if found != list(_REF2VA_ORDER):
        errors.append(f"six-section order mismatch, got={found}")
    if expect_dialogue and "<d>[Chinese]" not in output:
        errors.append("dialogue markup <d>[Chinese] ...</d> missing")
    markup_count = output.count("<d>[Chinese]")
    if expect_dialogue_count is not None and markup_count < expect_dialogue_count:
        errors.append(
            f"dialogue count mismatch: expected {expect_dialogue_count} lines, "
            f"found {markup_count}"
        )
    for text in expected_dialogue_texts:
        if text.strip() and text.strip() not in output:
            errors.append(f"dialogue line not preserved verbatim: {text[:30]}")
    if "retention_analysis:" in output:
        body = output.split("retention_analysis:", 1)[1].split("detailed_description:", 1)[0]
        marker_lines = [line for line in body.splitlines() if line.strip().startswith("<")]
        if not marker_lines:
            errors.append("retention_analysis has no per-label marker lines")
    for label in sorted(_referenced_labels(output) - _defined_labels(output)):
        errors.append(f"reference label {label} is used but never defined")
    if expected_duration_sec is not None:
        for match in _TIMECODE_RE.finditer(output):
            seconds = (
                int(match.group(1)) * 60 + int(match.group(2)) + int(match.group(3)) / 1000
            )
            # 等于视频末尾的切点属于合法边界，只有严格超过时长才算越界。
            if seconds > expected_duration_sec:
                errors.append(
                    f"cut time {match.group(0)} exceeds requested duration "
                    f"{expected_duration_sec:g}s"
                )
    return errors


@dataclass
class H3PromptBundle:
    compact: CompactResult
    ref2va_request: Ref2VARewriteRequest | None = None


def convert_compiled_prompt(
    source: str,
    *,
    build_native: bool = False,
    **native_kwargs: Any,
) -> H3PromptBundle:
    """便捷入口：完整规范版 -> 精简执行版（默认）/ 附带原生六段改写请求。"""

    compact = compact_chinese_prompt(source)
    request = build_ref2va_rewrite_request(source, **native_kwargs) if build_native else None
    return H3PromptBundle(compact=compact, ref2va_request=request)
