from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ValidationError

from app.dynamic_pipeline.schemas import (
    ArtifactMeta,
    Diagnostic,
    RhythmOutput,
    ScriptOutput,
    ScriptScene,
    ScriptSoundCue,
)

_MISSING = object()
_TIME_TOLERANCE = 0.01
_CJK_OR_WORD_RE = re.compile(r"[\u3400-\u9fff]|[A-Za-z0-9_]+")


class Stage3ValidationError(ValueError):
    """阶段 3 归一化无法保持稳定标识时抛出的错误。"""

    def __init__(
        self,
        message: str,
        *,
        code: str = "stage3_validation_error",
        path: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.path = path
        self.details = dict(details) if details is not None else {}
        super().__init__(message)


def _plain(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python")
    return value


def _root_mapping(raw: Any) -> dict[str, Any]:
    value = _plain(raw)
    if isinstance(value, str):
        try:
            value = json.loads(value.strip())
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("阶段 3 输出必须是对象或对象 JSON") from exc
    if not isinstance(value, Mapping):
        raise TypeError("阶段 3 输出必须是对象")

    # Provider 有时会把阶段产物包在 content/data/output 中；只在包裹值确实是对象时解包。
    for envelope_key in ("content", "data", "output"):
        nested = value.get(envelope_key, _MISSING)
        if isinstance(nested, BaseModel):
            nested = nested.model_dump(mode="python")
        if isinstance(nested, Mapping):
            return dict(nested)
    return dict(value)


def _pick(mapping: Mapping[str, Any], *names: str, default: Any = _MISSING) -> Any:
    first = _MISSING
    for name in names:
        if name not in mapping:
            continue
        value = mapping[name]
        if first is _MISSING:
            first = value
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, list | tuple | set | frozenset | dict) and not value:
            continue
        return value
    if first is not _MISSING:
        return first
    return default


def _strip(value: Any, default: Any = "") -> Any:
    if value is _MISSING or value is None:
        return default
    return value.strip() if isinstance(value, str) else value


def _number(value: Any, default: Any = None) -> Any:
    value = _strip(value, default)
    if value is default or value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value
    if isinstance(value, str):
        try:
            parsed = float(value)
        except ValueError:
            return value
        return int(parsed) if parsed.is_integer() else parsed
    return value


def _integer(value: Any, default: Any = None) -> Any:
    value = _strip(value, default)
    if value is default or value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            try:
                parsed = float(value)
            except ValueError:
                return value
            return int(parsed) if parsed.is_integer() else value
    return value


def _as_list(value: Any) -> list[Any]:
    if value is _MISSING or value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple | set | frozenset):
        return list(value) if not isinstance(value, set | frozenset) else sorted(value, key=repr)
    return [value]


def _string_list(value: Any, *, mapping_keys: tuple[str, ...] = ()) -> list[Any]:
    if value is _MISSING or value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python")
    if isinstance(value, Mapping):
        if mapping_keys:
            value = _pick(value, *mapping_keys, default=value)
        else:
            value = _pick(
                value,
                "name",
                "character",
                "canonical_name",
                "text",
                "description",
                default=value,
            )
        if isinstance(value, Mapping):
            return [value]
        return _string_list(value, mapping_keys=mapping_keys)
    if isinstance(value, tuple | set | frozenset):
        values = list(value) if not isinstance(value, set | frozenset) else sorted(value, key=repr)
    elif isinstance(value, list):
        values = list(value)
    else:
        return [value]
    result = []
    for item in values:
        if isinstance(item, Mapping):
            item = _pick(
                item,
                *(mapping_keys or ("name", "character", "canonical_name", "text")),
                default=item,
            )
        result.append(_strip(item))
    return result


def _merge_refs(local: Any, defaults: Sequence[str]) -> list[Any]:
    local_values = _string_list(local)
    return local_values if local_values else [_strip(item) for item in defaults]


def _mapping(value: Any) -> Mapping[str, Any] | None:
    value = _plain(value)
    return value if isinstance(value, Mapping) else None


def _clean_identifier(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def stable_scene_key(
    scene_id: Any = None,
    ordinal: Any = None,
    scene_heading: Any = None,
    **aliases: Any,
) -> str:
    """返回不依赖时间、随机数或对象地址的场次键。"""
    if isinstance(scene_id, ScriptScene):
        scene = scene_id
        scene_id = scene.scene_id
        ordinal = scene.ordinal if ordinal is None else ordinal
        scene_heading = scene.scene_heading if scene_heading is None else scene_heading
    elif isinstance(scene_id, ScriptOutput):
        raise TypeError("stable_scene_key 需要场次而不是剧本输出")
    elif isinstance(scene_id, Mapping):
        scene = scene_id
        scene_id = _pick(scene, "scene_id", "id", "scene_key", "key", default=None)
        if ordinal is None:
            ordinal = _pick(scene, "ordinal", "scene_no", "no", "index", default=None)
        scene_heading = _pick(
            scene,
            "scene_heading",
            "scene_name",
            "name",
            "heading",
            default=scene_heading,
        )

    if scene_heading is None:
        scene_heading = aliases.get("heading")
    if ordinal is None:
        ordinal = aliases.get("scene_ordinal", aliases.get("scene_no"))
    candidate = _clean_identifier(scene_id)
    if candidate is not None:
        return candidate

    ordinal_value = _integer(ordinal)
    if (
        isinstance(ordinal_value, int)
        and not isinstance(ordinal_value, bool)
        and ordinal_value >= 1
    ):
        return f"scene-{ordinal_value}"

    # 归一化阶段总会提供 ordinal；这个兜底只让独立 helper 仍然保持确定性。
    heading = _clean_identifier(scene_heading)
    if heading is not None:
        return f"scene-{heading}"
    return "scene-1"


def normalize_dialogue_line_id(
    scene_id: str,
    scene_ordinal: int,
    dialogue_index: int,
    value: Any = None,
) -> str:
    """保留合法对白 ID，否则按场次序号和对白序号生成稳定 ID。"""
    explicit = _clean_identifier(value)
    if explicit is not None:
        return explicit
    ordinal = _integer(scene_ordinal)
    index = _integer(dialogue_index)
    ordinal_text = (
        str(ordinal) if isinstance(ordinal, int) and not isinstance(ordinal, bool) else "0"
    )
    index_text = str(index) if isinstance(index, int) and not isinstance(index, bool) else "0"
    return f"scene-{ordinal_text}-dialogue-{index_text}"


def stable_dialogue_ids(
    scene_id: Any,
    scene_ordinal: Any = None,
    dialogues: Sequence[Any] | None = None,
) -> list[str]:
    """为一场戏的对白生成稳定 ID；也接受 ScriptScene/场次映射作为便捷输入。"""
    if isinstance(scene_id, ScriptOutput) and scene_ordinal is None and dialogues is None:
        result: list[str] = []
        for scene in scene_id.scenes:
            result.extend(stable_dialogue_ids(scene, scene.ordinal, scene.dialogue))
        return result

    if isinstance(scene_id, ScriptScene) and dialogues is None:
        scene = scene_id
        scene_id = scene.scene_id
        scene_ordinal = scene.ordinal
        dialogues = scene.dialogue
    elif isinstance(scene_id, Mapping) and dialogues is None:
        scene = scene_id
        scene_id = _pick(scene, "scene_id", "id", "scene_key", "key", default=None)
        scene_ordinal = _pick(scene, "ordinal", "scene_no", "no", "index", default=scene_ordinal)
        dialogues = _pick(
            scene,
            "dialogue",
            "dialogues",
            "dialogue_list",
            "dialogue_lines",
            "lines",
            default=[],
        )

    if dialogues is None and isinstance(scene_ordinal, list | tuple):
        dialogues = scene_ordinal
        scene_ordinal = None
    if dialogues is None:
        dialogues = []
    dialogues = _as_list(dialogues)

    scene_id_text = _clean_identifier(scene_id) or stable_scene_key(scene_id, scene_ordinal)
    ordinal = _integer(scene_ordinal, 1)
    result = []
    seen: set[str] = set()
    for index, item in enumerate(dialogues, start=1):
        mapping = _mapping(item)
        value = (
            _pick(mapping, "dialogue_line_id", "line_id", "id", default=None) if mapping else None
        )
        line_id = normalize_dialogue_line_id(scene_id_text, ordinal, index, value)
        if line_id in seen:
            raise Stage3ValidationError(
                f"重复 dialogue_line_id：{line_id}",
                code="duplicate_dialogue_line_id",
                path=f"dialogue[{index - 1}].dialogue_line_id",
                details={"dialogue_line_id": line_id},
            )
        seen.add(line_id)
        result.append(line_id)
    return result


def _normalize_dialogue(
    value: Any,
    *,
    scene_id: str,
    scene_ordinal: int,
    dialogue_index: int,
    shared_refs: Sequence[str] = (),
) -> dict[str, Any]:
    if isinstance(value, str):
        source: Mapping[str, Any] = {"text": value}
    else:
        source = _mapping(value) or {}
    return {
        "dialogue_line_id": normalize_dialogue_line_id(
            scene_id,
            scene_ordinal,
            dialogue_index,
            _pick(source, "dialogue_line_id", "line_id", "id", default=None),
        ),
        "speaker": _strip(
            _pick(source, "speaker", "character", "character_name", "name", "role", default="")
        ),
        "text": _strip(_pick(source, "text", "content", "line", "dialogue", "speech", default="")),
        "start_sec": _number(
            _pick(source, "start_sec", "start", "start_time", "start_time_sec", default=None)
        ),
        "end_sec": _number(
            _pick(source, "end_sec", "end", "end_time", "end_time_sec", default=None)
        ),
        "lip_sync_level": _strip(
            _pick(
                source,
                "lip_sync_level",
                "lip_sync",
                "lip_sync_grade",
                "mouth_sync",
                default="none",
            )
        ),
        "acting_intent": _strip(
            _pick(
                source,
                "acting_intent",
                "acting",
                "performance",
                "delivery",
                "purpose",
                default="",
            )
        ),
        "source_refs": _string_list(
            _pick(
                source,
                "source_refs",
                "source_ref",
                "references",
                "source_references",
                default=[],
            )
        )
        or [_strip(item) for item in shared_refs],
    }


def _normalize_sound_cue(value: Any) -> str | dict[str, Any]:
    if isinstance(value, str):
        return value.strip()
    source = _mapping(value) or {}
    return {
        "kind": _strip(_pick(source, "kind", "type", "category", default="")),
        "description": _strip(_pick(source, "description", "text", "sound", "cue", default="")),
        "start_sec": _number(
            _pick(source, "start_sec", "start", "start_time", "start_time_sec", default=None)
        ),
        "end_sec": _number(
            _pick(source, "end_sec", "end", "end_time", "end_time_sec", default=None)
        ),
        "source_refs": _string_list(
            _pick(
                source,
                "source_refs",
                "source_ref",
                "references",
                "source_references",
                default=[],
            )
        ),
    }


def _normalize_scene(value: Any, index: int, shared_refs: Sequence[str]) -> dict[str, Any]:
    source = _mapping(value) or {}
    ordinal = _integer(_pick(source, "ordinal", "scene_no", "no", "index", default=index), index)
    scene_id = stable_scene_key(
        _pick(source, "scene_id", "id", "scene_key", "key", default=None),
        ordinal,
    )
    time_weather = _strip(
        _pick(source, "time_weather", "time_and_weather", "time_weather_condition", default=None),
        None,
    )
    if time_weather is None:
        time_value = _strip(_pick(source, "time", "time_of_day", default=None), "")
        weather_value = _strip(_pick(source, "weather", "weather_condition", default=None), "")
        time_weather = "；".join(item for item in (time_value, weather_value) if item)

    heading = _strip(_pick(source, "scene_heading", "scene_name", "name", "heading", default=""))
    location = _strip(_pick(source, "location", "setting", "place", "scene_location", default=""))
    if not heading and location:
        heading = location

    raw_dialogue = _pick(
        source,
        "dialogue",
        "dialogues",
        "dialogue_list",
        "dialogue_lines",
        "lines",
        "dialogue_cues",
        default=[],
    )
    dialogue_values = _as_list(raw_dialogue)
    dialogue = [
        _normalize_dialogue(
            item,
            scene_id=scene_id,
            scene_ordinal=ordinal,
            dialogue_index=dialogue_index,
            shared_refs=shared_refs,
        )
        for dialogue_index, item in enumerate(dialogue_values, start=1)
    ]

    objective = _strip(
        _pick(
            source,
            "objective",
            "narrative_task",
            "task",
            "story_goal",
            "goal",
            "purpose",
            default="",
        )
    )
    action = _strip(
        _pick(
            source,
            "action",
            "plot_event",
            "event",
            "visible_action",
            "visual_action",
            "plot",
            "description",
            default="",
        )
    )
    narration = _string_list(
        _pick(source, "narration", "narrative", "voiceover", "旁白", default=[])
    )
    transition_to_next = _strip(
        _pick(
            source,
            "transition_to_next",
            "transition",
            "next_transition",
            "transition_to",
            "cut_to",
            default="",
        )
    )
    plot_event_ids = _scene_plot_event_ids(
        source,
        scene_id=scene_id,
        objective=objective,
        action=action,
        narration=narration,
    )

    return {
        "scene_id": scene_id,
        "ordinal": ordinal,
        "scene_heading": heading,
        "time_weather": time_weather or "",
        "location": location,
        "characters": _string_list(
            _pick(source, "characters", "character_list", "cast", "character_names", default=[]),
            mapping_keys=("name", "character", "canonical_name"),
        ),
        "objective": objective,
        "action": action,
        "plot_event_ids": plot_event_ids,
        "dialogue": dialogue,
        "narration": narration,
        "emotion": _strip(_pick(source, "emotion", "mood", "feeling", default="")),
        "dynamic_level": _number(
            _pick(
                source,
                "dynamic_level",
                "dynamic",
                "intensity",
                "dynamic_intensity",
                "action_intensity",
                default=0.0,
            ),
            0.0,
        ),
        "sound_cues": [
            _normalize_sound_cue(item)
            for item in _as_list(
                _pick(
                    source,
                    "sound_cues",
                    "sounds",
                    "sound",
                    "sound_effects",
                    "audio",
                    default=[],
                )
            )
        ],
        "subtitle_style": _strip(
            _pick(
                source,
                "subtitle_style",
                "subtitle",
                "subtitle_format",
                "caption_style",
                default="",
            )
        ),
        "duration_sec": _number(
            _pick(
                source,
                "duration_sec",
                "duration",
                "scene_duration",
                "estimated_duration_sec",
                "estimated_duration",
                default=None,
            )
        ),
        "transition_to_next": transition_to_next,
        "source_refs": _merge_refs(
            _pick(
                source,
                "source_refs",
                "source_ref",
                "references",
                "source_references",
                default=[],
            ),
            shared_refs,
        ),
    }


def _event_ids(value: Any) -> list[Any]:
    if value is _MISSING or value is None:
        return []
    if isinstance(value, Mapping):
        nested = _pick(value, "event_ids", "ids", "events", default=[])
        if isinstance(nested, list | tuple | set | frozenset):
            values = list(nested)
        else:
            values = [nested]
    else:
        values = _as_list(value)
    result = []
    for item in values:
        if isinstance(item, Mapping):
            item = _pick(item, "event_id", "id", "key", default=item)
        result.append(_strip(item))
    return result


def _scene_plot_event_ids(
    source: Mapping[str, Any],
    *,
    scene_id: str,
    objective: Any,
    action: Any,
    narration: Sequence[Any],
) -> list[str]:
    explicit = _event_ids(
        _pick(
            source,
            "plot_event_ids",
            "story_event_ids",
            "coverage_event_ids",
            "event_keys",
            default=[],
        )
    )
    result = [item for item in explicit if isinstance(item, str) and item.strip()]
    if _strip(objective):
        result.append(f"{scene_id}:objective")
    if _strip(action):
        result.append(f"{scene_id}:action")
    result.extend(
        f"{scene_id}:narration-{index}"
        for index, item in enumerate(narration, start=1)
        if _strip(item)
    )
    return list(dict.fromkeys(result))


def normalize_script_output(
    raw: Any,
    *,
    artifact_meta: ArtifactMeta | Mapping[str, Any],
    target_duration_sec: float,
    source_refs: Sequence[str] = (),
) -> ScriptOutput:
    """将 Provider 阶段 3 输出归一为严格、可恢复的详细剧本模型。"""
    root = _root_mapping(raw)
    shared_refs = _string_list(source_refs)
    raw_scenes = _pick(root, "scenes", "scene_list", default=[])
    scenes = [
        _normalize_scene(item, index, shared_refs)
        for index, item in enumerate(_as_list(raw_scenes), start=1)
    ]

    scene_ids = [scene["scene_id"] for scene in scenes]
    duplicate_scene_ids = sorted({key for key, count in Counter(scene_ids).items() if count > 1})
    if duplicate_scene_ids:
        raise Stage3ValidationError(
            f"重复 scene_id：{', '.join(duplicate_scene_ids)}",
            code="duplicate_scene_id",
            path="scenes",
            details={"scene_ids": duplicate_scene_ids},
        )

    dialogue_ids: list[str] = []
    for scene in scenes:
        dialogue_ids.extend(item["dialogue_line_id"] for item in scene["dialogue"])
    duplicate_dialogue_ids = sorted(
        {key for key, count in Counter(dialogue_ids).items() if count > 1}
    )
    if duplicate_dialogue_ids:
        raise Stage3ValidationError(
            f"重复 dialogue_line_id：{', '.join(duplicate_dialogue_ids)}",
            code="duplicate_dialogue_line_id",
            path="scenes.dialogue",
            details={"dialogue_line_ids": duplicate_dialogue_ids},
        )

    plot_event_ids = [event_id for scene in scenes for event_id in scene["plot_event_ids"]]
    duplicate_plot_event_ids = sorted(
        {key for key, count in Counter(plot_event_ids).items() if count > 1}
    )
    if duplicate_plot_event_ids:
        raise Stage3ValidationError(
            f"重复 plot_event_id：{', '.join(duplicate_plot_event_ids)}",
            code="duplicate_plot_event_id",
            path="scenes.plot_event_ids",
            details={"plot_event_ids": duplicate_plot_event_ids},
        )

    payload = {
        "artifact_meta": ArtifactMeta.model_validate(artifact_meta),
        "title": _strip(_pick(root, "title", "name", default="")),
        "logline": _strip(
            _pick(
                root,
                "logline",
                "episode_logline",
                "summary",
                "synopsis",
                "one_sentence_summary",
                default="",
            )
        ),
        # 阶段参数是运行配置的真源，Provider 返回的同名字段不能覆盖它。
        "target_duration_sec": target_duration_sec,
        "scenes": scenes,
        "strong_dynamic_event_ids": _event_ids(
            _pick(
                root,
                "strong_dynamic_event_ids",
                "dynamic_event_ids",
                "strong_events",
                "strong_dynamic_events",
                default=[],
            )
        ),
        "assumptions": _string_list(_pick(root, "assumptions", "assumption", default=[])),
        "open_questions": _string_list(
            _pick(root, "open_questions", "questions", "unresolved", default=[])
        ),
    }
    return ScriptOutput.model_validate(payload)


def _diagnostic(
    level: str,
    code: str,
    message: str,
    path: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> Diagnostic:
    return Diagnostic(
        level=level,  # type: ignore[arg-type]
        code=code,
        message=message,
        path=path,
        details=dict(details) if details is not None else {},
    )


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _duration_matches(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=1e-6, abs_tol=_TIME_TOLERANCE)


def _is_legacy_script_version(output: ScriptOutput) -> bool:
    version = output.artifact_meta.schema_version.strip().casefold()
    return version in {"legacy", "legacy-screenplay-v1", "dynamic-script-v1"} or not (
        version.startswith("dynamic-script-v2")
    )


def _sound_cue_text(value: ScriptSoundCue | str) -> str:
    if isinstance(value, ScriptSoundCue):
        return value.description.strip()
    return value.strip()


def _timed_cue_diagnostics(
    *,
    start_sec: float | None,
    end_sec: float | None,
    duration_sec: float,
    path: str,
    cue_kind: str,
    legacy: bool,
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    if start_sec is None or end_sec is None:
        diagnostics.append(
            _diagnostic(
                "warning" if legacy else "blocking",
                f"script_{cue_kind}_time_missing",
                f"{cue_kind} 缺少 start_sec/end_sec 时间绑定",
                path,
                {"legacy": legacy},
            )
        )
        return diagnostics
    if not math.isfinite(start_sec) or not math.isfinite(end_sec) or end_sec <= start_sec:
        diagnostics.append(
            _diagnostic(
                "blocking",
                f"script_{cue_kind}_time_invalid",
                f"{cue_kind} 的时间范围无效",
                path,
                {"start_sec": start_sec, "end_sec": end_sec},
            )
        )
        return diagnostics
    if start_sec < 0 or end_sec > duration_sec:
        diagnostics.append(
            _diagnostic(
                "blocking",
                f"script_{cue_kind}_time_out_of_bounds",
                f"{cue_kind} 时间范围不能超出场景 duration_sec",
                path,
                {
                    "start_sec": start_sec,
                    "end_sec": end_sec,
                    "duration_sec": duration_sec,
                },
            )
        )
    return diagnostics


def _text_values(scene: ScriptScene) -> list[str]:
    values = [
        scene.scene_heading,
        scene.location,
        scene.objective,
        scene.action,
        scene.emotion,
        scene.transition_to_next,
        *scene.narration,
        *(_sound_cue_text(item) for item in scene.sound_cues),
    ]
    values.extend(dialogue.text for dialogue in scene.dialogue)
    return [value.strip() for value in values if isinstance(value, str) and value.strip()]


def _tokens(value: str) -> set[str]:
    return set(_CJK_OR_WORD_RE.findall(value.lower()))


def _text_matches(expected: str, actual_values: Sequence[str]) -> bool:
    expected = expected.strip()
    if not expected:
        return True
    for actual in actual_values:
        actual = actual.strip()
        if expected in actual or actual in expected:
            return True
        expected_tokens = _tokens(expected)
        actual_tokens = _tokens(actual)
        shared = expected_tokens & actual_tokens
        if len(shared) >= 2 and len(shared) / max(len(expected_tokens), 1) >= 0.34:
            return True
    return False


def _script_scene_intervals(
    scenes: Sequence[ScriptScene],
) -> list[tuple[float, float, ScriptScene]]:
    current = 0.0
    result = []
    for scene in scenes:
        end = current + scene.duration_sec
        result.append((current, end, scene))
        current = end
    return result


def _rhythm_scene_candidates(
    segment_start: float,
    segment_end: float,
    intervals: Sequence[tuple[float, float, ScriptScene]],
) -> list[ScriptScene]:
    candidates = [
        scene
        for scene_start, scene_end, scene in intervals
        if min(scene_end, segment_end) - max(scene_start, segment_start) > _TIME_TOLERANCE
    ]
    return candidates or ([intervals[0][2]] if intervals else [])


def _rhythm_from_value(value: RhythmOutput | Mapping[str, Any]) -> RhythmOutput:
    if isinstance(value, RhythmOutput):
        return value
    return RhythmOutput.model_validate(value)


def _rhythm_diagnostics(
    output: ScriptOutput,
    rhythm: RhythmOutput,
    diagnostics: list[Diagnostic],
) -> None:
    scene_total = sum(scene.duration_sec for scene in output.scenes)
    if not _duration_matches(scene_total, rhythm.target_duration_sec):
        diagnostics.append(
            _diagnostic(
                "warning",
                "script_rhythm_duration_mismatch",
                "剧本场次总时长与节奏阶段目标时长不一致",
                "scenes",
                {
                    "scene_total_sec": scene_total,
                    "rhythm_target_duration_sec": rhythm.target_duration_sec,
                },
            )
        )

    timeline = list(rhythm.timeline)
    if timeline:
        timeline_total = timeline[-1].end_sec - timeline[0].start_sec
        if not _duration_matches(scene_total, timeline_total):
            diagnostics.append(
                _diagnostic(
                    "warning",
                    "script_rhythm_timeline_duration_mismatch",
                    "剧本场次总时长与节奏时间轴总时长不一致",
                    "scenes",
                    {"scene_total_sec": scene_total, "rhythm_timeline_total_sec": timeline_total},
                )
            )

        if len(output.scenes) == len(timeline):
            for index, (scene, segment) in enumerate(zip(output.scenes, timeline, strict=True)):
                segment_duration = segment.end_sec - segment.start_sec
                if not _duration_matches(scene.duration_sec, segment_duration):
                    diagnostics.append(
                        _diagnostic(
                            "warning",
                            "script_rhythm_scene_duration_mismatch",
                            "剧本场次时长与对应节奏段时长不一致",
                            f"scenes[{index}].duration_sec",
                            {
                                "scene_duration_sec": scene.duration_sec,
                                "rhythm_segment_duration_sec": segment_duration,
                                "segment_id": segment.segment_id,
                            },
                        )
                    )

    expected_event_ids = set(rhythm.strong_dynamic_budget.event_ids)
    expected_event_ids.update(event.event_id for event in rhythm.strong_dynamic_budget.events)
    expected_event_ids.update(
        segment.strong_dynamic_event_id for segment in timeline if segment.strong_dynamic_event_id
    )
    actual_event_ids = set(output.strong_dynamic_event_ids)
    missing_event_ids = sorted(expected_event_ids - actual_event_ids)
    if missing_event_ids:
        diagnostics.append(
            _diagnostic(
                "blocking",
                "script_rhythm_strong_dynamic_event_id_missing",
                "剧本缺少节奏阶段要求兑现的强动态 event id",
                "strong_dynamic_event_ids",
                {"missing_event_ids": missing_event_ids},
            )
        )

    intervals = _script_scene_intervals(output.scenes)
    for index, segment in enumerate(timeline):
        candidates = _rhythm_scene_candidates(segment.start_sec, segment.end_sec, intervals)
        expected_values = [segment.event_summary, segment.visual_action, segment.narrative_task]
        fulfilled = any(
            _text_matches(expected, values)
            for scene in candidates
            for values in [_text_values(scene)]
            for expected in expected_values
            if expected.strip()
        )
        if not fulfilled:
            diagnostics.append(
                _diagnostic(
                    "blocking",
                    "script_rhythm_event_unfulfilled",
                    "剧本场景未兑现节奏时间轴中的事件",
                    f"rhythm.timeline[{index}]",
                    {
                        "segment_id": segment.segment_id,
                        "event_summary": segment.event_summary,
                        "visual_action": segment.visual_action,
                    },
                )
            )


def validate_script_output(
    output: ScriptOutput | Mapping[str, Any],
    *,
    rhythm: RhythmOutput | Mapping[str, Any] | None = None,
    target_duration_sec: float | None = None,
) -> list[Diagnostic]:
    """返回阶段 3 门禁诊断；该函数只读输入，不做任何 canonical 物化。"""
    diagnostics: list[Diagnostic] = []
    try:
        content = (
            output if isinstance(output, ScriptOutput) else ScriptOutput.model_validate(output)
        )
    except ValidationError as exc:
        return [
            _diagnostic(
                "blocking",
                "script_output_invalid",
                "详细剧本不符合阶段 3 schema",
                None,
                {"errors": exc.errors()},
            )
        ]

    scenes = list(content.scenes)
    if not scenes:
        diagnostics.append(
            _diagnostic("blocking", "script_no_scenes", "详细剧本至少需要一个场次", "scenes")
        )
    scene_ids = [scene.scene_id for scene in scenes]
    duplicate_scene_ids = sorted({key for key, count in Counter(scene_ids).items() if count > 1})
    if duplicate_scene_ids:
        diagnostics.append(
            _diagnostic(
                "blocking",
                "script_duplicate_scene_id",
                "详细剧本存在重复 scene_id",
                "scenes",
                {"scene_ids": duplicate_scene_ids},
            )
        )

    previous_ordinal: int | None = None
    ordinal_counts = Counter(scene.ordinal for scene in scenes)
    duplicate_ordinals = sorted(ordinal for ordinal, count in ordinal_counts.items() if count > 1)
    if duplicate_ordinals:
        diagnostics.append(
            _diagnostic(
                "blocking",
                "script_duplicate_scene_ordinal",
                "详细剧本存在重复场次 ordinal",
                "scenes",
                {"ordinals": duplicate_ordinals},
            )
        )
    for index, scene in enumerate(scenes):
        if previous_ordinal is not None and scene.ordinal <= previous_ordinal:
            diagnostics.append(
                _diagnostic(
                    "blocking",
                    "script_scene_order_invalid",
                    "场次 ordinal 必须按顺序递增",
                    f"scenes[{index}].ordinal",
                    {"previous_ordinal": previous_ordinal, "ordinal": scene.ordinal},
                )
            )
        previous_ordinal = scene.ordinal

    dialogue_ids = [dialogue.dialogue_line_id for scene in scenes for dialogue in scene.dialogue]
    duplicate_dialogue_ids = sorted(
        {key for key, count in Counter(dialogue_ids).items() if count > 1}
    )
    if duplicate_dialogue_ids:
        diagnostics.append(
            _diagnostic(
                "blocking",
                "script_duplicate_dialogue_line_id",
                "详细剧本存在重复 dialogue_line_id",
                "scenes.dialogue",
                {"dialogue_line_ids": duplicate_dialogue_ids},
            )
        )

    legacy_script = _is_legacy_script_version(content)
    for index, scene in enumerate(scenes):
        path = f"scenes[{index}]"
        has_narration = any(item.strip() for item in scene.narration)
        has_sound = any(_sound_cue_text(item) for item in scene.sound_cues)
        if not scene.action.strip() and not has_narration and not has_sound:
            diagnostics.append(
                _diagnostic(
                    "blocking",
                    "script_scene_content_missing",
                    "场次必须至少包含 action、narration 或 sound_cues 之一",
                    path,
                )
            )
        for dialogue_index, dialogue in enumerate(scene.dialogue):
            if _is_blank(dialogue.text):
                diagnostics.append(
                    _diagnostic(
                        "blocking",
                        "script_dialogue_text_missing",
                        "对白缺少 text",
                        f"{path}.dialogue[{dialogue_index}].text",
                    )
                )
            else:
                diagnostics.extend(
                    _timed_cue_diagnostics(
                        start_sec=dialogue.start_sec,
                        end_sec=dialogue.end_sec,
                        duration_sec=scene.duration_sec,
                        path=f"{path}.dialogue[{dialogue_index}]",
                        cue_kind="dialogue",
                        legacy=legacy_script,
                    )
                )
        for sound_index, sound_cue in enumerate(scene.sound_cues):
            sound_path = f"{path}.sound_cues[{sound_index}]"
            if isinstance(sound_cue, str):
                if not sound_cue.strip():
                    diagnostics.append(
                        _diagnostic(
                            "blocking",
                            "script_sound_cue_empty",
                            "sound cue 描述不能为空",
                            sound_path,
                        )
                    )
                elif legacy_script:
                    diagnostics.append(
                        _diagnostic(
                            "warning",
                            "script_legacy_sound_cue_string",
                            "legacy 字符串 sound cue 已降级，缺少结构化时间轴",
                            sound_path,
                        )
                    )
                else:
                    diagnostics.append(
                        _diagnostic(
                            "blocking",
                            "script_sound_cue_structured_required",
                            "dynamic script 版本的 sound cue 必须使用结构化时间对象",
                            sound_path,
                        )
                    )
            else:
                if _is_blank(sound_cue.description):
                    diagnostics.append(
                        _diagnostic(
                            "blocking",
                            "script_sound_cue_description_missing",
                            "sound cue 缺少 description",
                            f"{sound_path}.description",
                        )
                    )
                diagnostics.extend(
                    _timed_cue_diagnostics(
                        start_sec=sound_cue.start_sec,
                        end_sec=sound_cue.end_sec,
                        duration_sec=scene.duration_sec,
                        path=sound_path,
                        cue_kind="sound_cue",
                        legacy=legacy_script,
                    )
                )
        if not math.isfinite(scene.dynamic_level) or not 0 <= scene.dynamic_level <= 1:
            diagnostics.append(
                _diagnostic(
                    "blocking",
                    "script_dynamic_level_out_of_range",
                    "场次 dynamic_level 必须在 0 到 1 之间",
                    f"{path}.dynamic_level",
                    {"dynamic_level": scene.dynamic_level},
                )
            )
        if scene.duration_sec <= 0 or not math.isfinite(scene.duration_sec):
            diagnostics.append(
                _diagnostic(
                    "blocking",
                    "script_scene_duration_invalid",
                    "场次 duration_sec 必须大于 0",
                    f"{path}.duration_sec",
                )
            )

    expected_duration = (
        content.target_duration_sec if target_duration_sec is None else target_duration_sec
    )
    scene_total = sum(scene.duration_sec for scene in scenes)
    if not _duration_matches(scene_total, expected_duration):
        diagnostics.append(
            _diagnostic(
                "blocking",
                "script_duration_mismatch",
                "剧本场次总时长与目标时长不一致；按时长收敛场次预估，避免下游分镜数量失控",
                "scenes",
                {"scene_total_sec": scene_total, "target_duration_sec": expected_duration},
            )
        )

    if rhythm is not None:
        try:
            rhythm_content = _rhythm_from_value(rhythm)
        except ValidationError as exc:
            diagnostics.append(
                _diagnostic(
                    "blocking",
                    "script_rhythm_invalid",
                    "传入的节奏产物不符合 schema",
                    "rhythm",
                    {"errors": exc.errors()},
                )
            )
        else:
            _rhythm_diagnostics(content, rhythm_content, diagnostics)

    return diagnostics


def script_output_to_legacy_screenplay(output: ScriptOutput) -> dict[str, Any]:
    """把阶段 3 产物投影为 legacy screenplay 结构，不写入数据库。"""
    if not isinstance(output, ScriptOutput):
        output = ScriptOutput.model_validate(output)
    return {
        "title": output.title,
        "logline": output.logline,
        "scenes": [
            {
                "scene_no": scene.ordinal,
                "scene_name": scene.scene_heading,
                "time_weather": scene.time_weather,
                "characters": list(scene.characters),
                "objective": scene.objective,
                "action": scene.action,
                "dialogue": [
                    {"speaker": dialogue.speaker, "text": dialogue.text}
                    for dialogue in scene.dialogue
                ],
                "narration": list(scene.narration),
                "emotion": scene.emotion,
                "transition_to_next": scene.transition_to_next,
            }
            for scene in output.scenes
        ],
    }


__all__ = [
    "Stage3ValidationError",
    "normalize_dialogue_line_id",
    "normalize_script_output",
    "script_output_to_legacy_screenplay",
    "stable_dialogue_ids",
    "stable_scene_key",
    "validate_script_output",
]
