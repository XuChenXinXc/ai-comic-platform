"""动态流水线的纯字幕时间轴与人工校正工具。

本模块只处理内存中的字幕数据，不读写数据库或文件。legacy 字幕服务仍由
``app.services.subtitle_service`` 负责；这里的时间轴使用行级对白 ID 和音频
ID，以避免一镜多对白互相覆盖。
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

_MISSING = object()
_EPSILON = 1e-9


class _StrictSubtitleModel(BaseModel):
    """字幕模型的共同严格配置。"""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
    )


class SubtitleLine(_StrictSubtitleModel):
    """一条相对某个分镜的字幕行。"""

    dialogue_line_id: str = Field(min_length=1)
    audio_id: str | None = Field(default=None, min_length=1)
    text: str = Field(min_length=1)
    start_time: float = Field(ge=0)
    end_time: float = Field(gt=0)
    style_preset: str = Field(default="default", min_length=1)

    # 这些字段是纯时间轴的可选追踪信息，不影响 legacy subtitle 表。
    speaker: str = ""
    shot_id: str | None = Field(default=None, min_length=1)
    revision: int = Field(default=1, ge=1)
    video_duration_snapshot: float | None = Field(default=None, gt=0)
    provenance: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_finite_values(self) -> SubtitleLine:
        if not math.isfinite(self.start_time) or not math.isfinite(self.end_time):
            raise ValueError("字幕时间必须是有限数值")
        if self.video_duration_snapshot is not None and not math.isfinite(
            self.video_duration_snapshot
        ):
            raise ValueError("video_duration_snapshot 必须是有限数值")
        if self.end_time <= self.start_time:
            raise ValueError("end_time 必须大于 start_time")
        return self


class SubtitleTimeline(_StrictSubtitleModel):
    """字幕时间轴及其版本、时长快照和可追溯信息。"""

    lines: list[SubtitleLine] = Field(default_factory=list)
    revision: int = Field(default=1, ge=1)
    video_duration_snapshot: float | None = Field(default=None, gt=0)
    timeline_signature: str = ""
    provenance: dict[str, Any] = Field(default_factory=dict)

    # source_signature 是输入来源签名的显式投影；provenance 中也会保留它，
    # 方便调用方只拿到 JSON 字段时诊断来源。
    source_signature: str = ""
    shot_id: str | None = Field(default=None, min_length=1)
    status: Literal["ready", "warning"] = "ready"
    warnings: list[str] = Field(default_factory=list)
    diagnostics: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_timeline(self) -> SubtitleTimeline:
        if self.video_duration_snapshot is not None and not math.isfinite(
            self.video_duration_snapshot
        ):
            raise ValueError("video_duration_snapshot 必须是有限数值")
        line_ids = [line.dialogue_line_id for line in self.lines]
        if len(line_ids) != len(set(line_ids)):
            raise ValueError("字幕时间轴不能包含重复 dialogue_line_id")
        if self.warnings and self.status == "ready":
            self.status = "warning"
        return self

    def __iter__(self) -> Any:
        """允许旧式调用方直接遍历 timeline.lines。"""

        return iter(self.lines)

    def __len__(self) -> int:
        return len(self.lines)

    def __getitem__(self, index: int) -> SubtitleLine:
        return self.lines[index]


class SubtitleCorrection(_StrictSubtitleModel):
    """针对一条对白行的人工校正；未提供的字段保持原值。"""

    dialogue_line_id: str = Field(min_length=1)
    text: str | None = Field(default=None, min_length=1)
    start_time: float | None = Field(default=None, ge=0)
    end_time: float | None = Field(default=None, gt=0)
    style_preset: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_correction(self) -> SubtitleCorrection:
        if (
            self.start_time is not None
            and not math.isfinite(self.start_time)
            or self.end_time is not None
            and not math.isfinite(self.end_time)
        ):
            raise ValueError("校正时间必须是有限数值")
        if (
            self.start_time is None
            and self.end_time is None
            and self.text is None
            and self.style_preset is None
        ):
            raise ValueError("校正至少需要修改一个字段")
        return self


class SubtitleTimelineError(ValueError):
    """字幕时间轴阻断错误，包含可供 UI/日志展示的结构化诊断。"""

    def __init__(
        self,
        message: str,
        *,
        diagnostics: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        self.diagnostics = [copy.deepcopy(dict(item)) for item in (diagnostics or ())]
        # ``errors`` 是面向调用方的兼容别名，避免调用方必须依赖具体字段名。
        self.errors = self.diagnostics
        detail = "; ".join(
            str(item.get("message", "")) for item in self.diagnostics if item.get("message")
        )
        super().__init__(f"{message}: {detail}" if detail else message)


def _diagnostic(
    code: str,
    message: str,
    *,
    path: str | None = None,
    details: Mapping[str, Any] | None = None,
    level: str = "blocking",
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "level": level,
        "code": code,
        "message": message,
    }
    if path is not None:
        result["path"] = path
    if details:
        result["details"] = copy.deepcopy(dict(details))
    return result


def _raise_blocking(message: str, diagnostics: Sequence[Mapping[str, Any]]) -> None:
    if diagnostics:
        raise SubtitleTimelineError(message, diagnostics=diagnostics)


def _plain(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return copy.deepcopy(value.model_dump(mode="python"))
    if isinstance(value, Mapping):
        return copy.deepcopy(dict(value))
    if hasattr(value, "__dict__"):
        return copy.deepcopy(vars(value))
    return copy.deepcopy(value)


def _mapping(value: Any) -> Mapping[str, Any] | None:
    plain = _plain(value)
    return plain if isinstance(plain, Mapping) else None


def _first(source: Mapping[str, Any], *names: str, default: Any = _MISSING) -> Any:
    for name in names:
        if name in source and source[name] is not None:
            return source[name]
    return default


def _clean_text(value: Any) -> str:
    if value is _MISSING or value is None:
        return ""
    return str(value).strip()


def _number(value: Any) -> float | None:
    if value is _MISSING or value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _number_present(value: Any) -> bool:
    return value is not _MISSING and value is not None


def _duration_from_value(value: Any) -> float | None:
    if isinstance(value, Mapping):
        value = _first(
            value,
            "duration_sec",
            "duration",
            "audio_duration_sec",
            "audio_duration",
            "actual_duration_sec",
            default=_MISSING,
        )
    return _number(value)


def _duration_lookup(
    durations: Any,
    *,
    audio_id: str | None,
    dialogue_line_id: str,
) -> tuple[float | None, str | None, bool]:
    """按 audio_id 优先、对白 ID 次之读取音频时长。"""

    if isinstance(durations, Mapping):
        for key, source in ((audio_id, "audio_id"), (dialogue_line_id, "dialogue_line_id")):
            if key and key in durations:
                value = durations[key]
                return _duration_from_value(value), source, value is not None
        return None, None, False

    if isinstance(durations, Sequence) and not isinstance(durations, str | bytes):
        for item in durations:
            item_mapping = _mapping(item)
            if item_mapping is None:
                continue
            item_audio_id = _clean_text(
                _first(item_mapping, "audio_id", "audioId", "id", default="")
            )
            item_line_id = _clean_text(
                _first(
                    item_mapping,
                    "dialogue_line_id",
                    "line_id",
                    "lineId",
                    default="",
                )
            )
            if (audio_id and item_audio_id == audio_id) or item_line_id == dialogue_line_id:
                raw_duration = _first(
                    item_mapping,
                    "duration_sec",
                    "duration",
                    "audio_duration_sec",
                    "audio_duration",
                    default=_MISSING,
                )
                return _duration_from_value(item_mapping), "records", raw_duration is not _MISSING
    return None, None, False


def _effective_duration(
    shot_duration_sec: Any,
    video_duration_sec: Any,
) -> tuple[float | None, str]:
    shot_duration = _number(shot_duration_sec)
    if shot_duration is None or shot_duration <= 0:
        return None, "invalid shot duration"
    if video_duration_sec is None:
        return shot_duration, "shot_duration_sec"
    video_duration = _number(video_duration_sec)
    if video_duration is None or video_duration <= 0:
        return None, "invalid video duration"
    return video_duration, "video_duration_sec"


def _line_core(line: SubtitleLine) -> dict[str, Any]:
    return {
        "dialogue_line_id": line.dialogue_line_id,
        "audio_id": line.audio_id,
        "text": line.text,
        "start_time": line.start_time,
        "end_time": line.end_time,
        "style_preset": line.style_preset,
        "speaker": line.speaker,
        "shot_id": line.shot_id,
    }


def _timeline_signature(
    lines: Sequence[SubtitleLine],
    *,
    source_signature: str,
    revision: int,
    video_duration_snapshot: float | None,
) -> str:
    payload = {
        "source_signature": source_signature,
        "revision": revision,
        "video_duration_snapshot": video_duration_snapshot,
        "lines": [_line_core(line) for line in lines],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _overlap_warnings(lines: Sequence[SubtitleLine]) -> list[str]:
    """重叠字幕不自动裁剪，保留输入并给出可读 warning。"""

    warnings: list[str] = []
    ordered = sorted(enumerate(lines), key=lambda item: (item[1].start_time, item[0]))
    for left_index in range(len(ordered)):
        _, left = ordered[left_index]
        for right_index in range(left_index + 1, len(ordered)):
            _, right = ordered[right_index]
            if right.start_time >= left.end_time:
                break
            warnings.append(
                f"字幕行重叠，已保留原始时间：{left.dialogue_line_id} 与 {right.dialogue_line_id}"
            )
    return warnings


def _timeline_with_signature(
    timeline: SubtitleTimeline,
    *,
    source_signature: str | None = None,
) -> SubtitleTimeline:
    signature_source = timeline.source_signature if source_signature is None else source_signature
    signature = _timeline_signature(
        timeline.lines,
        source_signature=signature_source,
        revision=timeline.revision,
        video_duration_snapshot=timeline.video_duration_snapshot,
    )
    payload = timeline.model_dump(mode="python")
    payload["source_signature"] = signature_source
    payload["timeline_signature"] = signature
    return SubtitleTimeline.model_validate(payload)


def _normalize_dialogue_line(raw: Any) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    source = _mapping(raw)
    if source is None:
        return None, [_diagnostic("invalid_dialogue_line", "对白行必须是对象")]

    nested_audio = _first(source, "audio", "audio_record", "audio_version", default=_MISSING)
    nested_audio_mapping = _mapping(nested_audio)

    dialogue_line_id = _clean_text(
        _first(
            source,
            "dialogue_line_id",
            "line_id",
            "lineId",
            "dialogueLineId",
            "id",
            "key",
            default="",
        )
    )
    audio_id = _clean_text(
        _first(
            source,
            "audio_id",
            "audioId",
            "audio_line_id",
            default=_MISSING,
        )
    )
    if not audio_id and nested_audio_mapping is not None:
        audio_id = _clean_text(
            _first(nested_audio_mapping, "audio_id", "audioId", "id", default="")
        )

    text = _clean_text(
        _first(
            source,
            "text",
            "content",
            "line",
            "dialogue",
            "speech",
            "dialogue_text",
            "dialogueText",
            default="",
        )
    )
    speaker = _clean_text(
        _first(
            source,
            "speaker",
            "character",
            "character_name",
            "characterName",
            "name",
            "role",
            default="",
        )
    )
    start_raw = _first(
        source,
        "start_time",
        "start_sec",
        "start",
        "startTime",
        default=_MISSING,
    )
    end_raw = _first(
        source,
        "end_time",
        "end_sec",
        "end",
        "endTime",
        default=_MISSING,
    )
    duration_raw = _first(
        source,
        "duration_sec",
        "duration",
        "audio_duration_sec",
        "audio_duration",
        default=_MISSING,
    )
    if duration_raw is _MISSING and nested_audio_mapping is not None:
        duration_raw = _first(
            nested_audio_mapping,
            "duration_sec",
            "duration",
            "audio_duration_sec",
            "audio_duration",
            default=_MISSING,
        )
    style_preset = (
        _clean_text(_first(source, "style_preset", "style", "subtitle_style", default="default"))
        or "default"
    )
    shot_id = _clean_text(_first(source, "shot_id", "shotId", default="")) or None

    return {
        "dialogue_line_id": dialogue_line_id,
        "audio_id": audio_id or None,
        "text": text,
        "speaker": speaker,
        "start_raw": start_raw,
        "end_raw": end_raw,
        "duration_raw": duration_raw,
        "style_preset": style_preset,
        "shot_id": shot_id,
    }, []


def build_subtitle_lines(
    dialogue_lines: Iterable[Any],
    *,
    audio_durations: Mapping[str, Any] | Sequence[Any] = {},
    shot_duration_sec: float,
    video_duration_sec: float | None = None,
    source_signature: str,
    revision: int = 1,
) -> SubtitleTimeline:
    """从对白行构建一个相对分镜时间轴。

    未提供明确起止时间时，按输入顺序从当前 cursor 开始；优先使用音频行时长，
    再使用对白自身时长，最后使用 shot 时长。任何超出有效时长的结果都会阻断，
    不会为了通过校验而静默截断。显式重叠被保留并记录 warning。
    """

    diagnostics: list[dict[str, Any]] = []
    effective_duration, duration_source = _effective_duration(
        shot_duration_sec,
        video_duration_sec,
    )
    if effective_duration is None:
        diagnostics.append(
            _diagnostic(
                "invalid_duration",
                "shot_duration_sec 和 video_duration_sec 必须是大于 0 的有限数值",
            )
        )

    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        diagnostics.append(_diagnostic("invalid_revision", "revision 必须是大于等于 1 的整数"))

    try:
        raw_lines = [item for item in dialogue_lines]
    except TypeError:
        raw_lines = []
        diagnostics.append(_diagnostic("invalid_dialogue_lines", "dialogue_lines 必须是可迭代对象"))

    lines: list[SubtitleLine] = []
    seen_ids: set[str] = set()
    cursor = 0.0
    fallback_duration = None
    shot_duration = _number(shot_duration_sec)
    if raw_lines and shot_duration is not None and shot_duration > 0:
        # 没有行级音频或显式时长时，均分 shot 时长，保证一镜多对白仍可独立排布。
        fallback_duration = shot_duration / len(raw_lines)

    for index, raw in enumerate(raw_lines):
        normalized, line_diagnostics = _normalize_dialogue_line(raw)
        if normalized is None:
            diagnostics.extend(
                _diagnostic(
                    item["code"],
                    item["message"],
                    path=f"dialogue_lines[{index}]",
                    details=item.get("details"),
                    level=item.get("level", "blocking"),
                )
                for item in line_diagnostics
            )
            continue

        path = f"dialogue_lines[{index}]"
        line_id = normalized["dialogue_line_id"]
        if not line_id:
            diagnostics.append(
                _diagnostic("missing_dialogue_line_id", "对白行缺少 dialogue_line_id", path=path)
            )
        elif line_id in seen_ids:
            diagnostics.append(
                _diagnostic(
                    "duplicate_dialogue_line_id",
                    f"重复 dialogue_line_id：{line_id}",
                    path=path,
                )
            )
        else:
            seen_ids.add(line_id)

        if not normalized["text"]:
            diagnostics.append(_diagnostic("missing_text", "对白行文本不能为空", path=path))

        start_raw = normalized["start_raw"]
        end_raw = normalized["end_raw"]
        duration_raw = normalized["duration_raw"]
        start = _number(start_raw) if _number_present(start_raw) else cursor
        if _number_present(start_raw) and start is None:
            diagnostics.append(
                _diagnostic("invalid_start_time", "start_time 不是有限数值", path=path)
            )

        explicit_end = _number(end_raw) if _number_present(end_raw) else None
        if _number_present(end_raw) and explicit_end is None:
            diagnostics.append(_diagnostic("invalid_end_time", "end_time 不是有限数值", path=path))

        audio_duration, duration_kind, audio_duration_present = _duration_lookup(
            audio_durations,
            audio_id=normalized["audio_id"],
            dialogue_line_id=line_id,
        )
        line_duration = _duration_from_value(duration_raw)
        if _number_present(duration_raw) and line_duration is None:
            diagnostics.append(
                _diagnostic("invalid_line_duration", "对白时长不是有限数值", path=path)
            )
        selected_duration = audio_duration if audio_duration is not None else line_duration
        selected_kind = duration_kind or ("line" if line_duration is not None else duration_source)

        if audio_duration_present and audio_duration is None:
            diagnostics.append(
                _diagnostic("invalid_audio_duration", "音频时长不是有限数值", path=path)
            )
        if audio_duration is not None and audio_duration <= 0:
            diagnostics.append(
                _diagnostic("invalid_audio_duration", "音频时长必须大于 0", path=path)
            )
        if line_duration is not None and line_duration <= 0:
            diagnostics.append(
                _diagnostic("invalid_line_duration", "对白时长必须大于 0", path=path)
            )

        if start is None:
            start = cursor
        if explicit_end is not None:
            end = explicit_end
        else:
            if selected_duration is None:
                selected_duration = fallback_duration or shot_duration
                selected_kind = "shot_duration_sec"
            end = (
                start + selected_duration
                if start is not None and selected_duration is not None
                else None
            )

        if start is None or end is None:
            diagnostics.append(_diagnostic("missing_time_range", "无法确定字幕时间范围", path=path))
            continue

        if start < 0:
            diagnostics.append(
                _diagnostic("negative_start_time", "start_time 不能小于 0", path=path)
            )
        if end <= start:
            diagnostics.append(
                _diagnostic("invalid_time_range", "end_time 必须大于 start_time", path=path)
            )
        if effective_duration is not None and end > effective_duration + _EPSILON:
            diagnostics.append(
                _diagnostic(
                    "subtitle_out_of_bounds",
                    f"字幕 end_time {end:g} 超出有效时长 {effective_duration:g}",
                    path=path,
                    details={"end_time": end, "effective_duration": effective_duration},
                )
            )

        # 出现数值错误时仍继续检查后续行，最终一次性返回所有 blocking 诊断。
        if not diagnostics or not any(item.get("path") == path for item in diagnostics):
            try:
                line = SubtitleLine(
                    dialogue_line_id=line_id,
                    audio_id=normalized["audio_id"],
                    text=normalized["text"],
                    start_time=start,
                    end_time=end,
                    style_preset=normalized["style_preset"],
                    speaker=normalized["speaker"],
                    shot_id=normalized["shot_id"],
                    revision=revision,
                    video_duration_snapshot=effective_duration,
                    provenance={
                        "source_signature": source_signature,
                        "duration_source": selected_kind,
                    },
                )
            except (ValidationError, ValueError, TypeError) as exc:
                diagnostics.append(
                    _diagnostic(
                        "invalid_subtitle_line",
                        f"字幕行无效：{exc}",
                        path=path,
                    )
                )
            else:
                lines.append(line)
                cursor = max(cursor, end)

    _raise_blocking("字幕时间轴构建被阻断", diagnostics)

    warnings = _overlap_warnings(lines)
    provenance = {
        "kind": "relative_shot_timeline",
        "source_signature": source_signature,
        "duration_source": duration_source,
        "effective_duration_sec": effective_duration,
        "overlap_policy": "preserve_with_warning",
        "warnings": copy.deepcopy(warnings),
    }
    timeline = SubtitleTimeline(
        lines=lines,
        revision=revision,
        video_duration_snapshot=effective_duration,
        provenance=provenance,
        source_signature=source_signature,
        status="warning" if warnings else "ready",
        warnings=warnings,
    )
    return _timeline_with_signature(timeline)


def _duration_map_value(value: Any) -> float | None:
    return _duration_from_value(value)


def _actual_duration_lookup(
    durations: Any,
    *,
    shot_id: str | None,
    shot_order: int | float | None,
    index: int,
) -> tuple[float | None, bool, str | None]:
    """返回 (时长, 是否找到, 取值来源)。"""

    if isinstance(durations, Mapping):
        candidates: list[Any] = []
        for candidate in (
            shot_id,
            shot_order,
            index,
            str(shot_id) if shot_id else None,
            str(shot_order) if shot_order is not None else None,
            str(index),
        ):
            if candidate is not None and candidate not in candidates:
                candidates.append(candidate)
        for candidate in candidates:
            if candidate in durations:
                value = durations[candidate]
                duration = _duration_map_value(value)
                # None 表示没有实际时长，允许按要求退回 shot duration；其它无效值
                # 则保持 found=True，由上层作为 blocking 诊断报告。
                return duration, value is not None, str(candidate)
        return None, False, None

    if isinstance(durations, Sequence) and not isinstance(durations, str | bytes):
        if index < len(durations):
            value = durations[index]
            return _duration_map_value(value), value is not None, str(index)
    return None, False, None


def _effective_crossfade_seconds(durations: Sequence[float], crossfade_sec: float) -> float:
    """复刻 CompositionService 的 xfade 有效时长规则；0 明确表示不叠化。"""

    if len(durations) < 2 or crossfade_sec <= 0:
        return 0.0
    return max(0.1, min(float(crossfade_sec), min(durations) / 2))


def _audio_offsets(durations: Sequence[float], crossfade_sec: float) -> list[float]:
    fade = _effective_crossfade_seconds(durations, crossfade_sec)
    offsets: list[float] = []
    cursor = 0.0
    for index, duration in enumerate(durations):
        offsets.append(cursor)
        cursor += duration - (fade if index < len(durations) - 1 else 0.0)
    return offsets


def _coerce_subtitle_line(
    raw: Any,
    *,
    path: str,
) -> tuple[SubtitleLine | None, list[dict[str, Any]]]:
    if isinstance(raw, SubtitleLine):
        return raw.model_copy(deep=True), []
    normalized, diagnostics = _normalize_dialogue_line(raw)
    if normalized is None:
        return None, diagnostics

    start = _number(normalized["start_raw"])
    end = _number(normalized["end_raw"])
    if start is None or end is None:
        return None, [
            _diagnostic(
                "missing_time_range",
                "绝对时间轴中的字幕行必须已有 start_time 和 end_time",
                path=path,
            )
        ]
    try:
        return (
            SubtitleLine(
                dialogue_line_id=normalized["dialogue_line_id"],
                audio_id=normalized["audio_id"],
                text=normalized["text"],
                start_time=start,
                end_time=end,
                style_preset=normalized["style_preset"],
                speaker=normalized["speaker"],
                shot_id=normalized["shot_id"],
                provenance={},
            ),
            [],
        )
    except (ValidationError, ValueError, TypeError) as exc:
        return None, [_diagnostic("invalid_subtitle_line", f"字幕行无效：{exc}", path=path)]


def _shot_mapping(raw: Any) -> tuple[Mapping[str, Any] | None, Any | None]:
    """拆解普通 shot、(shot, timeline) 元组和 timeline 本身。"""

    if isinstance(raw, tuple) and len(raw) == 2:
        left, right = raw
        mapping = _mapping(left)
        if mapping is None:
            return None, right
        payload = dict(mapping)
        payload["timeline"] = right
        return payload, right
    if isinstance(raw, SubtitleTimeline):
        return raw.model_dump(mode="python"), raw
    mapping = _mapping(raw)
    return mapping, None


def _extract_shot_lines(
    source: Mapping[str, Any],
    embedded_timeline: Any | None,
) -> tuple[list[Any], SubtitleTimeline | None, list[dict[str, Any]]]:
    diagnostics: list[dict[str, Any]] = []
    timeline_candidate = embedded_timeline
    if timeline_candidate is None:
        timeline_candidate = _first(
            source,
            "subtitle_timeline",
            "subtitleTimeline",
            "timeline",
            default=_MISSING,
        )

    timeline: SubtitleTimeline | None = None
    if timeline_candidate is not _MISSING and timeline_candidate is not None:
        if isinstance(timeline_candidate, SubtitleTimeline):
            timeline = timeline_candidate.model_copy(deep=True)
        else:
            try:
                timeline = SubtitleTimeline.model_validate(timeline_candidate)
            except (ValidationError, TypeError, ValueError) as exc:
                diagnostics.append(
                    _diagnostic("invalid_subtitle_timeline", f"分镜字幕时间轴无效：{exc}")
                )

    if timeline is not None:
        return list(timeline.lines), timeline, diagnostics

    line_candidate = _first(
        source,
        "subtitle_lines",
        "subtitleLines",
        "subtitles",
        "lines",
        "dialogue",
        default=_MISSING,
    )
    if line_candidate is _MISSING or line_candidate is None:
        return [], None, diagnostics
    if isinstance(line_candidate, Sequence) and not isinstance(line_candidate, str | bytes):
        return list(line_candidate), None, diagnostics
    return [line_candidate], None, diagnostics


def _shot_order_value(source: Mapping[str, Any], index: int) -> tuple[int, float]:
    raw = _first(source, "shot_order", "order", "ordinal", "shot_no", default=_MISSING)
    number = _number(raw)
    return (0, number) if number is not None else (1, float(index))


def build_absolute_timeline(
    shots: Iterable[Any],
    *,
    actual_video_durations: Mapping[Any, Any] | Sequence[Any],
    crossfade_sec: float = 0,
) -> SubtitleTimeline:
    """把各分镜的相对字幕转换为成片绝对时间轴。

    有真实视频时长就使用真实值；缺失时退回 shot duration，并在返回时间轴的
    ``warnings``/``provenance`` 中保留 warning。叠化 offset 与
    ``CompositionService._audio_offsets`` 等价，函数本身不修改输入。
    """

    diagnostics: list[dict[str, Any]] = []
    warnings: list[str] = []
    fade_input = _number(crossfade_sec)
    if fade_input is None or fade_input < 0:
        diagnostics.append(
            _diagnostic("invalid_crossfade", "crossfade_sec 必须是大于等于 0 的有限数值")
        )
        fade_input = 0.0

    try:
        raw_shots = list(shots)
    except TypeError:
        raw_shots = []
        diagnostics.append(_diagnostic("invalid_shots", "shots 必须是可迭代对象"))

    # 允许传入 {shot_id: shot_payload}，同时不改变 list 输入顺序语义。
    if isinstance(shots, Mapping):
        raw_shots = []
        for key, value in shots.items():
            value_mapping = _mapping(value)
            if value_mapping is None:
                raw_shots.append({"shot_id": str(key), "duration_sec": value})
            else:
                payload = dict(value_mapping)
                payload.setdefault("shot_id", str(key))
                raw_shots.append(payload)

    records: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_shots):
        source, embedded_timeline = _shot_mapping(raw)
        if source is None:
            diagnostics.append(
                _diagnostic("invalid_shot", "shot 必须是对象", path=f"shots[{index}]")
            )
            continue

        shot_id = (
            _clean_text(
                _first(source, "shot_id", "shotId", "id", "external_key", "key", default="")
            )
            or None
        )
        order_value = _shot_order_value(source, index)
        order_number = order_value[1]
        line_raws, timeline, line_diagnostics = _extract_shot_lines(source, embedded_timeline)
        diagnostics.extend(
            _diagnostic(
                item["code"],
                item["message"],
                path=item.get("path", f"shots[{index}]"),
                details=item.get("details"),
                level=item.get("level", "blocking"),
            )
            for item in line_diagnostics
        )

        planned_raw = _first(
            source,
            "duration_sec",
            "shot_duration_sec",
            "duration",
            "planned_duration_sec",
            default=_MISSING,
        )
        planned_duration = _duration_from_value(planned_raw)
        if planned_duration is None and timeline is not None:
            planned_duration = timeline.video_duration_snapshot
        if planned_duration is None:
            # actual duration 已知时不需要计划时长；否则在后面给出明确 blocking。
            planned_duration = None

        wrapper_actual = _first(
            source,
            "actual_duration_sec",
            "actual_video_duration_sec",
            "actual_duration",
            default=_MISSING,
        )
        records.append(
            {
                "index": index,
                "source": source,
                "shot_id": shot_id,
                "order": order_number,
                "order_key": order_value,
                "line_raws": line_raws,
                "timeline": timeline,
                "planned_duration": planned_duration,
                "wrapper_actual": wrapper_actual,
            }
        )

    records.sort(key=lambda item: item["order_key"])

    durations: list[float] = []
    for record in records:
        actual, found, actual_source = _actual_duration_lookup(
            actual_video_durations,
            shot_id=record["shot_id"],
            shot_order=record["order"],
            index=record["index"],
        )
        if not found and record["wrapper_actual"] is not _MISSING:
            actual = _duration_from_value(record["wrapper_actual"])
            found = record["wrapper_actual"] is not None
            actual_source = "shot.actual_duration_sec"

        if found and actual is None:
            diagnostics.append(
                _diagnostic(
                    "invalid_actual_video_duration",
                    "actual video duration 必须是大于 0 的有限数值",
                    path=f"shots[{record['index']}]",
                )
            )
            continue

        if actual is None:
            actual = record["planned_duration"]
            if actual is None or actual <= 0 or not math.isfinite(actual):
                diagnostics.append(
                    _diagnostic(
                        "missing_shot_duration",
                        "缺少 actual video duration，且没有可用的 shot duration",
                        path=f"shots[{record['index']}]",
                    )
                )
                continue
            warning = (
                f"shot {record['shot_id'] or record['index']} 缺少实际视频时长，"
                "已使用 shot duration"
            )
            warnings.append(warning)
            actual_source = "shot_duration_sec"

        if actual <= 0 or not math.isfinite(actual):
            diagnostics.append(
                _diagnostic(
                    "invalid_video_duration",
                    "视频时长必须是大于 0 的有限数值",
                    path=f"shots[{record['index']}]",
                )
            )
            continue

        record["actual_duration"] = actual
        record["actual_source"] = actual_source or "actual_video_durations"
        durations.append(actual)

    if len(durations) != len(records):
        _raise_blocking("绝对字幕时间轴构建被阻断", diagnostics)

    fade = _effective_crossfade_seconds(durations, fade_input)
    offsets = _audio_offsets(durations, fade_input)
    absolute_lines: list[SubtitleLine] = []
    seen_ids: set[str] = set()

    for record, offset in zip(records, offsets, strict=True):
        shot_duration = record["actual_duration"]
        shot_id = record["shot_id"]
        timeline = record["timeline"]
        line_revision = timeline.revision if timeline is not None else 1
        source_signature = (
            timeline.source_signature
            if timeline is not None and timeline.source_signature
            else _clean_text(record["source"].get("source_signature", ""))
        )

        for line_index, raw_line in enumerate(record["line_raws"]):
            line, line_diagnostics = _coerce_subtitle_line(
                raw_line,
                path=f"shots[{record['index']}].lines[{line_index}]",
            )
            diagnostics.extend(line_diagnostics)
            if line is None:
                continue
            if not line.dialogue_line_id:
                diagnostics.append(
                    _diagnostic(
                        "missing_dialogue_line_id",
                        "字幕行缺少 dialogue_line_id",
                        path=f"shots[{record['index']}].lines[{line_index}]",
                    )
                )
                continue
            if line.dialogue_line_id in seen_ids:
                diagnostics.append(
                    _diagnostic(
                        "duplicate_dialogue_line_id",
                        f"重复 dialogue_line_id：{line.dialogue_line_id}",
                        path=f"shots[{record['index']}].lines[{line_index}]",
                    )
                )
                continue
            seen_ids.add(line.dialogue_line_id)

            if line.end_time > shot_duration + _EPSILON:
                diagnostics.append(
                    _diagnostic(
                        "subtitle_out_of_bounds",
                        f"字幕 end_time {line.end_time:g} 超出分镜实际时长 {shot_duration:g}",
                        path=f"shots[{record['index']}].lines[{line_index}]",
                        details={
                            "end_time": line.end_time,
                            "actual_video_duration": shot_duration,
                        },
                    )
                )
                continue

            payload = line.model_dump(mode="python")
            payload.update(
                {
                    "start_time": offset + line.start_time,
                    "end_time": offset + line.end_time,
                    "shot_id": shot_id or line.shot_id,
                    "revision": line_revision,
                    "video_duration_snapshot": shot_duration,
                    "provenance": {
                        **copy.deepcopy(line.provenance),
                        "source_signature": source_signature,
                        "shot_offset_sec": offset,
                        "actual_video_duration_sec": shot_duration,
                        "actual_duration_source": record["actual_source"],
                    },
                }
            )
            try:
                absolute_lines.append(SubtitleLine.model_validate(payload))
            except (ValidationError, ValueError, TypeError) as exc:
                diagnostics.append(
                    _diagnostic(
                        "invalid_absolute_subtitle_line",
                        f"绝对字幕行无效：{exc}",
                        path=f"shots[{record['index']}].lines[{line_index}]",
                    )
                )

    _raise_blocking("绝对字幕时间轴构建被阻断", diagnostics)

    total_duration = offsets[-1] + durations[-1] if durations else 0.0
    shot_identifiers = [record["shot_id"] or str(record["index"]) for record in records]
    source_parts = [
        record["timeline"].source_signature
        for record in records
        if record["timeline"] is not None and record["timeline"].source_signature
    ]
    absolute_source_signature = hashlib.sha256(
        json.dumps(
            {
                "sources": source_parts,
                "shots": shot_identifiers,
                "durations": durations,
                "crossfade_sec": fade,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    warnings.extend(_overlap_warnings(absolute_lines))
    warning_diagnostics = [
        _diagnostic(
            "missing_actual_video_duration",
            warning,
            level="warning",
        )
        for warning in warnings
    ]
    provenance = {
        "kind": "absolute_composition_timeline",
        "shot_order": shot_identifiers,
        "shot_offsets_sec": {
            identifier: offset for identifier, offset in zip(shot_identifiers, offsets, strict=True)
        },
        "actual_video_durations_sec": {
            identifier: duration
            for identifier, duration in zip(shot_identifiers, durations, strict=True)
        },
        "crossfade_sec": fade_input,
        "effective_crossfade_sec": fade,
        "warnings": copy.deepcopy(warnings),
        "diagnostics": copy.deepcopy(warning_diagnostics),
    }
    timeline = SubtitleTimeline(
        lines=absolute_lines,
        revision=max(
            (record["timeline"].revision for record in records if record["timeline"]),
            default=1,
        ),
        video_duration_snapshot=total_duration if total_duration > 0 else None,
        provenance=provenance,
        source_signature=absolute_source_signature,
        status="warning" if warnings else "ready",
        warnings=warnings,
        diagnostics=warning_diagnostics,
    )
    return _timeline_with_signature(timeline)


def _coerce_correction(
    raw: Any,
    *,
    path: str,
) -> tuple[SubtitleCorrection | None, list[dict[str, Any]]]:
    if isinstance(raw, SubtitleCorrection):
        return raw.model_copy(deep=True), []
    source = _mapping(raw)
    if source is None:
        return None, [_diagnostic("invalid_correction", "校正必须是对象", path=path)]

    payload = {
        "dialogue_line_id": _clean_text(
            _first(
                source,
                "dialogue_line_id",
                "line_id",
                "lineId",
                "id",
                default="",
            )
        ),
        "text": _first(source, "text", "content", "line", default=None),
        "start_time": _first(source, "start_time", "start_sec", "start", default=None),
        "end_time": _first(source, "end_time", "end_sec", "end", default=None),
        "style_preset": _first(source, "style_preset", "style", default=None),
    }
    if payload["text"] is not None:
        payload["text"] = _clean_text(payload["text"])
    for key in ("start_time", "end_time"):
        if payload[key] is not None:
            payload[key] = _number(payload[key])
    if payload["style_preset"] is not None:
        payload["style_preset"] = _clean_text(payload["style_preset"])

    try:
        return SubtitleCorrection.model_validate(payload), []
    except (ValidationError, ValueError, TypeError) as exc:
        return None, [_diagnostic("invalid_correction", f"校正无效：{exc}", path=path)]


def _correction_items(corrections: Any) -> list[Any]:
    if corrections is None:
        return []
    if isinstance(corrections, Mapping):
        correction_keys = {
            "dialogue_line_id",
            "line_id",
            "lineId",
            "id",
            "text",
            "content",
            "start_time",
            "end_time",
            "start",
            "end",
            "style_preset",
            "style",
        }
        if correction_keys.intersection(corrections):
            return [corrections]
        items: list[Any] = []
        for line_id, value in corrections.items():
            if isinstance(value, Mapping):
                payload = dict(value)
                payload.setdefault("dialogue_line_id", line_id)
                items.append(payload)
            else:
                items.append({"dialogue_line_id": line_id, "text": value})
        return items
    if isinstance(corrections, Sequence) and not isinstance(corrections, str | bytes):
        return list(corrections)
    return [corrections]


def apply_subtitle_correction(
    timeline: SubtitleTimeline,
    corrections: Iterable[Any] | Mapping[str, Any],
) -> SubtitleTimeline:
    """应用人工校正并返回新 revision；timeline 与 corrections 均不被修改。"""

    diagnostics: list[dict[str, Any]] = []
    try:
        original = (
            timeline.model_copy(deep=True)
            if isinstance(timeline, SubtitleTimeline)
            else SubtitleTimeline.model_validate(timeline)
        )
    except (ValidationError, ValueError, TypeError) as exc:
        raise SubtitleTimelineError(f"输入字幕时间轴无效：{exc}") from exc

    items = _correction_items(corrections)
    if not items:
        return original.model_copy(deep=True)

    parsed: list[SubtitleCorrection] = []
    seen_corrections: set[str] = set()
    for index, raw in enumerate(items):
        correction, item_diagnostics = _coerce_correction(
            raw,
            path=f"corrections[{index}]",
        )
        diagnostics.extend(item_diagnostics)
        if correction is None:
            continue
        if correction.dialogue_line_id in seen_corrections:
            diagnostics.append(
                _diagnostic(
                    "duplicate_correction",
                    f"同一批次重复校正 dialogue_line_id：{correction.dialogue_line_id}",
                    path=f"corrections[{index}]",
                )
            )
        seen_corrections.add(correction.dialogue_line_id)
        parsed.append(correction)

    line_by_id = {line.dialogue_line_id: line for line in original.lines}
    for index, correction in enumerate(parsed):
        if correction.dialogue_line_id not in line_by_id:
            diagnostics.append(
                _diagnostic(
                    "dialogue_line_not_found",
                    f"找不到待校正字幕行：{correction.dialogue_line_id}",
                    path=f"corrections[{index}]",
                )
            )

    effective_duration = original.video_duration_snapshot
    if effective_duration is None:
        effective_duration = _number(original.provenance.get("effective_duration_sec"))

    updated_lines: list[SubtitleLine] = []
    applied: list[dict[str, Any]] = []
    parsed_by_id = {item.dialogue_line_id: item for item in parsed}
    for line in original.lines:
        correction = parsed_by_id.get(line.dialogue_line_id)
        if correction is None:
            updated_lines.append(line.model_copy(deep=True))
            continue

        start = line.start_time if correction.start_time is None else correction.start_time
        end = line.end_time if correction.end_time is None else correction.end_time
        if start < 0:
            diagnostics.append(
                _diagnostic(
                    "negative_start_time",
                    "校正后的 start_time 不能小于 0",
                    path=f"dialogue_line_id={line.dialogue_line_id}",
                )
            )
        if end <= start:
            diagnostics.append(
                _diagnostic(
                    "invalid_time_range",
                    "校正后的 end_time 必须大于 start_time",
                    path=f"dialogue_line_id={line.dialogue_line_id}",
                )
            )
        if effective_duration is not None and end > effective_duration + _EPSILON:
            diagnostics.append(
                _diagnostic(
                    "subtitle_out_of_bounds",
                    f"校正后的 end_time {end:g} 超出有效时长 {effective_duration:g}",
                    path=f"dialogue_line_id={line.dialogue_line_id}",
                    details={"end_time": end, "effective_duration": effective_duration},
                )
            )

        payload = line.model_dump(mode="python")
        payload.update(
            {
                "text": line.text if correction.text is None else correction.text,
                "start_time": start,
                "end_time": end,
                "style_preset": (
                    line.style_preset
                    if correction.style_preset is None
                    else correction.style_preset
                ),
                "revision": original.revision + 1,
            }
        )
        try:
            updated_line = SubtitleLine.model_validate(payload)
        except (ValidationError, ValueError, TypeError) as exc:
            diagnostics.append(
                _diagnostic(
                    "invalid_corrected_line",
                    f"校正后的字幕行无效：{exc}",
                    path=f"dialogue_line_id={line.dialogue_line_id}",
                )
            )
        else:
            updated_lines.append(updated_line)
            applied.append(
                {
                    "dialogue_line_id": correction.dialogue_line_id,
                    "text": correction.text,
                    "start_time": correction.start_time,
                    "end_time": correction.end_time,
                    "style_preset": correction.style_preset,
                }
            )

    _raise_blocking("字幕人工校正被阻断", diagnostics)

    revision = original.revision + 1
    warnings = _overlap_warnings(updated_lines)
    provenance = copy.deepcopy(original.provenance)
    provenance.update(
        {
            "corrected_from_revision": original.revision,
            "corrections": applied,
            "overlap_policy": "preserve_with_warning",
            "warnings": copy.deepcopy(warnings),
        }
    )
    corrected = SubtitleTimeline(
        lines=updated_lines,
        revision=revision,
        video_duration_snapshot=original.video_duration_snapshot,
        provenance=provenance,
        source_signature=original.source_signature,
        shot_id=original.shot_id,
        status="warning" if warnings else "ready",
        warnings=warnings,
    )
    return _timeline_with_signature(corrected)


def _srt_timestamp(seconds: float) -> str:
    if not math.isfinite(seconds) or seconds < 0:
        raise SubtitleTimelineError(
            "SRT 时间戳无效",
            diagnostics=[_diagnostic("invalid_srt_time", "SRT 时间必须是大于等于 0 的有限数值")],
        )
    try:
        milliseconds = int(
            (Decimal(str(seconds)) * Decimal("1000")).quantize(
                Decimal("1"),
                rounding=ROUND_HALF_UP,
            )
        )
    except (InvalidOperation, ValueError) as exc:
        raise SubtitleTimelineError("SRT 时间戳无法格式化") from exc

    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds_part, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds_part:02d},{millis:03d}"


def render_srt(lines: Iterable[Any] | SubtitleTimeline) -> str:
    """将字幕行稳定排序并渲染为 SRT 文本，不创建或写入文件。"""

    if isinstance(lines, SubtitleTimeline):
        raw_lines: list[Any] = list(lines.lines)
    elif isinstance(lines, SubtitleLine):
        raw_lines = [lines]
    else:
        try:
            raw_lines = list(lines)
        except TypeError as exc:
            raise SubtitleTimelineError("render_srt 的 lines 必须是可迭代对象") from exc

    normalized: list[SubtitleLine] = []
    diagnostics: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_lines):
        line, line_diagnostics = _coerce_subtitle_line(raw, path=f"lines[{index}]")
        diagnostics.extend(line_diagnostics)
        if line is not None:
            normalized.append(line)
    _raise_blocking("SRT 渲染被阻断", diagnostics)

    ordered = sorted(enumerate(normalized), key=lambda item: (item[1].start_time, item[0]))
    blocks: list[str] = []
    for index, (_, line) in enumerate(ordered, start=1):
        blocks.append(
            "\n".join(
                (
                    str(index),
                    f"{_srt_timestamp(line.start_time)} --> {_srt_timestamp(line.end_time)}",
                    line.text,
                )
            )
        )
    return "\n\n".join(blocks)


__all__ = [
    "SubtitleCorrection",
    "SubtitleLine",
    "SubtitleTimeline",
    "SubtitleTimelineError",
    "apply_subtitle_correction",
    "build_absolute_timeline",
    "build_subtitle_lines",
    "render_srt",
]
