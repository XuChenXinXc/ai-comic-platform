"""导演设计层：DirectorPlan 结构、对白绑定、最终一致性校验与自然语言渲染器。

本模块是「分镜视频提示词生成」重构的核心：把「为什么拍 / 怎么拍」的导演决策从
「填充固定约束模板」中拆成独立结构，最终 Prompt 只渲染已经计算完成的结果。

纯函数、无数据库与 Provider 依赖，便于单测与复用：
- DirectorPlan / DialogueBinding 等 Pydantic 结构；
- validate_shot_consistency()：最终一致性校验器；
- render_director_video_prompt()：导演口吻自然语言最终提示词渲染器
  （不再输出 10 段【xxx】标题与机械时间轴）。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---------------------------------------------------------------------------
# 结构定义
# ---------------------------------------------------------------------------

Composition = Literal[
    "centered",
    "left-weighted",
    "right-weighted",
    "environment-dominant",
    "negative-space",
    "foreground-obstructed",
    "layered-depth",
]

VoiceSource = Literal[
    "character",
    "device",
    "broadcast",
    "phone",
    "offscreen",
]

_COMPOSITION_ZH: dict[str, str] = {
    "centered": "人物居中",
    "left-weighted": "画面重心偏左",
    "right-weighted": "画面重心偏右",
    "environment-dominant": "环境占主导、人物退为画中元素",
    "negative-space": "大面积留白/负空间",
    "foreground-obstructed": "前景遮挡、人物半隐",
    "layered-depth": "前中后景层层递进",
}


class AudienceFocus(BaseModel):
    """观众视觉注意力的第一 / 第二 / 第三落点，避免人物、场景与世界观元素平均展示。"""

    model_config = ConfigDict(extra="forbid")

    first: str = Field(min_length=1)
    second: str = ""
    third: str = ""


class MicroPerformance(BaseModel):
    """单个自然微动作。必须来自人物状态、剧情动作或环境刺激，不得无故加戏。"""

    model_config = ConfigDict(extra="forbid")

    trigger: str = Field(default="", description="触发时机或诱因")
    action: str = Field(min_length=1, description="具体微动作")
    source: str = Field(default="", description="来源：人物状态 / 剧情动作 / 环境刺激")


class DialogueBinding(BaseModel):
    """结构化对白绑定。真人角色 requires_lip_sync=True；设备/广播/电话等非人物发声为 False。"""

    model_config = ConfigDict(extra="forbid")

    speaker: str = Field(min_length=1)
    text: str = Field(min_length=1)
    start_time: float = Field(ge=0)
    end_time: float = Field(gt=0)
    requires_lip_sync: bool = True
    voice_source: VoiceSource = "character"

    @model_validator(mode="after")
    def validate_time_range(self) -> DialogueBinding:
        if self.end_time <= self.start_time:
            raise ValueError("对白 end_time 必须大于 start_time")
        return self


class DirectorTimelineSegment(BaseModel):
    """唯一绝对时间轴的一段：start / end 单位秒，action 为可执行动作描述。"""

    model_config = ConfigDict(extra="forbid")

    start: float = Field(ge=0)
    end: float = Field(gt=0)
    action: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_time_range(self) -> DirectorTimelineSegment:
        if self.end <= self.start:
            raise ValueError("时间轴 end 必须大于 start")
        return self


class DirectorPlan(BaseModel):
    """导演设计层：只决定「如何呈现」，不修改剧情 / 连续性 / 资产等既有事实。"""

    model_config = ConfigDict(extra="forbid")

    shot_purpose: str = Field(min_length=1)
    audience_focus: AudienceFocus
    composition: Composition
    foreground: str = ""
    midground: str = ""
    background: str = ""
    camera_motivation: str = ""
    performance: str = ""
    micro_performance: list[MicroPerformance] = Field(default_factory=list, max_length=2)
    environment_motion: str = ""
    visual_tone: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class DirectorShotBundle:
    """导演设计层的一次完整产出：导演计划 + 唯一时间轴 + 对白绑定。"""

    director_plan: DirectorPlan
    timeline: list[DirectorTimelineSegment]
    dialogue_bindings: list[DialogueBinding]


_COMPOSITION_ALIASES: dict[str, str] = {
    "centered": "centered",
    "center": "centered",
    "居中": "centered",
    "left-weighted": "left-weighted",
    "left": "left-weighted",
    "偏左": "left-weighted",
    "right-weighted": "right-weighted",
    "right": "right-weighted",
    "偏右": "right-weighted",
    "environment-dominant": "environment-dominant",
    "环境主导": "environment-dominant",
    "negative-space": "negative-space",
    "negative space": "negative-space",
    "留白": "negative-space",
    "负空间": "negative-space",
    "foreground-obstructed": "foreground-obstructed",
    "前景遮挡": "foreground-obstructed",
    "layered-depth": "layered-depth",
    "前中后景": "layered-depth",
    "层次递进": "layered-depth",
}


def _coerce_composition(value: Any) -> Composition:
    text = _text(value).casefold()
    return cast(Composition, _COMPOSITION_ALIASES.get(text, "centered"))


def director_bundle_from_llm(output: Mapping[str, Any]) -> DirectorShotBundle:
    """把导演设计 LLM 输出（camelCase 契约）转换为运行时结构。

    兼容 camelCase（shotPurpose / audienceFocus / …）与 snake_case 两种键名。
    """

    def pick(*names: str, default: Any = None) -> Any:
        for name in names:
            if name in output and output[name] not in (None, "", []):
                return output[name]
        return default

    focus_raw = pick("audienceFocus", "audience_focus") or {}
    focus = AudienceFocus(
        first=_text(focus_raw.get("first", "")),
        second=_text(focus_raw.get("second", "")),
        third=_text(focus_raw.get("third", "")),
    )

    micro_raw = pick("microPerformance", "micro_performance") or []
    micro = [
        MicroPerformance(
            trigger=_text(item.get("trigger", "")),
            action=_text(item.get("action", "")),
            source=_text(item.get("source", "")),
        )
        for item in micro_raw
        if isinstance(item, Mapping) and _text(item.get("action", ""))
    ][:2]

    plan = DirectorPlan(
        shot_purpose=_text(pick("shotPurpose", "shot_purpose")) or "本镜叙事推进",
        audience_focus=focus,
        composition=_coerce_composition(pick("composition", "composition")),
        foreground=_text(pick("foreground", "foreground")),
        midground=_text(pick("midground", "midground")),
        background=_text(pick("background", "background")),
        camera_motivation=_text(pick("cameraMotivation", "camera_motivation")),
        performance=_text(pick("performance", "performance")),
        micro_performance=micro,
        environment_motion=_text(pick("environmentMotion", "environment_motion")),
        visual_tone=[
            _text(item)
            for item in (pick("visualTone", "visual_tone") or [])
            if _text(item)
        ],
    )

    timeline_raw = pick("timeline", "timeline") or []
    timeline = [
        DirectorTimelineSegment(
            start=float(item.get("start", 0.0) or 0.0),
            end=float(item.get("end", 0.0) or 0.0),
            action=_text(item.get("action", "")),
        )
        for item in timeline_raw
        if isinstance(item, Mapping) and _text(item.get("action", ""))
    ]

    dialogue_raw = pick("dialogueBindings", "dialogue_bindings") or []
    dialogue_bindings = [
        DialogueBinding(
            speaker=_text(item.get("speaker", "")),
            text=_text(item.get("text", "")),
            start_time=float(item.get("startTime", item.get("start_time", 0.0)) or 0.0),
            end_time=float(item.get("endTime", item.get("end_time", 0.0)) or 0.0),
            requires_lip_sync=bool(
                item.get("requiresLipSync", item.get("requires_lip_sync", True))
            ),
            voice_source=cast(
                VoiceSource,
                _text(item.get("voiceSource", item.get("voice_source", "character"))),
            ),
        )
        for item in dialogue_raw
        if isinstance(item, Mapping) and _text(item.get("text", ""))
    ]

    return DirectorShotBundle(
        director_plan=plan,
        timeline=timeline,
        dialogue_bindings=dialogue_bindings,
    )


# ---------------------------------------------------------------------------
# 一致性校验器
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConsistencyIssue:
    """一致性校验发现的单个问题。"""

    code: str
    message: str
    severity: Literal["warning", "blocking"] = "warning"
    details: dict[str, Any] = field(default_factory=dict)


_EPS = 0.01

# 非人物发声源关键词：这些「说话者」不产生画内口型
_NON_LIP_SYNC_MARKERS = (
    "音箱",
    "广播",
    "电话",
    "对讲",
    "录音",
    "旁白",
    "画外音",
    "语音",
    "喇叭",
    "电台",
)
_OFFSCREEN_MARKERS = ("旁白", "画外音", "内心", "OS", "独白")

# 姿态关键词（用于动作与起始姿态冲突检测）
_POSTURE_WORDS = {
    "standing": ("站", "起身", "起立", "站立"),
    "sitting": ("坐", "坐下", "落座"),
    "crouching": ("蹲", "蹲下"),
    "lying": ("躺", "卧", "蜷缩", "趴"),
}

# 互相矛盾的运镜词对（同一镜同时出现即告警）
_CONFLICTING_CAMERA_PAIRS = (
    (("固定", "静止", "不动"), ("跟随", "跟拍", "推近", "横移", "环绕", "升降", "甩", "摇")),
)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, int | float) and not isinstance(value, bool):
        return f"{value:g}"
    return ""


def _has_word(text: str, *words: str) -> bool:
    return any(word and word in text for word in words)


def normalize_voice_source(speaker: str) -> tuple[VoiceSource, bool]:
    """按说话人名称推导发声来源与是否需要口型。

    真人角色默认 requires_lip_sync=True；蓝牙音箱 / 广播 / 电话等非人物发声
    requires_lip_sync=False。
    """
    speaker_text = _text(speaker)
    if _has_word(speaker_text, *_NON_LIP_SYNC_MARKERS):
        if _has_word(speaker_text, *_OFFSCREEN_MARKERS):
            return "offscreen", False
        if _has_word(speaker_text, "广播", "电台", "喇叭"):
            return "broadcast", False
        if _has_word(speaker_text, "电话", "对讲"):
            return "phone", False
        return "device", False
    return "character", True


def _timeline_issues(
    timeline: Sequence[DirectorTimelineSegment | Mapping[str, Any]],
    duration_sec: float,
) -> list[ConsistencyIssue]:
    """校验：时间轴是否从 0 完整覆盖到总时长且无重叠 / 无缝隙。"""
    issues: list[ConsistencyIssue] = []
    if not timeline:
        return [
            ConsistencyIssue(
                "timeline_empty",
                "唯一时间轴为空，缺少可执行的动作分段",
                "blocking",
            )
        ]
    segments: list[tuple[float, float, str]] = []
    for index, item in enumerate(timeline):
        if isinstance(item, Mapping):
            start = float(item.get("start", item.get("start_time", 0.0)) or 0.0)
            end = float(item.get("end", item.get("end_time", 0.0)) or 0.0)
            action = _text(item.get("action", ""))
        else:
            start, end, action = item.start, item.end, item.action
        if end <= start:
            issues.append(
                ConsistencyIssue(
                    "timeline_invalid_segment",
                    f"时间轴第 {index + 1} 段 end 不大于 start",
                    "blocking",
                    {"index": index, "start": start, "end": end},
                )
            )
            continue
        segments.append((start, end, action))
    segments.sort(key=lambda item: (item[0], item[1]))

    if segments and abs(segments[0][0]) > _EPS:
        issues.append(
            ConsistencyIssue(
                "timeline_gap_at_start",
                "时间轴必须从 0 秒开始",
                "blocking",
                {"first_start": segments[0][0]},
            )
        )
    for position, (previous, current) in enumerate(
        zip(segments, segments[1:], strict=False), start=1
    ):
        if abs(current[0] - previous[1]) > _EPS:
            issues.append(
                ConsistencyIssue(
                    "timeline_gap_or_overlap",
                    "时间轴相邻段必须首尾相接，不允许间隙或重叠",
                    "blocking",
                    {
                        "segment_index": position,
                        "previous_end": previous[1],
                        "current_start": current[0],
                    },
                )
            )
    if segments:
        last_end = segments[-1][1]
        if abs(last_end - duration_sec) > _EPS:
            issues.append(
                ConsistencyIssue(
                    "timeline_end_mismatch",
                    f"时间轴末段结束于 {last_end:g}s，与总时长 {duration_sec:g}s 不一致",
                    "blocking",
                    {"timeline_end": last_end, "duration_sec": duration_sec},
                )
            )
    return issues


def _dialogue_issues(
    dialogue_bindings: Sequence[DialogueBinding | Mapping[str, Any]],
    source_dialogue: Sequence[Mapping[str, Any] | str],
    duration_sec: float,
) -> list[ConsistencyIssue]:
    """校验：对白是否出现在对白列表、是否被重复、时间是否落在时长内。"""
    issues: list[ConsistencyIssue] = []

    def binding_payload(value: DialogueBinding | Mapping[str, Any]) -> dict[str, Any]:
        if isinstance(value, DialogueBinding):
            return value.model_dump()
        return dict(value)

    def source_texts() -> list[str]:
        result: list[str] = []
        for item in source_dialogue or []:
            if isinstance(item, str):
                result.append(item)
            elif isinstance(item, Mapping):
                result.append(_text(item.get("text") or item.get("content") or item.get("line")))
        return [item for item in result if item]

    texts = source_texts()
    bindings = [binding_payload(item) for item in dialogue_bindings or []]

    seen_keys: set[tuple[str, str, float, float]] = set()
    for index, item in enumerate(bindings):
        speaker = _text(item.get("speaker"))
        text = _text(item.get("text"))
        start = float(item.get("start_time", 0.0) or 0.0)
        end = float(item.get("end_time", 0.0) or 0.0)

        if text and texts:
            normalized = re.sub(r"\s+", "", text)
            normalized_sources = [re.sub(r"\s+", "", item) for item in texts]
            if not any(
                normalized in source or source in normalized
                for source in normalized_sources
            ):
                issues.append(
                    ConsistencyIssue(
                        "dialogue_not_in_list",
                        f"对白「{text[:20]}」未出现在分镜对白列表中",
                        "blocking",
                        {"index": index, "speaker": speaker},
                    )
                )
        if end > duration_sec + _EPS:
            issues.append(
                ConsistencyIssue(
                    "dialogue_over_time",
                    f"对白「{text[:20]}」结束时间 {end:g}s 超过总时长 {duration_sec:g}s",
                    "blocking",
                    {"index": index, "end_time": end, "duration_sec": duration_sec},
                )
            )
        if end <= start:
            issues.append(
                ConsistencyIssue(
                    "dialogue_invalid_time",
                    f"对白「{text[:20]}」end_time 不大于 start_time",
                    "blocking",
                    {"index": index},
                )
            )
            continue
        key = (speaker, text, start, end)
        if key in seen_keys:
            issues.append(
                ConsistencyIssue(
                    "dialogue_duplicated",
                    f"对白「{text[:20]}」被重复绑定",
                    "blocking",
                    {"index": index, "speaker": speaker},
                )
            )
        seen_keys.add(key)

    # 列表中的对白应全部被绑定（防止漏绑导致口型缺失）。
    bound_texts = [re.sub(r"\s+", "", _text(item.get("text"))) for item in bindings]
    for text in texts:
        normalized = re.sub(r"\s+", "", text)
        if normalized and not any(
            normalized in bound or bound in normalized for bound in bound_texts
        ):
            issues.append(
                ConsistencyIssue(
                    "dialogue_missing_binding",
                    f"对白列表中的「{text[:20]}」未被结构化绑定",
                    "warning",
                    {"text": text},
                )
            )
    return issues


def _continuity_issues(
    transition_from_prev: str,
    prev_end_state: str,
    start_state: str,
    next_start_state: str,
) -> list[ConsistencyIssue]:
    """校验：上一镜结束状态与下一镜开始状态是否成立（文本弱匹配）。"""
    issues: list[ConsistencyIssue] = []
    if transition_from_prev not in {"continuous", "elliptical"}:
        return issues
    prev = _text(prev_end_state)
    start = _text(start_state)
    if prev and start and transition_from_prev == "continuous":
        # 连续镜头：本镜起始状态应承接上一镜结束状态（按双字 n-gram 共享判断）。
        def bigrams(text: str) -> set[str]:
            compact = re.sub(r"\s+", "", text)
            return {compact[index : index + 2] for index in range(len(compact) - 1)}

        prev_tokens = bigrams(prev)
        start_tokens = bigrams(start)
        shared = prev_tokens & start_tokens
        if prev_tokens and start_tokens and not shared:
            issues.append(
                ConsistencyIssue(
                    "continuity_mismatch",
                    "continuous 镜头起始状态与上一镜结束状态缺少承接证据",
                    "warning",
                    {"prev_end_state": prev, "start_state": start},
                )
            )
    return issues


# 动态物体运动一致性（如自动门）：连续承接或同场切镜时，
# 上一镜结束状态与本镜开始状态对同一物体的运动状态不应直接互斥。
_DYNAMIC_OBJECT_MARKERS = ("门", "窗", "帘", "电梯", "雨", "雪", "灯", "幕")
_OPEN_MOTION = ("滑开", "打开", "拉开", "升起", "亮起", "开")
_CLOSE_MOTION = ("关闭", "合上", "拉上", "落下", "熄灭", "关")


def _motion_state(text: str, index: int, marker: str) -> str | None:
    """取动态物体标记词附近的运动状态：open / close / None。"""

    def first_match(region: str) -> str | None:
        matches: list[tuple[int, int, str]] = []
        for words, state in ((_OPEN_MOTION, "open"), (_CLOSE_MOTION, "close")):
            for word in words:
                position = region.find(word)
                if position >= 0:
                    matches.append((position, len(word), state))
        if not matches:
            return None
        # 最早出现优先；同位置取更长词（「滑开」优先于「开」）。
        matches.sort(key=lambda item: (item[0], -item[1]))
        return matches[0][2]

    window = text[index + len(marker) : index + len(marker) + 6]
    state = first_match(window)
    if state is not None:
        return state
    return first_match(text[max(0, index - 5) : index])


def _dynamic_object_issues(
    transition_from_prev: str,
    prev_end_state: str,
    start_state: str,
) -> list[ConsistencyIssue]:
    """校验：自动门等动态物体的运动方式是否前后一致。"""
    if transition_from_prev not in {"continuous", "same_scene_cut"}:
        return []
    prev = _text(prev_end_state)
    start = _text(start_state)
    if not prev or not start:
        return []
    issues: list[ConsistencyIssue] = []
    for marker in _DYNAMIC_OBJECT_MARKERS:
        prev_index = prev.find(marker)
        start_index = start.find(marker)
        if prev_index < 0 or start_index < 0:
            continue
        prev_motion = _motion_state(prev, prev_index, marker)
        start_motion = _motion_state(start, start_index, marker)
        if prev_motion and start_motion and prev_motion != start_motion:
            issues.append(
                ConsistencyIssue(
                    "dynamic_object_conflict",
                    f"动态物体「{marker}」在上一镜结束状态与本镜开始状态的运动方式不一致",
                    "warning",
                    {
                        "object": marker,
                        "prev_end_state": prev,
                        "start_state": start,
                    },
                )
            )
    return issues


def _prop_issues(
    shot: Mapping[str, Any],
    end_state: str,
) -> list[ConsistencyIssue]:
    """校验：持有道具是否在结束状态中交代去向，避免凭空消失。"""
    issues: list[ConsistencyIssue] = []
    props = shot.get("props") or shot.get("prop_names") or shot.get("prop_ids") or []
    if isinstance(props, str):
        props = [props]
    if not props:
        return issues
    end = _text(end_state)
    action = _text(shot.get("action"))
    for prop in props:
        prop_text = _text(prop)
        if not prop_text:
            continue
        # 若道具在本镜动作中被提及，但结束状态既未交代去向也未出现在动作里，提示风险。
        if prop_text in action and prop_text not in end and not _has_word(
            end, "放下", "离手", "离开画面", "消失"
        ):
            issues.append(
                ConsistencyIssue(
                    "prop_disappeared",
                    f"道具「{prop_text}」出现在动作中但结束状态未交代去向",
                    "warning",
                    {"prop": prop_text},
                )
            )
    return issues


def _posture_issues(
    start_state: str,
    action: str,
    positions: Sequence[Mapping[str, Any]],
) -> list[ConsistencyIssue]:
    """校验：人物动作是否与起始姿态冲突。"""
    issues: list[ConsistencyIssue] = []
    start = _text(start_state)
    act = _text(action)
    position_text = "；".join(
        _text(item.get("position") or item.get("facing") or "")
        for item in (positions or [])
        if isinstance(item, Mapping)
    )
    body_text = f"{start} {position_text}"
    if not body_text.strip() or not act:
        return issues

    def posture_in(body: str, words: tuple[str, ...]) -> bool:
        return _has_word(body, *words)

    if posture_in(body_text, _POSTURE_WORDS["lying"]) and _has_word(
        act, "走", "跑", "站起", "起身"
    ):
        if not _has_word(act, "起身", "站起", "爬起", "坐起"):
            issues.append(
                ConsistencyIssue(
                    "posture_conflict",
                    "起始姿态为躺/蜷缩，但动作直接进入站立/行走且缺少起身过渡",
                    "warning",
                    {"start_state": start, "action": act},
                )
            )
    if posture_in(body_text, _POSTURE_WORDS["sitting"]) and _has_word(act, "走", "跑"):
        if not _has_word(act, "起身", "站起"):
            issues.append(
                ConsistencyIssue(
                    "posture_conflict",
                    "起始姿态为坐姿，但动作直接进入行走且缺少起身过渡",
                    "warning",
                    {"start_state": start, "action": act},
                )
            )
    return issues


def _camera_issues(
    camera_motivation: str,
    composition: str,
    camera_move: str,
) -> list[ConsistencyIssue]:
    """校验：同一镜是否出现互相矛盾的镜头运动或构图描述。"""
    issues: list[ConsistencyIssue] = []
    camera_text = f"{_text(camera_motivation)} {_text(camera_move)}"
    for static_words, move_words in _CONFLICTING_CAMERA_PAIRS:
        has_static = _has_word(camera_text, *static_words)
        has_move = _has_word(camera_text, *move_words)
        if has_static and has_move:
            issues.append(
                ConsistencyIssue(
                    "camera_contradiction",
                    "同一镜同时出现「固定机位」与运镜描述，互相矛盾",
                    "warning",
                    {"camera_text": camera_text.strip()},
                )
            )
            break
    if composition == "centered" and _has_word(camera_text, "负空间", "留白"):
        issues.append(
            ConsistencyIssue(
                "camera_contradiction",
                "构图声明为人物居中，但镜头说明又要求负空间/留白",
                "warning",
                {"composition": composition},
            )
        )
    return issues


def _premature_result_issues(
    action: str,
    end_state: str,
    timeline: Sequence[DirectorTimelineSegment | Mapping[str, Any]],
) -> list[ConsistencyIssue]:
    """校验：剧情尚未完成时不得提前执行后续结果（结束状态必须落在时间轴内）。"""
    issues: list[ConsistencyIssue] = []
    timeline_text = " ".join(
        _text(item.get("action")) if isinstance(item, Mapping) else _text(item.action)
        for item in (timeline or [])
    )
    end = _text(end_state)
    act = _text(action)
    if end and act and timeline_text:
        # 结束状态里出现的关键结果词，若既不在动作也不在时间轴内，提示「提前执行后续结果」风险。
        result_words = ("离开", "消失", "完成", "落下", "关闭", "倒下", "合上", "走到", "抵达")
        for word in result_words:
            if word in end and word not in act and word not in timeline_text:
                issues.append(
                    ConsistencyIssue(
                        "premature_result",
                        f"结束状态包含「{word}」结果，但动作与时间轴未交代该过程",
                        "warning",
                        {"word": word, "end_state": end},
                    )
                )
                break
    return issues


def validate_shot_consistency(
    *,
    shot: Mapping[str, Any] | None = None,
    director_plan: DirectorPlan | Mapping[str, Any] | None = None,
    timeline: Sequence[DirectorTimelineSegment | Mapping[str, Any]] = (),
    dialogue_bindings: Sequence[DialogueBinding | Mapping[str, Any]] = (),
    duration_sec: float = 0.0,
    prev_end_state: str = "",
    next_start_state: str = "",
) -> list[ConsistencyIssue]:
    """最终一致性校验器：对单个分镜执行结构性 + 语义性检查。

    参数均为只读事实；本函数不修改任何输入，只返回问题列表（空列表表示通过）。
    """
    shot = dict(shot or {})
    issues: list[ConsistencyIssue] = []

    issues.extend(_timeline_issues(timeline, duration_sec))
    issues.extend(_dialogue_issues(dialogue_bindings, shot.get("dialogue", []), duration_sec))
    issues.extend(
        _continuity_issues(
            _text(shot.get("transition_from_prev", "scene_change")),
            prev_end_state,
            _text(shot.get("start_state")),
            next_start_state,
        )
    )
    issues.extend(
        _dynamic_object_issues(
            _text(shot.get("transition_from_prev", "scene_change")),
            prev_end_state,
            _text(shot.get("start_state")),
        )
    )
    issues.extend(_prop_issues(shot, _text(shot.get("end_state"))))
    issues.extend(
        _posture_issues(
            _text(shot.get("start_state")),
            _text(shot.get("action")),
            shot.get("positions", []),
        )
    )

    camera_motivation = ""
    composition = ""
    if director_plan is not None:
        plan = (
            director_plan
            if isinstance(director_plan, DirectorPlan)
            else DirectorPlan.model_validate(director_plan)
        )
        camera_motivation = plan.camera_motivation
        composition = plan.composition
    issues.extend(
        _camera_issues(camera_motivation, composition, _text(shot.get("camera_move")))
    )
    issues.extend(
        _premature_result_issues(
            _text(shot.get("action")),
            _text(shot.get("end_state")),
            timeline,
        )
    )
    return issues


# ---------------------------------------------------------------------------
# 自然语言最终提示词渲染器
# ---------------------------------------------------------------------------

_HARD_OVERLAP_RE = re.compile(r"身份|服装|场景结构|人物|文字|消失|关键帧锁定")


def _render_visual_tone(visual_tone: Sequence[str]) -> str:
    values = [_text(item) for item in visual_tone if _text(item)]
    if not values:
        return ""
    return "、".join(values)


def _render_audience_focus(focus: AudienceFocus) -> str:
    parts = [f"观众的注意力先落在{_clean_text(focus.first)}"]
    if _clean_text(focus.second):
        parts.append(f"其次{_clean_text(focus.second)}")
    if _clean_text(focus.third):
        parts.append(f"最后{_clean_text(focus.third)}")
    return "，".join(parts) + "。"


def _clean_text(value: Any) -> str:
    """取文本并去掉句尾标点，供字段拼接使用。"""
    return _text(value).strip().rstrip("。，；、 ")


def _render_depth_layers(plan: DirectorPlan) -> str:
    parts: list[str] = []
    if _clean_text(plan.foreground):
        parts.append(f"前景是{_clean_text(plan.foreground)}")
    if _clean_text(plan.midground):
        parts.append(f"中景{_clean_text(plan.midground)}")
    if _clean_text(plan.background):
        parts.append(f"背景{_clean_text(plan.background)}")
    return "，".join(parts)


def _render_micro_performance(items: Sequence[MicroPerformance]) -> str:
    rendered: list[str] = []
    for item in items:
        base = _clean_text(item.action)
        if _clean_text(item.trigger):
            base = f"{_clean_text(item.trigger)}时，{base}"
        if _clean_text(item.source):
            base = f"{base}（{_clean_text(item.source)}）"
        rendered.append(base)
    return "；".join(rendered)


def _quote_text(text: str) -> str:
    """台词保持原文，仅在原文没有结尾标点时补句号。"""
    stripped = _text(text).strip()
    if stripped.endswith(("。", "！", "？", "…")):
        return stripped
    return f"{stripped}。"


def _render_dialogue(bindings: Sequence[DialogueBinding]) -> list[str]:
    if not bindings:
        return ["本镜没有画内对白，人物全程不开口。"]
    lines: list[str] = ["本镜对白按以下顺序进行："]
    for index, item in enumerate(bindings, start=1):
        speaker = _text(item.speaker)
        if item.requires_lip_sync:
            lines.append(f"第{index}句，{speaker}说：“{_quote_text(item.text)}”")
        else:
            lines.append(
                f"第{index}句是画外或设备发出的声音：“{_quote_text(item.text)}”，"
                "画面人物不开口。"
            )
    lines.append("说话人开口时口型自然配合，其余角色保持安静，不抢话、不代说。")
    return lines


def _timeline_flow_text(timeline: Sequence[DirectorTimelineSegment]) -> str:
    return "；".join(f"{item.start:g}-{item.end:g}秒：{_text(item.action)}" for item in timeline)


def _hard_constraint_line(hard: Sequence[str]) -> str:
    """把硬约束渲染成一条简短的自然语言负面约束。"""
    base = (
        "画面中不出现：人物身份或服装改变、场景结构被重构、"
        "新增无关人物、画面内文字、道具凭空消失。"
    )
    extras: list[str] = []
    for item in hard:
        extra_text = _text(item)
        if extra_text and extra_text not in extras and not _HARD_OVERLAP_RE.search(extra_text):
            extras.append(extra_text)
    if not extras:
        return base
    return base.rstrip("。") + "；" + "、".join(extras) + "。"


def _reference_sentences(anchors: Sequence[str]) -> list[str]:
    """把程序生成好的 @ImageN 锚点自然嵌入句子（编号与含义由调用方决定）。"""
    from app.services.natural_video_prompt import parse_reference_anchors

    tokens = parse_reference_anchors(anchors)
    lines: list[str] = []
    keyframe = next((item for item in tokens if item.role == "keyframe"), None)
    prev_end_frame = next((item for item in tokens if item.role == "prev_end_frame"), None)
    prev_keyframe = next((item for item in tokens if item.role == "previous_keyframe"), None)
    used = {
        item.token
        for item in (keyframe, prev_end_frame, prev_keyframe)
        if item is not None
    }
    if prev_end_frame and keyframe and prev_end_frame.token < keyframe.token:
        lines.append(
            f"画面从 {prev_end_frame.token}（上一镜的结束画面）自然进入，"
            f"向 {keyframe.token} 展示的目标画面发展。"
        )
    elif keyframe:
        lines.append(
            f"以 {keyframe.token} 作为这一镜的起始画面，"
            "沿用其中人物的位置、朝向、服装以及人物与场景之间的空间关系。"
        )
        if prev_end_frame:
            lines.append(
                f"上一镜的结束画面（{prev_end_frame.token}）仅用于衔接连贯，"
                "不改变本镜的起始构图。"
            )
    elif prev_end_frame:
        lines.append(f"起始状态承接 {prev_end_frame.token}（上一镜的结束画面）。")
    if prev_keyframe:
        lines.append(f"人物与空间的关系沿用 {prev_keyframe.token}（上一镜的画面）。")
    for item in tokens:
        if item.token in used:
            continue
        used.add(item.token)
        name = item.label.split("，")[0].split(",")[0].strip()
        if item.role == "character_identity":
            subject = name or "角色"
            lines.append(
                f"{subject}的外貌、发型和当天服装沿用 {item.token} 的参考素材，"
                "镜头过程中不发生变化。"
            )
        elif item.role == "scene_layout":
            subject = f"（{name}）" if name else ""
            lines.append(f"场景结构、陈设与光线沿用 {item.token}{subject}的参考素材。")
        elif item.role == "prop_identity":
            subject = name or "道具"
            lines.append(f"{subject}沿用 {item.token} 的外观与材质。")
        elif item.role == "scene_master":
            lines.append(f"场景母版空间结构沿用 {item.token}。")
        else:
            lines.append(f"{item.token} 作为画面参考。")
    return lines


def render_director_video_prompt(
    *,
    style_prompt: str,
    aspect_ratio: str,
    director_plan: DirectorPlan,
    timeline: Sequence[DirectorTimelineSegment],
    dialogue_bindings: Sequence[DialogueBinding],
    initial_state: str,
    end_state: str,
    shot_type: str = "中景",
    anchors: Sequence[str] = (),
    hard_constraints: Sequence[str] = (),
) -> str:
    """把导演设计与已算好的时间轴 / 对白渲染成自然语言导演式视频提示词。

    只渲染已经计算完成的结果，不再注入「锁定 / 禁止 / 保持稳定 / 唯一时间轴」
    等上游推理规则；时间轴去掉精确秒数，按粗粒度阶段自然表达。
    """
    from app.services.natural_video_prompt import naturalize_timeline_flow

    plan = (
        director_plan
        if isinstance(director_plan, DirectorPlan)
        else DirectorPlan.model_validate(director_plan)
    )

    paragraphs: list[str] = []

    # 第一段：风格、叙事意图与影像基调
    style_lines = [
        (
            f"整体延续{_text(style_prompt).rstrip('。')}的风格，"
            f"画面比例 {_text(aspect_ratio)}。"
            if _text(style_prompt)
            else f"画面比例 {_text(aspect_ratio)}。"
        ),
        f"本镜的叙事意图：{_clean_text(plan.shot_purpose)}。",
        _render_audience_focus(plan.audience_focus),
    ]
    visual_tone = _render_visual_tone(plan.visual_tone)
    if visual_tone:
        style_lines.append(f"影像基调：{visual_tone}。")
    paragraphs.append("".join(style_lines))

    # 第二段：起始状态与素材引用
    start_lines = [*_reference_sentences(anchors)]
    if _text(initial_state):
        start_lines.append(f"这一镜开始时，{_clean_text(initial_state)}。")
    if start_lines:
        paragraphs.append("".join(start_lines))

    # 第三段：表演与动作发展
    performance_parts = [
        part
        for part in (
            _clean_text(plan.performance),
            _render_micro_performance(plan.micro_performance),
        )
        if part
    ]
    action_lines: list[str] = []
    if performance_parts:
        action_lines.append("；".join(performance_parts) + "。")
    flow = naturalize_timeline_flow(
        _timeline_flow_text(timeline),
        float(timeline[-1].end) if timeline else 0.0,
    )
    if flow:
        action_lines.append(f"从这一起始状态出发，{flow}")
    if action_lines:
        paragraphs.append("".join(action_lines))

    # 第四段：镜头与构图
    camera_line = (
        f"景别为{_text(shot_type)}，"
        f"构图{_COMPOSITION_ZH.get(plan.composition, plan.composition)}。"
    )
    if _text(plan.camera_motivation):
        camera_line += f"镜头的运动动机是{_clean_text(plan.camera_motivation)}。"
    depth = _render_depth_layers(plan)
    if depth:
        camera_line += f"{depth}。"
    paragraphs.append(camera_line)

    # 第五段：环境动态、对白、结束状态与负面约束
    tail_lines: list[str] = []
    environment_motion = _text(plan.environment_motion).strip("。")
    if environment_motion:
        tail_lines.append(f"{environment_motion}。")
    tail_lines.extend(_render_dialogue(dialogue_bindings))
    if _text(end_state):
        tail_lines.append(f"接近结尾时，{_text(end_state).rstrip('。')}。")
    tail_lines.append(_hard_constraint_line(hard_constraints))
    paragraphs.append("".join(tail_lines))

    return "\n\n".join(part for part in paragraphs if part.strip())


__all__ = [
    "AudienceFocus",
    "Composition",
    "ConsistencyIssue",
    "DialogueBinding",
    "DirectorPlan",
    "DirectorShotBundle",
    "DirectorTimelineSegment",
    "MicroPerformance",
    "VoiceSource",
    "director_bundle_from_llm",
    "normalize_voice_source",
    "render_director_video_prompt",
    "validate_shot_consistency",
]
