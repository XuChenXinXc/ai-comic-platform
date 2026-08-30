"""Phase 2A 阶段产物的纯门禁校验。"""

from __future__ import annotations

import copy
from collections import Counter
from collections.abc import Mapping
from math import isfinite
from typing import Any, TypeVar, cast

from pydantic import BaseModel, ValidationError

from app.services.signature_service import calculate_signature

from .schemas import (
    CharacterBiosOutput,
    Diagnostic,
    DiagnosticLevel,
    RhythmOutput,
    SourceAnalysisOutput,
    StageArtifactEnvelope,
    StageGateResult,
    StrongDynamicEvent,
)

GATE_VERSION = "dynamic-gate-v1"
DEFAULT_STRONG_DYNAMIC_MAX_EVENTS = 3
MAX_STRONG_DYNAMIC_MAX_EVENTS = 12
_TIME_TOLERANCE = 0.01
_MISSING = object()
_ModelT = TypeVar("_ModelT", bound=BaseModel)


class GateInputError(ValueError):
    """门禁输入不是目标阶段的合法 typed 产物。"""


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _has_non_blank(values: Any) -> bool:
    if values is None:
        return False
    if isinstance(values, str):
        return bool(values.strip())
    try:
        return any(not _is_blank(value) for value in values)
    except TypeError:
        return not _is_blank(values)


def _diagnostic(
    level: DiagnosticLevel,
    code: str,
    message: str,
    path: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> Diagnostic:
    return Diagnostic(
        level=level,
        code=code,
        message=message,
        path=path,
        details=dict(details or {}),
    )


def _result(diagnostics: list[Diagnostic], now: str | None) -> StageGateResult:
    ordered = sorted(
        diagnostics,
        key=lambda item: (item.code, item.path or "", item.message),
    )
    has_blocking = any(item.level == "blocking" for item in ordered)
    if has_blocking:
        status = "blocked"
        passed = False
        can_confirm = False
    elif ordered:
        status = "warning"
        passed = True
        can_confirm = True
    else:
        status = "ready"
        passed = True
        can_confirm = True
    return StageGateResult(
        status=status,
        passed=passed,
        diagnostics=ordered,
        checked_at=now,
        can_confirm=can_confirm,
    )


def _validate_now(now: str | None) -> str | None:
    if now is not None and not isinstance(now, str):
        raise GateInputError("now 必须是字符串或 None")
    return now


def _target_number(value: Any, *, required: bool = True) -> float | None:
    if value is None:
        if required:
            raise GateInputError("target_duration_sec 是必填参数")
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise GateInputError("target_duration_sec 必须是数字")
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise GateInputError("target_duration_sec 必须是有限数字") from exc
    if not isfinite(number):
        raise GateInputError("target_duration_sec 必须是有限数字")
    return number


def resolve_strong_dynamic_max_events(
    config_snapshot: Mapping[str, Any] | None = None,
) -> int:
    """读取服务端运行配置的强动态上限，绝不读取模型产物中的 max_events。"""

    if not isinstance(config_snapshot, Mapping):
        return DEFAULT_STRONG_DYNAMIC_MAX_EVENTS

    sources: list[Mapping[str, Any]] = [config_snapshot]
    for key in ("rhythm", "rhythm_profile", "dynamic_budget", "strong_dynamic_budget"):
        value = config_snapshot.get(key)
        if isinstance(value, Mapping):
            sources.append(value)
    keys = (
        "strong_dynamic_max_events",
        "max_strong_dynamic_events",
        "strong_dynamic_budget_max_events",
    )
    for source in sources:
        for key in keys:
            value = source.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, int) and value >= 0:
                return min(value, MAX_STRONG_DYNAMIC_MAX_EVENTS)
            if isinstance(value, str) and value.strip().isdigit():
                return min(int(value.strip()), MAX_STRONG_DYNAMIC_MAX_EVENTS)
    return DEFAULT_STRONG_DYNAMIC_MAX_EVENTS


def server_bound_rhythm_content(
    content: Any,
    config_snapshot: Mapping[str, Any] | None = None,
) -> Any:
    """复制节奏输入并写入服务端预算，调用方可将副本持久化。"""

    if isinstance(content, BaseModel):
        content = content.model_dump(mode="python")
    if not isinstance(content, Mapping):
        return content
    payload = copy.deepcopy(dict(content))
    budget = payload.get("strong_dynamic_budget")
    if isinstance(budget, BaseModel):
        budget = budget.model_dump(mode="python")
    if isinstance(budget, Mapping):
        bounded_budget = dict(budget)
        bounded_budget["max_events"] = resolve_strong_dynamic_max_events(config_snapshot)
        payload["strong_dynamic_budget"] = bounded_budget
        artifact_meta = payload.get("artifact_meta")
        if isinstance(artifact_meta, Mapping):
            canonical_content = {
                key: value for key, value in payload.items() if key != "artifact_meta"
            }
            rebuilt_meta = dict(artifact_meta)
            rebuilt_meta["content_signature"] = calculate_signature(
                canonical_content,
                rebuilt_meta.get("input_signature", ""),
            )
            payload["artifact_meta"] = rebuilt_meta
    return payload


def _small_beats_minimum(
    target_duration_sec: float,
    config_snapshot: Mapping[str, Any] | None,
) -> int:
    """短片保留旧数据兼容，其它时长按 profile 约束小爽点数量。"""

    profile = ""
    if isinstance(config_snapshot, Mapping):
        for key in ("rhythm_profile", "duration_profile", "profile"):
            value = config_snapshot.get(key)
            if isinstance(value, str) and value.strip():
                profile = value.strip().casefold()
                break
    if profile in {"short", "short_30s", "30s", "micro", "短片", "30秒"}:
        return 0
    if target_duration_sec <= 30:
        return 0
    if profile in {"long", "feature", "长片", "长视频"}:
        return max(1, int(target_duration_sec // 30))
    return max(1, int(target_duration_sec // 30))


def _check_declared_stage(value: Any, stage_key: str) -> None:
    if isinstance(value, BaseModel):
        declared = getattr(value, "stage_key", _MISSING)
    elif isinstance(value, Mapping):
        declared = value.get("stage_key", _MISSING)
    else:
        declared = _MISSING
    if declared is not _MISSING and declared is not None and declared != stage_key:
        raise GateInputError(f"阶段产物属于 {declared!r}，不能用于 {stage_key!r} 门禁")


def _extract_content(value: Any, stage_key: str) -> Any:
    current = value
    for _ in range(4):
        _check_declared_stage(current, stage_key)
        if isinstance(current, StageArtifactEnvelope):
            current = current.content
            continue
        if isinstance(current, BaseModel):
            content = getattr(current, "content", _MISSING)
            if content is not _MISSING:
                current = content
                continue
            draft = getattr(current, "draft_output", _MISSING)
            normalized = getattr(current, "normalized_output", _MISSING)
            if draft is not _MISSING and draft not in (None, {}):
                current = draft
                continue
            if normalized is not _MISSING:
                current = normalized
                continue
            break
        if isinstance(current, Mapping):
            if "content" in current:
                current = current["content"]
                continue
            draft = current.get("draft_output", _MISSING)
            normalized = current.get("normalized_output", _MISSING)
            if draft is not _MISSING and draft not in (None, {}):
                current = draft
                continue
            if normalized is not _MISSING:
                current = normalized
                continue
            break
        break

    _check_declared_stage(current, stage_key)
    if isinstance(current, BaseModel):
        return current.model_dump(mode="python")
    if isinstance(current, Mapping):
        return {
            key: item for key, item in current.items() if key not in {"gate_result", "stage_key"}
        }
    return current


def _validate_content(
    model_type: type[_ModelT],
    value: Any,
    stage_key: str,
) -> _ModelT:
    try:
        payload = value.model_dump(mode="python") if isinstance(value, BaseModel) else value
        return model_type.model_validate(payload)
    except (ValidationError, TypeError, ValueError) as exc:
        raise GateInputError(f"阶段 {stage_key} 产物结构校验失败") from exc


def _duplicate_diagnostics(
    values: list[Any],
    *,
    code: str,
    label: str,
    path_prefix: str,
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    first_indexes: dict[Any, int] = {}
    for index, value in enumerate(values):
        if value not in first_indexes:
            first_indexes[value] = index
            continue
        diagnostics.append(
            _diagnostic(
                "blocking",
                code,
                f"{label}重复：{value!r}",
                f"{path_prefix}[{index}]",
                {"first_index": first_indexes[value], "value": value},
            )
        )
    return diagnostics


def _evaluate_source_gate(
    content: SourceAnalysisOutput,
    *,
    target_duration_sec: float | None,
    now: str | None,
) -> StageGateResult:
    diagnostics: list[Diagnostic] = []

    if _is_blank(content.episode_logline):
        diagnostics.append(
            _diagnostic(
                "blocking",
                "source_missing_episode_logline",
                "缺少本集一句话梗概",
                "episode_logline",
            )
        )

    metadata = content.metadata
    if metadata is None:
        diagnostics.append(
            _diagnostic("blocking", "source_missing_metadata", "缺少源文元信息", "metadata")
        )
    else:
        for field in ("title", "chapter_title", "source_signature", "aspect_ratio"):
            if _is_blank(getattr(metadata, field, None)):
                diagnostics.append(
                    _diagnostic(
                        "blocking",
                        f"source_metadata_{field}_empty",
                        f"源文元信息字段 {field} 不能为空",
                        f"metadata.{field}",
                    )
                )

    scenes = list(content.scenes)
    if not scenes:
        diagnostics.append(
            _diagnostic("blocking", "source_no_scenes", "至少需要一个场次", "scenes")
        )
    else:
        diagnostics.extend(
            _duplicate_diagnostics(
                [scene.scene_id for scene in scenes],
                code="source_duplicate_scene_id",
                label="scene_id",
                path_prefix="scenes",
            )
        )
        diagnostics.extend(
            _duplicate_diagnostics(
                [scene.ordinal for scene in scenes],
                code="source_duplicate_scene_ordinal",
                label="场次 ordinal",
                path_prefix="scenes",
            )
        )

        shootable_count = 0
        for index, scene in enumerate(scenes):
            scene_path = f"scenes[{index}]"
            for field in (
                "location",
                "time_weather",
                "narrative_task",
                "plot_event",
                "visual_focus",
            ):
                if _is_blank(getattr(scene, field, None)):
                    diagnostics.append(
                        _diagnostic(
                            "blocking",
                            f"source_scene_{field}_empty",
                            f"场次字段 {field} 不能为空",
                            f"{scene_path}.{field}",
                        )
                    )

            shootability = scene.shootability
            if shootability.status in {"shootable", "needs_adaptation"}:
                shootable_count += 1
            if shootability.status == "unshootable" and _is_blank(shootability.adaptation_note):
                diagnostics.append(
                    _diagnostic(
                        "blocking",
                        "source_unshootable_without_adaptation",
                        "不可拍场次必须提供 adaptation_note",
                        f"{scene_path}.shootability.adaptation_note",
                    )
                )
            if shootability.status == "needs_adaptation":
                diagnostics.append(
                    _diagnostic(
                        "warning",
                        "source_needs_adaptation",
                        "场次需要改编后才能拍摄",
                        f"{scene_path}.shootability",
                    )
                )
            if _has_non_blank(shootability.production_risks):
                diagnostics.append(
                    _diagnostic(
                        "warning",
                        "source_production_risk",
                        "场次存在制作风险",
                        f"{scene_path}.shootability.production_risks",
                        {"risks": list(shootability.production_risks)},
                    )
                )

        if shootable_count == 0:
            diagnostics.append(
                _diagnostic(
                    "blocking",
                    "source_no_shootable_scene",
                    "至少需要一个可拍或需改编的场次",
                    "scenes",
                )
            )

    scene_ids = {scene.scene_id for scene in scenes}
    for index, conflict_turn in enumerate(content.conflict_turns):
        if conflict_turn.scene_id and conflict_turn.scene_id not in scene_ids:
            diagnostics.append(
                _diagnostic(
                    "blocking",
                    "source_conflict_scene_missing",
                    f"冲突/转折引用了不存在的场次：{conflict_turn.scene_id}",
                    f"conflict_turns[{index}].scene_id",
                    {"scene_id": conflict_turn.scene_id},
                )
            )

    diagnostics.extend(
        _duplicate_diagnostics(
            [character.character_key for character in content.character_roster],
            code="source_duplicate_character_key",
            label="character_key",
            path_prefix="character_roster",
        )
    )

    for field in (
        "hard_to_visualize",
        "open_questions",
        "assumptions",
        "continuation_outline",
    ):
        values = getattr(content, field)
        if _has_non_blank(values):
            diagnostics.append(
                _diagnostic(
                    "warning",
                    f"source_{field}",
                    f"源文拆解包含 {field} 内容",
                    field,
                    {"count": len(values)},
                )
            )

    target = target_duration_sec
    if target is None and metadata is not None:
        target = metadata.target_duration_sec
    if target is not None and target > 0 and scenes:
        estimated_total = sum(scene.estimated_duration_sec for scene in scenes)
        difference = estimated_total - target
        if abs(difference) > _TIME_TOLERANCE:
            diagnostics.append(
                _diagnostic(
                    "warning",
                    "source_duration_mismatch",
                    "场次预估时长总和与目标时长存在差异",
                    "scenes",
                    {
                        "estimated_total_sec": estimated_total,
                        "target_duration_sec": target,
                        "difference_sec": difference,
                    },
                )
            )

    return _result(diagnostics, now)


def _evaluate_character_bios_gate(
    content: CharacterBiosOutput,
    *,
    now: str | None,
) -> StageGateResult:
    diagnostics: list[Diagnostic] = []
    characters = list(content.characters)
    if not characters:
        diagnostics.append(
            _diagnostic("blocking", "character_no_characters", "至少需要一个角色", "characters")
        )
    else:
        diagnostics.extend(
            _duplicate_diagnostics(
                [character.basic_info.character_key for character in characters],
                code="character_duplicate_character_key",
                label="character_key",
                path_prefix="characters",
            )
        )

        role_fields = (
            "background",
            "personality_behavior_logic",
            "dynamic_comic_setting",
            "plot_relation",
        )
        visual_fields = ("face", "hair", "body", "costume")
        for index, character in enumerate(characters):
            base_path = f"characters[{index}]"
            if character.basic_info.importance in {"core", "supporting"}:
                for field in role_fields:
                    if _is_blank(getattr(character, field, None)):
                        diagnostics.append(
                            _diagnostic(
                                "blocking",
                                f"character_{field}_empty",
                                f"{character.basic_info.importance} 角色的 {field} 不能为空",
                                f"{base_path}.{field}",
                            )
                        )

            if character.basic_info.importance == "core":
                anchor = character.visual_anchor
                for field in ("immutable_features", "forbidden_changes"):
                    if not _has_non_blank(getattr(anchor, field, None)):
                        diagnostics.append(
                            _diagnostic(
                                "blocking",
                                f"character_core_{field}_empty",
                                f"核心角色必须提供 {field}",
                                f"{base_path}.visual_anchor.{field}",
                            )
                        )
                if _is_blank(anchor.makeup_keyword_string):
                    diagnostics.append(
                        _diagnostic(
                            "blocking",
                            "character_core_makeup_keyword_string_empty",
                            "核心角色必须提供定妆关键词串",
                            f"{base_path}.visual_anchor.makeup_keyword_string",
                        )
                    )
                for field in visual_fields:
                    if _is_blank(getattr(anchor, field, None)):
                        diagnostics.append(
                            _diagnostic(
                                "blocking",
                                f"character_core_visual_{field}_empty",
                                f"核心角色视觉锚点的 {field} 不能为空",
                                f"{base_path}.visual_anchor.{field}",
                            )
                        )
                evidence = character.behavior_evidence
                for field in ("trigger", "action", "expression", "voice"):
                    if _is_blank(getattr(evidence, field, None)):
                        diagnostics.append(
                            _diagnostic(
                                "blocking",
                                f"character_core_behavior_evidence_{field}_empty",
                                f"核心角色必须提供行为证据 {field}",
                                f"{base_path}.behavior_evidence.{field}",
                            )
                        )

            if not _has_non_blank(character.visual_anchor.approved_reference_ids):
                diagnostics.append(
                    _diagnostic(
                        "warning",
                        "character_missing_approved_reference",
                        "角色尚未绑定 approved reference，可继续确认",
                        f"{base_path}.visual_anchor.approved_reference_ids",
                    )
                )

    diagnostics.extend(
        _duplicate_diagnostics(
            [anchor.ensemble_key for anchor in content.ensemble_anchors],
            code="character_duplicate_ensemble_key",
            label="ensemble_key",
            path_prefix="ensemble_anchors",
        )
    )
    if _has_non_blank(content.unresolved_character_refs):
        diagnostics.append(
            _diagnostic(
                "warning",
                "character_unresolved_character_refs",
                "存在未解析的人物引用",
                "unresolved_character_refs",
                {"refs": list(content.unresolved_character_refs)},
            )
        )

    return _result(diagnostics, now)


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if isfinite(number) else None


def _beat_is_blank(value: Any) -> bool:
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, StrongDynamicEvent):
        return _is_blank(value.summary)
    return value is None


def _evaluate_rhythm_gate(
    content: RhythmOutput,
    target_duration_sec: float,
    *,
    config_snapshot: Mapping[str, Any] | None,
    now: str | None,
) -> StageGateResult:
    diagnostics: list[Diagnostic] = []
    if target_duration_sec <= 0:
        diagnostics.append(
            _diagnostic(
                "blocking",
                "rhythm_invalid_target_duration",
                "target_duration_sec 必须大于 0",
                "target_duration_sec",
            )
        )

    output_duration = _finite_number(content.target_duration_sec)
    if output_duration is None or output_duration <= 0:
        diagnostics.append(
            _diagnostic(
                "blocking",
                "rhythm_invalid_output_duration",
                "节奏产物的 target_duration_sec 必须大于 0",
                "target_duration_sec",
            )
        )
    elif target_duration_sec > 0 and abs(output_duration - target_duration_sec) > _TIME_TOLERANCE:
        diagnostics.append(
            _diagnostic(
                "blocking",
                "rhythm_target_duration_mismatch",
                "节奏产物的目标时长与运行目标不一致",
                "target_duration_sec",
                {
                    "output_target_duration_sec": output_duration,
                    "target_duration_sec": target_duration_sec,
                },
            )
        )

    timeline = list(content.timeline)
    if not timeline:
        diagnostics.append(
            _diagnostic("blocking", "rhythm_empty_timeline", "时间轴不能为空", "timeline")
        )

    diagnostics.extend(
        _duplicate_diagnostics(
            [segment.segment_id for segment in timeline],
            code="rhythm_duplicate_segment_id",
            label="segment_id",
            path_prefix="timeline",
        )
    )

    ordered_timeline = sorted(
        enumerate(timeline),
        key=lambda item: (
            item[1].ordinal,
            item[1].start_sec,
            item[1].segment_id,
            item[0],
        ),
    )
    if ordered_timeline:
        first_index, first_segment = ordered_timeline[0]
        first_start = _finite_number(first_segment.start_sec)
        if first_start is None:
            diagnostics.append(
                _diagnostic(
                    "blocking",
                    "rhythm_invalid_segment_time",
                    "时间轴段的时间必须是有限数字",
                    f"timeline[{first_index}].start_sec",
                )
            )
        elif abs(first_start) > _TIME_TOLERANCE:
            diagnostics.append(
                _diagnostic(
                    "blocking",
                    "rhythm_timeline_must_start_at_zero",
                    "时间轴首段必须从 0 秒开始",
                    f"timeline[{first_index}].start_sec",
                    {"actual_start_sec": first_start},
                )
            )

        previous_end: float | None = None
        previous_index: int | None = None
        for original_index, segment in ordered_timeline:
            start = _finite_number(segment.start_sec)
            end = _finite_number(segment.end_sec)
            path = f"timeline[{original_index}]"
            if start is None or end is None:
                diagnostics.append(
                    _diagnostic(
                        "blocking",
                        "rhythm_invalid_segment_time",
                        "时间轴段的时间必须是有限数字",
                        path,
                    )
                )
                previous_end = None
                previous_index = original_index
                continue
            if end <= start:
                diagnostics.append(
                    _diagnostic(
                        "blocking",
                        "rhythm_segment_end_not_after_start",
                        "每个时间轴段的 end_sec 必须大于 start_sec",
                        path,
                    )
                )
            if previous_end is not None:
                delta = start - previous_end
                if delta > _TIME_TOLERANCE:
                    diagnostics.append(
                        _diagnostic(
                            "blocking",
                            "rhythm_timeline_gap",
                            "相邻时间轴段之间存在间隙",
                            f"{path}.start_sec",
                            {"previous_index": previous_index, "gap_sec": delta},
                        )
                    )
                elif delta < -_TIME_TOLERANCE:
                    diagnostics.append(
                        _diagnostic(
                            "blocking",
                            "rhythm_timeline_overlap",
                            "相邻时间轴段之间存在重叠",
                            f"{path}.start_sec",
                            {"previous_index": previous_index, "overlap_sec": -delta},
                        )
                    )
            previous_end = end
            previous_index = original_index

            for field in ("narrative_task", "visual_action"):
                if _is_blank(getattr(segment, field, None)):
                    diagnostics.append(
                        _diagnostic(
                            "blocking",
                            f"rhythm_segment_{field}_empty",
                            f"时间轴段的 {field} 不能为空",
                            f"{path}.{field}",
                        )
                    )

        if previous_end is not None and target_duration_sec > 0:
            if abs(previous_end - target_duration_sec) > _TIME_TOLERANCE:
                diagnostics.append(
                    _diagnostic(
                        "blocking",
                        "rhythm_timeline_end_mismatch",
                        "时间轴末段 end_sec 必须等于目标时长",
                        f"timeline[{previous_index}].end_sec",
                        {
                            "actual_end_sec": previous_end,
                            "target_duration_sec": target_duration_sec,
                        },
                    )
                )

    budget = content.strong_dynamic_budget
    server_max_events = resolve_strong_dynamic_max_events(config_snapshot)
    if budget is None:
        diagnostics.append(
            _diagnostic(
                "blocking",
                "rhythm_missing_strong_dynamic_budget",
                "缺少强动态预算",
                "strong_dynamic_budget",
            )
        )
        events: list[StrongDynamicEvent] = []
        event_ids: list[str] = []
    else:
        events = list(budget.events)
        event_ids = list(budget.event_ids)

    event_model_ids = [event.event_id for event in events]
    if Counter(event_ids) != Counter(event_model_ids):
        diagnostics.append(
            _diagnostic(
                "blocking",
                "rhythm_dynamic_event_ids_inconsistent",
                "强动态预算的 event_ids 必须与 events 一致",
                "strong_dynamic_budget.event_ids",
                {"event_ids": event_ids, "event_model_ids": event_model_ids},
            )
        )
    diagnostics.extend(
        _duplicate_diagnostics(
            event_model_ids,
            code="rhythm_duplicate_dynamic_event_id",
            label="event_id",
            path_prefix="strong_dynamic_budget.events",
        )
    )
    if len(events) > server_max_events:
        diagnostics.append(
            _diagnostic(
                "blocking",
                "rhythm_dynamic_budget_exceeded",
                "强动态事件数量超过预算上限",
                "strong_dynamic_budget.events",
                {
                    "event_count": len(events),
                    "server_max_events": server_max_events,
                },
            )
        )

    segment_ids = {segment.segment_id for segment in timeline}
    event_id_set = set(event_model_ids)
    for index, event in enumerate(events):
        if event.segment_id and event.segment_id not in segment_ids:
            diagnostics.append(
                _diagnostic(
                    "blocking",
                    "rhythm_dynamic_event_segment_missing",
                    f"强动态事件引用了不存在的时间轴段：{event.segment_id}",
                    f"strong_dynamic_budget.events[{index}].segment_id",
                    {"segment_id": event.segment_id},
                )
            )
    for index, segment in enumerate(timeline):
        if segment.strong_dynamic_event_id and segment.strong_dynamic_event_id not in event_id_set:
            diagnostics.append(
                _diagnostic(
                    "blocking",
                    "rhythm_segment_dynamic_event_missing",
                    f"时间轴段引用了不存在的强动态事件：{segment.strong_dynamic_event_id}",
                    f"timeline[{index}].strong_dynamic_event_id",
                    {"event_id": segment.strong_dynamic_event_id},
                )
            )

    if _beat_is_blank(content.core_payoff):
        diagnostics.append(
            _diagnostic("blocking", "rhythm_missing_core_payoff", "核心爽点不能为空", "core_payoff")
        )

    small_beats = list(content.small_beats)
    for index, beat in enumerate(small_beats):
        if _beat_is_blank(beat):
            diagnostics.append(
                _diagnostic(
                    "blocking",
                    "rhythm_small_beat_empty",
                    "小爽点不能是空值",
                    f"small_beats[{index}]",
                )
            )
    minimum_small_beats = _small_beats_minimum(target_duration_sec, config_snapshot)
    if len(small_beats) < minimum_small_beats:
        diagnostics.append(
            _diagnostic(
                "blocking",
                "rhythm_small_beats_insufficient",
                "节奏产物的小爽点数量不足当前时长或 profile 要求",
                "small_beats",
                {
                    "actual_count": len(small_beats),
                    "minimum_count": minimum_small_beats,
                    "target_duration_sec": target_duration_sec,
                },
            )
        )

    for field in ("assumptions", "open_space", "foreshadowing"):
        if not _has_non_blank(getattr(content, field)):
            diagnostics.append(
                _diagnostic(
                    "warning",
                    f"rhythm_empty_{field}",
                    f"节奏产物的 {field} 为空",
                    field,
                )
            )

    for field, suggestions in (
        ("dialogue_suggestions", content.dialogue_suggestions),
        ("sound_suggestions", content.sound_suggestions),
    ):
        for index, suggestion in enumerate(suggestions):
            if suggestion.segment_id and suggestion.segment_id not in segment_ids:
                diagnostics.append(
                    _diagnostic(
                        "warning",
                        f"rhythm_{field.removesuffix('_suggestions')}_segment_missing",
                        f"{field} 引用了不存在的时间轴段：{suggestion.segment_id}",
                        f"{field}[{index}].segment_id",
                        {"segment_id": suggestion.segment_id},
                    )
                )

    dialogue_counts = Counter(
        suggestion.segment_id
        for suggestion in content.dialogue_suggestions
        if suggestion.segment_id
    )
    if len(content.dialogue_suggestions) > len(timeline) or any(
        count > 1 for count in dialogue_counts.values()
    ):
        diagnostics.append(
            _diagnostic(
                "warning",
                "rhythm_dialogue_suggestions_dense",
                "对白建议相对时间轴过密",
                "dialogue_suggestions",
                {
                    "dialogue_count": len(content.dialogue_suggestions),
                    "timeline_count": len(timeline),
                },
            )
        )

    return _result(diagnostics, now)


def evaluate_source_gate(
    content: SourceAnalysisOutput,
    *,
    now: str | None = None,
) -> StageGateResult:
    """校验阶段 0 的源文拆解产物。"""

    model = _validate_content(SourceAnalysisOutput, content, "0-source")
    return _evaluate_source_gate(
        model,
        target_duration_sec=None,
        now=_validate_now(now),
    )


def evaluate_character_bios_gate(
    content: CharacterBiosOutput,
    *,
    now: str | None = None,
) -> StageGateResult:
    """校验阶段 1 的人物小传产物。"""

    model = _validate_content(CharacterBiosOutput, content, "1-character-bios")
    return _evaluate_character_bios_gate(model, now=_validate_now(now))


def evaluate_rhythm_gate(
    content: RhythmOutput,
    target_duration_sec: float,
    *,
    config_snapshot: Mapping[str, Any] | None = None,
    now: str | None = None,
) -> StageGateResult:
    """校验阶段 2 的节奏时间轴和爽点产物。"""

    model = _validate_content(RhythmOutput, content, "2-rhythm")
    target = _target_number(target_duration_sec)
    assert target is not None
    return _evaluate_rhythm_gate(
        cast(RhythmOutput, model),
        target,
        config_snapshot=config_snapshot,
        now=_validate_now(now),
    )


def evaluate_stage_gate(
    stage_key: str,
    content: Any,
    *,
    target_duration_sec: float | None = None,
    config_snapshot: Mapping[str, Any] | None = None,
    now: str | None = None,
) -> StageGateResult:
    """按阶段键重新计算门禁，忽略输入中的客户端 gate_result。"""

    models: dict[str, type[BaseModel]] = {
        "0-source": SourceAnalysisOutput,
        "1-character-bios": CharacterBiosOutput,
        "2-rhythm": RhythmOutput,
    }
    if not isinstance(stage_key, str) or stage_key not in models:
        raise GateInputError(f"不支持的阶段键：{stage_key!r}")
    checked_at = _validate_now(now)
    try:
        extracted = _extract_content(content, stage_key)
    except GateInputError:
        raise
    except (AttributeError, KeyError, OverflowError, TypeError, ValueError) as exc:
        raise GateInputError(f"阶段 {stage_key} 产物输入无效") from exc
    model = _validate_content(models[stage_key], extracted, stage_key)

    if stage_key == "0-source":
        target = _target_number(target_duration_sec, required=False)
        return _evaluate_source_gate(model, target_duration_sec=target, now=checked_at)  # type: ignore[arg-type]
    if stage_key == "1-character-bios":
        return _evaluate_character_bios_gate(model, now=checked_at)  # type: ignore[arg-type]

    target = _target_number(target_duration_sec)
    assert target is not None
    return _evaluate_rhythm_gate(
        cast(RhythmOutput, model),
        target,
        config_snapshot=config_snapshot,
        now=checked_at,
    )


__all__ = [
    "GATE_VERSION",
    "DEFAULT_STRONG_DYNAMIC_MAX_EVENTS",
    "MAX_STRONG_DYNAMIC_MAX_EVENTS",
    "GateInputError",
    "evaluate_character_bios_gate",
    "evaluate_rhythm_gate",
    "evaluate_source_gate",
    "evaluate_stage_gate",
    "resolve_strong_dynamic_max_events",
    "server_bound_rhythm_content",
]
