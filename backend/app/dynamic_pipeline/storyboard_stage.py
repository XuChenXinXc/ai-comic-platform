"""Phase 4 标准分镜的纯模型、归一化、门禁和 legacy 投影函数。"""

from __future__ import annotations

import copy
import json
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from app.dynamic_pipeline.schemas import (
    ArtifactMeta,
    Diagnostic,
    DynamicLevel,
    PipelineModel,
    RhythmOutput,
    ScriptDialogue,
    ScriptOutput,
    ScriptScene,
)

StoryboardTransition = Literal[
    "continuous",
    "elliptical",
    "same_scene_cut",
    "scene_change",
]

StoryboardDialogue = ScriptDialogue | dict[str, Any]

_MISSING = object()
_TIME_TOLERANCE = 0.01
_CJK_OR_WORD_RE = re.compile(r"[\u3400-\u9fff]|[A-Za-z0-9_]+")


class StoryboardShot(PipelineModel):
    """一个可被后续 materializer 无歧义投影的标准分镜。"""

    external_key: str = Field(min_length=1)
    shot_no: int = Field(ge=1)
    ordinal: int = Field(default=1, ge=1)
    scene_id: str = ""
    scene_no: int | None = Field(default=None, ge=1)
    scene_name: str = ""
    time_weather: str = ""
    scene_asset_id: str | None = None
    character_names: list[str] = Field(default_factory=list)
    character_asset_ids: list[str] = Field(default_factory=list)
    prop_names: list[str] = Field(default_factory=list)
    prop_asset_ids: list[str] = Field(default_factory=list)
    positions: list[dict[str, Any]] = Field(default_factory=list)
    expressions: list[dict[str, Any]] = Field(default_factory=list)
    plot_event_ids: list[str] = Field(default_factory=list)
    continuity_state: dict[str, Any] = Field(default_factory=dict)
    start_state: str = ""
    action: str = ""
    end_state: str = ""
    next_hook: str = ""
    transition_from_prev: StoryboardTransition = "scene_change"
    duration_sec: float = Field(gt=0)
    provider_duration_sec: float | None = Field(default=None, gt=0)
    dialogue: list[StoryboardDialogue] = Field(default_factory=list)
    narration: str = ""
    sound_cues: list[Any] = Field(default_factory=list)
    dynamic_level: DynamicLevel = 0.0
    dynamic_event_id: str | None = None
    frame_description: str = ""
    source_refs: list[str] = Field(default_factory=list)
    reference_bindings: list[dict[str, Any]] = Field(default_factory=list)
    prompt_intent: dict[str, Any] = Field(default_factory=dict)

    # 以下字段是现有 Shot 的兼容输入；阶段 4 不生成媒体，也不会擅自填充提示词。
    source_type: str = "screenplay"
    source_ref: str = ""
    shot_type: str = ""
    camera_angle: str = ""
    camera_move: str = ""
    image_prompt: str = ""
    video_prompt: str = ""
    negative_prompt: str = ""


class StoryboardOutput(PipelineModel):
    """阶段 4 的严格标准分镜产物。"""

    artifact_meta: ArtifactMeta
    target_duration_sec: float = Field(gt=0)
    shots: list[StoryboardShot] = Field(default_factory=list)
    scenes: list[dict[str, Any]] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


def _plain(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return copy.deepcopy(value.model_dump(mode="python"))
    return copy.deepcopy(value)


def _root_value(raw: Any) -> Any:
    value = _plain(raw)
    if isinstance(value, str):
        try:
            value = json.loads(value.strip())
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("阶段 4 输出必须是对象、数组或对象 JSON") from exc
    return value


def _root_mapping(raw: Any) -> Mapping[str, Any] | list[Any]:
    value = _root_value(raw)
    if isinstance(value, list):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("阶段 4 输出必须是对象或数组")

    for envelope_key in ("content", "data", "output"):
        nested = value.get(envelope_key, _MISSING)
        if isinstance(nested, Mapping | list):
            return nested
    return value


def _usable(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping | list | tuple | set | frozenset):
        return bool(value)
    return True


def _pick(mapping: Mapping[str, Any], *names: str, default: Any = _MISSING) -> Any:
    first = _MISSING
    for name in names:
        if name not in mapping:
            continue
        value = mapping[name]
        if first is _MISSING:
            first = value
        if _usable(value):
            return value
    if first is not _MISSING:
        return first
    return default


def _pick_from_sources(
    primary: Mapping[str, Any], secondary: Mapping[str, Any], *names: str, default: Any = _MISSING
) -> Any:
    value = _pick(primary, *names, default=_MISSING)
    if value is not _MISSING and _usable(value):
        return value
    value = _pick(secondary, *names, default=_MISSING)
    if value is not _MISSING:
        return value
    return default if value is _MISSING else value


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
        return copy.deepcopy(value)
    if isinstance(value, tuple | set | frozenset):
        values = list(value)
        return copy.deepcopy(
            values if not isinstance(value, set | frozenset) else sorted(values, key=repr)
        )
    return [copy.deepcopy(value)]


def _text(value: Any, default: str = "") -> str:
    value = _plain(value)
    if value is _MISSING or value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, list | tuple):
        return "；".join(item for item in (_text(item) for item in value) if item)
    if isinstance(value, Mapping):
        for key in (
            "text",
            "description",
            "summary",
            "value",
            "content",
            "action",
            "state",
            "name",
        ):
            candidate = value.get(key, _MISSING)
            if _usable(candidate):
                return _text(candidate)
        return "；".join(
            f"{key}：{item_text}" for key, item in value.items() if (item_text := _text(item))
        )
    return str(value).strip()


def _string_list(value: Any, *, item_keys: tuple[str, ...] = ()) -> list[str]:
    if value is _MISSING or value is None:
        return []
    if isinstance(value, Mapping):
        nested = _pick(
            value,
            "names",
            "items",
            "values",
            "list",
            "ids",
            "asset_ids",
            default=_MISSING,
        )
        if nested is not _MISSING and nested is not value:
            return _string_list(nested, item_keys=item_keys)
        candidate = _pick(
            value,
            *(item_keys or ("name", "canonical_name", "character", "character_name", "prop")),
            "text",
            "description",
            default=_MISSING,
        )
        text = _text(candidate) if candidate is not _MISSING else ""
        return [text] if text else []
    result: list[str] = []
    for item in _as_list(value):
        if isinstance(item, Mapping):
            item = _pick(
                item,
                *(item_keys or ("name", "canonical_name", "character", "character_name", "prop")),
                "text",
                "description",
                default=_MISSING,
            )
        text = _text(item)
        if text:
            result.append(text)
    return result


def _dict_list(value: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in _as_list(value):
        item = _plain(item)
        if isinstance(item, Mapping):
            result.append(copy.deepcopy(dict(item)))
        elif item is not None:
            result.append({"value": _text(item)})
    return result


def _normalize_positions(value: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in _dict_list(value):
        normalized = copy.deepcopy(item)
        aliases = {
            "name": "character",
            "speaker": "character",
            "character_name": "character",
            "location": "position",
            "side": "position",
            "placement": "position",
            "direction": "facing",
            "look": "gaze",
            "eye_line": "gaze",
            "movement_direction": "action_direction",
        }
        for alias, target in aliases.items():
            if not _usable(normalized.get(target)) and _usable(normalized.get(alias)):
                normalized[target] = normalized[alias]
        result.append(normalized)
    return result


def _normalize_expressions(value: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in _dict_list(value):
        normalized = copy.deepcopy(item)
        aliases = {
            "name": "character",
            "speaker": "character",
            "character_name": "character",
            "emotion": "expression",
            "mood": "expression",
        }
        for alias, target in aliases.items():
            if not _usable(normalized.get(target)) and _usable(normalized.get(alias)):
                normalized[target] = normalized[alias]
        result.append(normalized)
    return result


def _string_ref_list(value: Any) -> list[str]:
    return _string_list(value, item_keys=("ref", "source_ref", "reference", "value", "name"))


def _entity_lists(
    source: Mapping[str, Any],
    scene_source: Mapping[str, Any],
    *,
    kind: Literal["character", "prop"],
) -> tuple[list[str], list[str]]:
    if kind == "character":
        name_keys = (
            "character_names",
            "characters",
            "character",
            "character_list",
            "cast",
        )
        id_keys = ("character_asset_ids", "character_ids", "character_asset_id_list")
        item_keys = ("name", "canonical_name", "character", "character_name")
    else:
        name_keys = ("prop_names", "props", "prop", "prop_list", "objects", "items")
        id_keys = ("prop_asset_ids", "prop_ids", "prop_asset_id_list")
        item_keys = ("name", "canonical_name", "prop", "prop_name", "object")

    raw_names = _pick_from_sources(source, scene_source, *name_keys, default=[])
    raw_ids = _pick_from_sources(source, scene_source, *id_keys, default=[])
    names = _string_list(raw_names, item_keys=item_keys)
    ids = _string_list(raw_ids, item_keys=("asset_id", "asset_ref", "id", "value"))

    # 只接受对象中明确标注的 asset_id；不会把名称或场景 ID 猜成资产 ID。
    for item in _as_list(raw_names):
        item = _plain(item)
        if not isinstance(item, Mapping):
            continue
        explicit_id = _pick(
            item,
            "asset_id",
            "assetId",
            "canonical_asset_id",
            "asset_ref",
            default=_MISSING,
        )
        explicit_text = _pick(item, *item_keys, default=_MISSING)
        if explicit_id is not _MISSING and _text(explicit_id):
            explicit_id_text = _text(explicit_id)
            if explicit_id_text not in ids:
                ids.append(explicit_id_text)
        if explicit_text is not _MISSING and not _text(explicit_text):
            continue

    return names, ids


def _normalize_transition(value: Any) -> Any:
    if isinstance(value, Mapping):
        value = _pick(value, "type", "transition_type", "name", "value", default="scene_change")
    value = _text(value, "scene_change").casefold()
    aliases = {
        "continuous": "continuous",
        "continue": "continuous",
        "continuity": "continuous",
        "连续": "continuous",
        "连续镜头": "continuous",
        "elliptical": "elliptical",
        "ellipse": "elliptical",
        "省略": "elliptical",
        "省略转场": "elliptical",
        "same_scene_cut": "same_scene_cut",
        "same-scene-cut": "same_scene_cut",
        "same scene cut": "same_scene_cut",
        "同场切": "same_scene_cut",
        "同场切换": "same_scene_cut",
        "scene_change": "scene_change",
        "scene-change": "scene_change",
        "scene change": "scene_change",
        "场景切换": "scene_change",
        "切换场景": "scene_change",
    }
    return aliases.get(value, value or "scene_change")


def _script_model(value: ScriptOutput | Mapping[str, Any] | None) -> ScriptOutput | None:
    if value is None:
        return None
    try:
        return value if isinstance(value, ScriptOutput) else ScriptOutput.model_validate(value)
    except (ValidationError, TypeError, ValueError):
        return None


def _find_script_scene(
    script: ScriptOutput | None,
    *,
    scene_id: str,
    scene_no: int | None,
) -> Any:
    if script is None:
        return None
    for scene in script.scenes:
        if scene_id and scene.scene_id == scene_id:
            return scene
    if scene_no is not None:
        for scene in script.scenes:
            if scene.ordinal == scene_no:
                return scene
    return None


def _dialogue_id(value: Any) -> str:
    if isinstance(value, ScriptDialogue):
        return value.dialogue_line_id.strip()
    if isinstance(value, Mapping):
        return _text(_pick(value, "dialogue_line_id", "line_id", "id", "key", default=""))
    return ""


def _same_text(left: str, right: str) -> bool:
    left = left.strip()
    right = right.strip()
    if not left or not right:
        return False
    if left in right or right in left:
        return True
    left_tokens = set(_CJK_OR_WORD_RE.findall(left.casefold()))
    right_tokens = set(_CJK_OR_WORD_RE.findall(right.casefold()))
    shared = left_tokens & right_tokens
    return len(shared) >= 2 and len(shared) / max(len(left_tokens), 1) >= 0.34


def _normalize_dialogue(
    value: Any,
    *,
    shot_no: int,
    dialogue_index: int,
    script_scene: Any,
    shared_refs: Sequence[str],
) -> StoryboardDialogue:
    if isinstance(value, ScriptDialogue):
        return value.model_copy(deep=True)
    source = _plain(value)
    if isinstance(source, str):
        source = {"text": source}
    if not isinstance(source, Mapping):
        source = {"text": _text(source)}

    speaker = _text(
        _pick(source, "speaker", "character", "character_name", "name", "role", default="")
    )
    text = _text(_pick(source, "text", "content", "line", "dialogue", "speech", default=""))
    explicit_id = _text(_pick(source, "dialogue_line_id", "line_id", "id", "key", default=""))
    dialogue_id = explicit_id
    if (
        not dialogue_id
        and script_scene is not None
        and dialogue_index <= len(script_scene.dialogue)
    ):
        candidate = script_scene.dialogue[dialogue_index - 1]
        if (not text or _same_text(text, candidate.text)) and (
            not speaker or not candidate.speaker or _same_text(speaker, candidate.speaker)
        ):
            dialogue_id = candidate.dialogue_line_id
    if not dialogue_id:
        dialogue_id = f"shot-{shot_no}-dialogue-{dialogue_index}"

    payload: dict[str, Any] = {
        "dialogue_line_id": dialogue_id,
        "speaker": speaker,
        "text": text,
        "start_sec": _number(
            _pick(source, "start_sec", "start", "start_time", "start_time_sec", default=None)
        ),
        "end_sec": _number(
            _pick(source, "end_sec", "end", "end_time", "end_time_sec", default=None)
        ),
        "lip_sync_level": _text(
            _pick(
                source, "lip_sync_level", "lip_sync", "lip_sync_grade", "mouth_sync", default="none"
            )
        ),
        "acting_intent": _text(
            _pick(
                source, "acting_intent", "acting", "performance", "delivery", "purpose", default=""
            )
        ),
        "source_refs": _string_ref_list(
            _pick(source, "source_refs", "source_ref", "references", default=[]) or shared_refs
        ),
    }
    if not payload["source_refs"]:
        payload["source_refs"] = list(shared_refs)

    if not text:
        # 保留结构化的缺陷对白，让 gate 返回明确诊断，而不是在归一化时丢失原始信息。
        return payload
    try:
        return ScriptDialogue.model_validate(payload)
    except ValidationError:
        return payload


def _nested_scene(source: Mapping[str, Any]) -> tuple[Mapping[str, Any], Any]:
    raw_scene = _pick(source, "scene", "scene_info", "scene_context", default=_MISSING)
    if isinstance(raw_scene, Mapping):
        return raw_scene, raw_scene
    return {}, raw_scene


def _normalize_shot(
    value: Any,
    index: int,
    *,
    scope: str,
    script: ScriptOutput | None,
    shared_refs: Sequence[str],
) -> dict[str, Any]:
    source = _plain(value)
    if not isinstance(source, Mapping):
        source = {"action": _text(source)}
    scene_source, raw_scene_value = _nested_scene(source)

    ordinal = _integer(
        _pick(source, "ordinal", "shot_ordinal", "sequence", "sequence_no", default=index), index
    )
    if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 1:
        ordinal = index
    shot_no = _integer(
        _pick(
            source, "shot_no", "shot_number", "shot_num", "number", "no", "镜头号", default=ordinal
        ),
        ordinal,
    )

    scene_no = _integer(
        _pick_from_sources(
            source,
            scene_source,
            "scene_no",
            "scene_number",
            "scene_ordinal",
            "scene_num",
            "no",
            "number",
            "场次",
            default=None,
        )
    )
    if not isinstance(scene_no, int) or isinstance(scene_no, bool) or scene_no < 1:
        scene_no = None

    scene_id = _text(
        _pick_from_sources(
            source,
            scene_source,
            "scene_id",
            "scene_key",
            "scene_ref",
            "id",
            "key",
            default="",
        )
    )
    explicit_scene_id = bool(scene_id)
    if not scene_id:
        scene_id = f"scene-{scene_no}" if scene_no is not None else f"scene-{index}"
    script_scene = _find_script_scene(script, scene_id=scene_id, scene_no=scene_no)
    if script_scene is not None:
        if scene_no is None:
            scene_no = script_scene.ordinal
        if not explicit_scene_id:
            scene_id = script_scene.scene_id

    scene_name_value = _pick_from_sources(
        source,
        scene_source,
        "scene_name",
        "scene_heading",
        "scene_title",
        "heading",
        "name",
        "location",
        "setting",
        "场景",
        default="",
    )
    if scene_name_value is _MISSING or not _usable(scene_name_value):
        scene_name_value = raw_scene_value if isinstance(raw_scene_value, str) else ""

    time_weather = _pick_from_sources(
        source,
        scene_source,
        "time_weather",
        "time_and_weather",
        "time_weather_condition",
        default=_MISSING,
    )
    if time_weather is _MISSING or not _usable(time_weather):
        time_value = _text(
            _pick_from_sources(source, scene_source, "time", "time_of_day", "时段", default="")
        )
        weather_value = _text(
            _pick_from_sources(
                source, scene_source, "weather", "weather_condition", "天气", default=""
            )
        )
        time_weather = "；".join(item for item in (time_value, weather_value) if item)

    character_names, character_asset_ids = _entity_lists(source, scene_source, kind="character")
    prop_names, prop_asset_ids = _entity_lists(source, scene_source, kind="prop")

    camera = _pick(source, "camera", "camera_plan", "镜头", default={})
    camera_source = camera if isinstance(camera, Mapping) else {}
    prompt_intent = _pick(
        source, "prompt_intent", "shot_intent", "prompt_ir", "semantic_plan", default={}
    )
    prompt_intent = copy.deepcopy(dict(prompt_intent)) if isinstance(prompt_intent, Mapping) else {}
    dynamic_event_id = _text(
        _pick(
            source,
            "dynamic_event_id",
            "strong_dynamic_event_id",
            "event_id",
            default=_pick(
                prompt_intent,
                "dynamic_event_id",
                "strong_dynamic_event_id",
                "event_id",
                default="",
            ),
        )
    )

    raw_dialogue = _pick(
        source,
        "dialogue",
        "dialogues",
        "dialogue_list",
        "dialogue_lines",
        "dialogue_cues",
        "台词",
        default=[],
    )
    dialogue = [
        _normalize_dialogue(
            item,
            shot_no=shot_no,
            dialogue_index=dialogue_index,
            script_scene=script_scene,
            shared_refs=shared_refs,
        )
        for dialogue_index, item in enumerate(_as_list(raw_dialogue), start=1)
    ]

    external_key = _text(
        _pick(
            source,
            "external_key",
            "external_id",
            "external_shot_key",
            "stable_key",
            default="",
        )
    )
    if not external_key:
        external_key = stable_external_key(scope, scene_id, shot_no, ordinal)

    duration = _number(
        _pick(
            source,
            "duration_sec",
            "duration",
            "shot_duration",
            "planned_duration_sec",
            "target_duration",
            "时长",
            default=_MISSING,
        )
    )
    provider_duration = _number(
        _pick(
            source,
            "provider_duration_sec",
            "provider_duration",
            "generation_duration_sec",
            "generated_duration_sec",
            default=None,
        )
    )

    positions = _normalize_positions(
        _pick(source, "positions", "position", "blocking", "站位", default=[])
    )
    expressions = _normalize_expressions(
        _pick(source, "expressions", "expression", "emotions", "表情", default=[])
    )
    sound_cues = _as_list(
        _pick(source, "sound_cues", "sound", "sounds", "sound_effects", "audio", "音效", default=[])
    )
    source_refs = _string_ref_list(
        _pick(source, "source_refs", "source_ref_list", "references", default=shared_refs)
    ) or list(shared_refs)
    plot_event_ids = _string_ref_list(
        _pick(
            source,
            "plot_event_ids",
            "story_event_ids",
            "coverage_event_ids",
            "event_keys",
            default=[],
        )
    )
    continuity_state_value = _pick(
        source,
        "continuity_state",
        "state_snapshot",
        "continuity_snapshot",
        default=_pick(prompt_intent, "continuity_state", "state_snapshot", default={}),
    )
    continuity_state = (
        copy.deepcopy(dict(continuity_state_value))
        if isinstance(continuity_state_value, Mapping)
        else {}
    )

    payload = {
        "external_key": external_key,
        "shot_no": shot_no,
        "ordinal": ordinal,
        "scene_id": scene_id,
        "scene_no": scene_no,
        "scene_name": _text(scene_name_value),
        "time_weather": _text(time_weather),
        "scene_asset_id": _text(
            _pick(
                source,
                "scene_asset_id",
                "scene_asset_ref",
                "scene_asset_key",
                default=_pick(
                    scene_source,
                    "scene_asset_id",
                    "scene_asset_ref",
                    "asset_id",
                    "assetId",
                    default="",
                ),
            )
        )
        or None,
        "character_names": character_names,
        "character_asset_ids": character_asset_ids,
        "prop_names": prop_names,
        "prop_asset_ids": prop_asset_ids,
        "positions": positions,
        "expressions": expressions,
        "plot_event_ids": plot_event_ids,
        "continuity_state": continuity_state,
        "start_state": _text(
            _pick(
                source,
                "start_state",
                "start",
                "initial_state",
                "opening_state",
                "begin_state",
                "起始状态",
                default="",
            )
        ),
        "action": _text(
            _pick(
                source,
                "action",
                "main_action",
                "visual_action",
                "event",
                "description",
                "动作",
                default="",
            )
        ),
        "end_state": _text(
            _pick(
                source,
                "end_state",
                "end",
                "finish",
                "final_state",
                "closing_state",
                "结束状态",
                default="",
            )
        ),
        "next_hook": _text(
            _pick(source, "next_hook", "hook", "next_shot_hook", "衔接", default="")
        ),
        "transition_from_prev": _normalize_transition(
            _pick(
                source,
                "transition_from_prev",
                "transition_type",
                "transition",
                "cut_type",
                "continuity",
                "cut",
                "转场",
                default="scene_change",
            )
        ),
        "duration_sec": duration,
        "provider_duration_sec": provider_duration,
        "dialogue": dialogue,
        "narration": _text(_pick(source, "narration", "voiceover", "旁白", default="")),
        "sound_cues": sound_cues,
        "dynamic_level": _number(
            _pick(
                source,
                "dynamic_level",
                "dynamic",
                "intensity",
                "dynamic_intensity",
                "动态等级",
                default=0.0,
            ),
            0.0,
        ),
        "dynamic_event_id": dynamic_event_id or None,
        "frame_description": _text(
            _pick(
                source,
                "frame_description",
                "frame",
                "visual_description",
                "image_description",
                "画面",
                default="",
            )
        ),
        "source_refs": source_refs,
        "reference_bindings": _dict_list(
            _pick(
                source, "reference_bindings", "reference_binding", "references_bindings", default=[]
            )
        ),
        "prompt_intent": prompt_intent,
        "source_type": _text(_pick(source, "source_type", default="screenplay")) or "screenplay",
        "source_ref": _text(_pick(source, "source_ref", default="")),
        "shot_type": _text(_pick(source, "shot_type", "shot_size", "framing", "景别", default="")),
        "camera_angle": _text(
            _pick(
                source,
                "camera_angle",
                "angle",
                "view_angle",
                "机位",
                default=_pick(camera_source, "angle", default=""),
            )
        ),
        "camera_move": _text(
            _pick(
                source,
                "camera_move",
                "camera_motion",
                "movement",
                "运镜",
                default=_pick(camera_source, "move", "motion", "movement", default=""),
            )
        ),
        "image_prompt": _text(
            _pick(source, "image_prompt", "image_prompt_override", "图片提示词", default="")
        ),
        "video_prompt": _text(
            _pick(source, "video_prompt", "video_prompt_override", "视频提示词", default="")
        ),
        "negative_prompt": _text(
            _pick(source, "negative_prompt", "negative", "负面提示词", default="")
        ),
    }
    return payload


def _extract_shot_values(root: Mapping[str, Any] | list[Any]) -> list[Any]:
    if isinstance(root, list):
        return _as_list(root)
    candidate = _pick(
        root,
        "shots",
        "storyboard_shots",
        "shot_list",
        "shots_list",
        "storyboard",
        "镜头列表",
        "分镜列表",
        "镜头",
        "分镜",
        default=[],
    )
    if isinstance(candidate, Mapping):
        nested = _pick(
            candidate, "shots", "shot_list", "items", "list", "镜头列表", default=_MISSING
        )
        if nested is not _MISSING:
            candidate = nested
    return _as_list(candidate)


def _normalize_scene_values(
    root: Mapping[str, Any], shots: Sequence[StoryboardShot]
) -> list[dict[str, Any]]:
    raw_scenes = _pick(root, "scenes", "scene_list", "scene_blocks", "场景列表", default=_MISSING)
    if raw_scenes is not _MISSING and _usable(raw_scenes):
        return [
            copy.deepcopy(dict(item))
            if isinstance(_plain(item), Mapping)
            else {"name": _text(item)}
            for item in _as_list(raw_scenes)
        ]

    result: list[dict[str, Any]] = []
    by_key: dict[tuple[str, int | None, str], dict[str, Any]] = {}
    for shot in shots:
        key = (shot.scene_id, shot.scene_no, shot.scene_name)
        scene = by_key.get(key)
        if scene is None:
            scene = {
                "scene_id": shot.scene_id,
                "scene_no": shot.scene_no,
                "scene_name": shot.scene_name,
                "time_weather": shot.time_weather,
                "shot_external_keys": [],
                "duration_sec": 0.0,
            }
            by_key[key] = scene
            result.append(scene)
        scene["shot_external_keys"].append(shot.external_key)
        scene["duration_sec"] += shot.duration_sec
    return result


def stable_external_key(
    run_id_or_chapter_id: str | None = None,
    scene_id: str = "",
    shot_no: int = 0,
    ordinal: int = 0,
    *,
    run_id: str = "",
    chapter_id: str = "",
) -> str:
    """按调用方提供的 run/chapter 作用域生成确定性外部镜头键。"""

    scope = run_id or chapter_id or run_id_or_chapter_id or ""

    def component(value: Any, fallback: str) -> str:
        text = str(value).strip() if value is not None else ""
        text = re.sub(r"\s+", "_", text)
        return text or fallback

    return ":".join(
        (
            "storyboard",
            component(scope, "unscoped"),
            component(scene_id, "scene"),
            f"shot-{component(shot_no, '0')}",
            f"ordinal-{component(ordinal, '0')}",
        )
    )


def normalize_storyboard_output(
    raw: Any,
    *,
    artifact_meta: ArtifactMeta | Mapping[str, Any],
    target_duration_sec: float,
    run_id: str = "",
    chapter_id: str = "",
    script: ScriptOutput | Mapping[str, Any] | None = None,
) -> StoryboardOutput:
    """将 Provider 的标准分镜别名归一为严格模型，不写入媒体或数据库。"""

    root = _root_mapping(raw)
    root_mapping: Mapping[str, Any] = root if isinstance(root, Mapping) else {}
    scope = _text(run_id) or _text(chapter_id)
    shared_refs = _string_ref_list(
        _pick(root_mapping, "source_refs", "source_ref", "references", default=[])
    )
    script_content = _script_model(script)
    shots = [
        StoryboardShot.model_validate(
            _normalize_shot(
                item,
                index,
                scope=scope,
                script=script_content,
                shared_refs=shared_refs,
            )
        )
        for index, item in enumerate(_extract_shot_values(root), start=1)
    ]

    assumptions = _string_list(_pick(root_mapping, "assumptions", "assumption", "假设", default=[]))
    open_questions = _string_list(
        _pick(root_mapping, "open_questions", "questions", "unresolved", "待确认", default=[])
    )
    payload = {
        "artifact_meta": ArtifactMeta.model_validate(artifact_meta),
        # 阶段参数是运行配置真源，不能被 Provider 同名字段覆盖。
        "target_duration_sec": target_duration_sec,
        "shots": shots,
        "scenes": _normalize_scene_values(root_mapping, shots),
        "assumptions": assumptions,
        "open_questions": open_questions,
    }
    return StoryboardOutput.model_validate(payload)


def _diagnostic(
    level: Literal["warning", "blocking"],
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


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _duration_matches(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=1e-6, abs_tol=_TIME_TOLERANCE)


def _references_previous_frame(shot: StoryboardShot) -> bool:
    values: list[Any] = [shot.reference_bindings, shot.prompt_intent]
    marker_keys = (
        "previous",
        "prev",
        "上一镜",
        "previous_shot",
        "previous_keyframe",
        "previous_end_frame",
        "previous_frame",
        "inherit_previous",
        "last_frame",
        "end_frame_path",
    )

    def visit(value: Any) -> bool:
        if isinstance(value, Mapping):
            role = str(value.get("role", value.get("type", value.get("kind", "")))).casefold()
            if any(marker in role for marker in marker_keys):
                return True
            for key, item in value.items():
                key_text = str(key).casefold()
                if any(marker in key_text for marker in marker_keys) and _usable(item):
                    return True
                if visit(item):
                    return True
            return False
        if isinstance(value, list | tuple):
            return any(visit(item) for item in value)
        if isinstance(value, str):
            return any(marker in value.casefold() for marker in marker_keys)
        return False

    return any(visit(value) for value in values)


def _has_frame_reference(shot: StoryboardShot) -> bool:
    if not _is_blank(shot.end_state) or not _is_blank(shot.frame_description):
        return True
    return _references_previous_frame(shot)


def _dialogue_text(value: Any) -> str:
    if isinstance(value, ScriptDialogue):
        return value.text.strip()
    if isinstance(value, Mapping):
        return _text(_pick(value, "text", "content", "line", "dialogue", default=""))
    return ""


def _continuity_boundary(shot: StoryboardShot, side: str) -> dict[str, Any]:
    state = shot.continuity_state
    if not isinstance(state, Mapping) or not state:
        return {}
    boundary = state.get(side)
    if isinstance(boundary, Mapping):
        return copy.deepcopy(dict(boundary))
    if "start" not in state and "end" not in state:
        return copy.deepcopy(dict(state))
    return {}


def _continuity_differences(
    expected: Mapping[str, Any], actual: Mapping[str, Any], prefix: str = ""
) -> list[dict[str, Any]]:
    differences: list[dict[str, Any]] = []
    for key in sorted(set(expected) | set(actual), key=str):
        path = f"{prefix}.{key}" if prefix else str(key)
        if key not in actual:
            differences.append({"path": path, "expected": expected[key], "actual": None})
            continue
        if key not in expected:
            differences.append({"path": path, "expected": None, "actual": actual[key]})
            continue
        expected_value = expected[key]
        actual_value = actual[key]
        if isinstance(expected_value, Mapping) and isinstance(actual_value, Mapping):
            differences.extend(_continuity_differences(expected_value, actual_value, prefix=path))
        elif expected_value != actual_value:
            differences.append({"path": path, "expected": expected_value, "actual": actual_value})
    return differences


def _shot_text_values(shot: StoryboardShot) -> list[str]:
    values = [
        shot.scene_name,
        shot.time_weather,
        shot.start_state,
        shot.action,
        shot.end_state,
        shot.next_hook,
        shot.frame_description,
        shot.narration,
        *shot.source_refs,
    ]
    values.extend(_text(item) for item in shot.sound_cues)
    for item in shot.dialogue:
        if isinstance(item, ScriptDialogue):
            values.extend((item.speaker, item.text, item.acting_intent))
        elif isinstance(item, Mapping):
            values.extend(
                (
                    _text(_pick(item, "speaker", "character", default="")),
                    _text(_pick(item, "text", "content", "line", default="")),
                )
            )
    return [item.strip() for item in values if isinstance(item, str) and item.strip()]


def _shot_action_values(shot: StoryboardShot) -> list[str]:
    """返回用于兑现动作/节奏 visual_action 的字段，避免 end_state 伪造动作命中。"""

    values = [shot.action, shot.frame_description]
    prompt_action = _pick(shot.prompt_intent, "action", "visual_action", "main_action", default="")
    values.append(_text(prompt_action))
    for event in _action_events(shot):
        if isinstance(event, Mapping):
            values.extend(
                (
                    _text(_pick(event, "action", "visual_action", "main_action", default="")),
                    _text(_pick(event, "event_summary", "summary", default="")),
                )
            )
        else:
            values.append(_text(event))
    return [item.strip() for item in values if isinstance(item, str) and item.strip()]


# 连续性判定的高频虚词：只共享这些字不算承接证据
_CONTINUITY_STOPWORDS = frozenset(
    "的地得着了过在与及和或之于此其些如把被将向从往到对为予以让叫令"
    "就是才又也都更很最太非常分秒时候里面上下前后左右中间"
)


def _text_matches(expected: str, actual_values: Sequence[str], *, strict: bool = False) -> bool:
    """文本弱匹配；strict=True 用于画面连续性判定。

    严格模式：过滤高频虚词后取共享 token，分母取双方 token 数较大者，
    避免「两个完全不同的画面描述因共享常用字而判为承接」。
    """

    expected = expected.strip()
    if not expected:
        return True
    for actual in actual_values:
        actual = actual.strip()
        if expected in actual or actual in expected:
            return True
        expected_tokens = set(_CJK_OR_WORD_RE.findall(expected.casefold()))
        actual_tokens = set(_CJK_OR_WORD_RE.findall(actual.casefold()))
        if strict:
            expected_tokens -= _CONTINUITY_STOPWORDS
            actual_tokens -= _CONTINUITY_STOPWORDS
            shared = expected_tokens & actual_tokens
            if (
                len(shared) >= 2
                and len(shared) / max(len(expected_tokens), len(actual_tokens), 1) >= 0.34
            ):
                return True
            continue
        shared = expected_tokens & actual_tokens
        if len(shared) >= 2 and len(shared) / max(len(expected_tokens), 1) >= 0.34:
            return True
    return False


def _scene_shots(output: StoryboardOutput, scene: Any) -> list[StoryboardShot]:
    by_id = [shot for shot in output.shots if scene.scene_id and shot.scene_id == scene.scene_id]
    if by_id:
        return by_id
    return [shot for shot in output.shots if scene.ordinal == shot.scene_no]


def _rhythm_model(value: RhythmOutput | Mapping[str, Any] | None) -> RhythmOutput | None:
    if value is None:
        return None
    try:
        return value if isinstance(value, RhythmOutput) else RhythmOutput.model_validate(value)
    except (ValidationError, TypeError, ValueError):
        return None


def _shot_intervals(shots: Sequence[StoryboardShot]) -> list[tuple[float, float, StoryboardShot]]:
    current = 0.0
    result: list[tuple[float, float, StoryboardShot]] = []
    for shot in shots:
        end = current + shot.duration_sec
        result.append((current, end, shot))
        current = end
    return result


def _dynamic_event_ids(shot: StoryboardShot) -> set[str]:
    result = (
        {shot.dynamic_event_id.strip()}
        if shot.dynamic_event_id and shot.dynamic_event_id.strip()
        else set()
    )
    for key in ("dynamic_event_id", "strong_dynamic_event_id", "event_id"):
        value = _pick(shot.prompt_intent, key, default=_MISSING)
        if value is not _MISSING and _text(value):
            result.add(_text(value))
    return result


def _script_diagnostics(
    output: StoryboardOutput,
    script: ScriptOutput | Mapping[str, Any],
    diagnostics: list[Diagnostic],
) -> None:
    try:
        content = (
            script if isinstance(script, ScriptOutput) else ScriptOutput.model_validate(script)
        )
    except (ValidationError, TypeError, ValueError) as exc:
        diagnostics.append(
            _diagnostic(
                "blocking",
                "storyboard_script_invalid",
                "传入的详细剧本不符合阶段 3 schema",
                "script",
                {"errors": getattr(exc, "errors", lambda: [])()},
            )
        )
        return

    actual_dialogues = [
        (_dialogue_id(item), _dialogue_text(item))
        for shot in output.shots
        for item in shot.dialogue
        if _dialogue_id(item)
    ]
    actual_dialogue_ids = {item[0] for item in actual_dialogues}
    expected_dialogues = {
        dialogue.dialogue_line_id: dialogue.text.strip()
        for scene in content.scenes
        for dialogue in scene.dialogue
    }
    extra_dialogue_ids = sorted(actual_dialogue_ids - set(expected_dialogues))
    if extra_dialogue_ids:
        diagnostics.append(
            _diagnostic(
                "blocking",
                "storyboard_script_dialogue_extra",
                "标准分镜新增了剧本中不存在的对白行",
                "shots",
                {"dialogue_line_ids": extra_dialogue_ids},
            )
        )
    expected_dialogue_order = list(expected_dialogues)
    actual_known_dialogue_order = [
        dialogue_id for dialogue_id, _ in actual_dialogues if dialogue_id in expected_dialogues
    ]
    expected_present_order = [
        dialogue_id for dialogue_id in expected_dialogue_order if dialogue_id in actual_dialogue_ids
    ]
    if actual_known_dialogue_order != expected_present_order:
        diagnostics.append(
            _diagnostic(
                "blocking",
                "storyboard_script_dialogue_order_mismatch",
                "标准分镜对白顺序与剧本不一致",
                "shots",
                {
                    "script_order": expected_dialogue_order,
                    "storyboard_order": actual_known_dialogue_order,
                },
            )
        )
    for dialogue_id, actual_text in actual_dialogues:
        expected_text = expected_dialogues.get(dialogue_id)
        if expected_text is not None and actual_text != expected_text:
            diagnostics.append(
                _diagnostic(
                    "blocking",
                    "storyboard_script_dialogue_text_mismatch",
                    f"标准分镜对白原文被改写：{dialogue_id}",
                    "shots",
                    {
                        "dialogue_line_id": dialogue_id,
                        "script_text": expected_text,
                        "storyboard_text": actual_text,
                    },
                )
            )
    expected_plot_event_ids = {
        event_id for scene in content.scenes for event_id in scene.plot_event_ids
    }
    actual_plot_event_ids = {
        event_id for shot in output.shots for event_id in shot.plot_event_ids if event_id.strip()
    }
    if expected_plot_event_ids and not actual_plot_event_ids:
        diagnostics.append(
            _diagnostic(
                "blocking",
                "storyboard_script_plot_event_ids_missing",
                "标准分镜未显式携带剧本剧情事件 ID，不能证明剧情覆盖关系",
                "shots",
                {"expected_plot_event_ids": sorted(expected_plot_event_ids)},
            )
        )
    else:
        missing_plot_event_ids = sorted(expected_plot_event_ids - actual_plot_event_ids)
        extra_plot_event_ids = sorted(actual_plot_event_ids - expected_plot_event_ids)
        if missing_plot_event_ids:
            diagnostics.append(
                _diagnostic(
                    "blocking",
                    "storyboard_script_plot_event_missing",
                    "标准分镜遗漏剧本剧情事件",
                    "shots",
                    {"missing_plot_event_ids": missing_plot_event_ids},
                )
            )
        if extra_plot_event_ids:
            diagnostics.append(
                _diagnostic(
                    "blocking",
                    "storyboard_script_plot_event_extra",
                    "标准分镜包含剧本之外的新增剧情事件",
                    "shots",
                    {"extra_plot_event_ids": extra_plot_event_ids},
                )
            )
    for scene_index, scene in enumerate(content.scenes):
        scene_shots = _scene_shots(output, scene)
        scene_path = f"script.scenes[{scene_index}]"
        if not scene_shots:
            diagnostics.append(
                _diagnostic(
                    "blocking",
                    "storyboard_script_scene_missing",
                    f"标准分镜缺少剧本场景：{scene.scene_id}",
                    scene_path,
                    {"scene_id": scene.scene_id, "scene_no": scene.ordinal},
                )
            )
            continue
        action_values = [item for shot in scene_shots for item in _shot_action_values(shot)]
        if scene.action and not _text_matches(scene.action, action_values):
            diagnostics.append(
                _diagnostic(
                    "blocking",
                    "storyboard_script_action_unfulfilled",
                    f"标准分镜未兑现剧本动作：{scene.action}",
                    f"shots[{output.shots.index(scene_shots[0])}].action",
                    {"scene_id": scene.scene_id, "action": scene.action},
                )
            )
        shot_duration = sum(shot.duration_sec for shot in scene_shots)
        if not _duration_matches(shot_duration, scene.duration_sec):
            diagnostics.append(
                _diagnostic(
                    "warning",
                    "storyboard_script_scene_duration_mismatch",
                    "标准分镜场景时长与剧本场景时长不一致",
                    scene_path,
                    {
                        "storyboard_duration_sec": shot_duration,
                        "script_duration_sec": scene.duration_sec,
                    },
                )
            )
        for dialogue_index, dialogue in enumerate(scene.dialogue):
            if dialogue.dialogue_line_id not in actual_dialogue_ids:
                diagnostics.append(
                    _diagnostic(
                        "blocking",
                        "storyboard_script_dialogue_missing",
                        f"标准分镜缺少剧本对白行：{dialogue.dialogue_line_id}",
                        f"{scene_path}.dialogue[{dialogue_index}]",
                        {"dialogue_line_id": dialogue.dialogue_line_id},
                    )
                )
        narration_values = [
            item for shot in scene_shots for item in (shot.narration,) if _usable(item)
        ]
        for narration_index, expected_narration in enumerate(scene.narration):
            text = expected_narration.strip()
            if not text or _text_matches(text, narration_values):
                continue
            diagnostics.append(
                _diagnostic(
                    "blocking",
                    "storyboard_script_narration_missing",
                    f"标准分镜缺少剧本旁白：{text[:20]}",
                    f"{scene_path}.narration[{narration_index}]",
                    {"narration": text},
                )
            )

    expected_narrations = [
        text
        for scene in content.scenes
        for text in (item.strip() for item in scene.narration)
        if text
    ]
    for shot in output.shots:
        actual = shot.narration.strip()
        if not actual:
            continue
        if any(_same_text(expected, actual) for expected in expected_narrations):
            continue
        diagnostics.append(
            _diagnostic(
                "blocking",
                "storyboard_script_narration_extra",
                f"标准分镜包含剧本之外的旁白：{actual[:20]}",
                f"shots[{output.shots.index(shot)}].narration",
                {"narration": actual},
            )
        )


def _rhythm_diagnostics(
    output: StoryboardOutput,
    rhythm: RhythmOutput | Mapping[str, Any],
    diagnostics: list[Diagnostic],
) -> None:
    content = _rhythm_model(rhythm)
    if content is None:
        diagnostics.append(
            _diagnostic(
                "blocking", "storyboard_rhythm_invalid", "传入的节奏产物不符合 schema", "rhythm"
            )
        )
        return

    total = sum(shot.duration_sec for shot in output.shots)
    if not _duration_matches(total, content.target_duration_sec):
        diagnostics.append(
            _diagnostic(
                "warning",
                "storyboard_rhythm_duration_mismatch",
                "标准分镜总时长与节奏阶段目标时长不一致",
                "shots",
                {
                    "storyboard_duration_sec": total,
                    "rhythm_target_duration_sec": content.target_duration_sec,
                },
            )
        )

    intervals = _shot_intervals(output.shots)
    for index, segment in enumerate(content.timeline):
        candidates = [
            shot
            for start, end, shot in intervals
            if min(end, segment.end_sec) - max(start, segment.start_sec) > _TIME_TOLERANCE
        ]
        values = [item for shot in candidates for item in _shot_text_values(shot)]
        action_values = [item for shot in candidates for item in _shot_action_values(shot)]
        # visual_action 必须被镜头动作兑现；事件摘要必须被镜头文本兑现（防「动作文本
        # 相似但事件本身未发生」的假命中）；narrative_task 仅在 visual_action 为空时兜底，
        # 避免因台词主观措辞差异误杀。
        action_fulfilled = not segment.visual_action or _text_matches(
            segment.visual_action, action_values
        )
        content_fulfilled = not segment.event_summary or _text_matches(
            segment.event_summary, values
        )
        task_fulfilled = not segment.narrative_task or _text_matches(segment.narrative_task, values)
        fulfilled = action_fulfilled and content_fulfilled
        if not segment.visual_action:
            # narrative_task 只在没有 visual_action 时兜底，避免主观文案误杀
            fulfilled = fulfilled and task_fulfilled
        if not candidates or not fulfilled:
            diagnostics.append(
                _diagnostic(
                    "blocking",
                    "storyboard_rhythm_event_unfulfilled",
                    "标准分镜未兑现节奏时间轴事件",
                    f"rhythm.timeline[{index}]",
                    {
                        "segment_id": segment.segment_id,
                        "event_summary": segment.event_summary,
                        "visual_action": segment.visual_action,
                        "narrative_task": segment.narrative_task,
                    },
                )
            )
        if candidates:
            actual_dynamic = max(shot.dynamic_level for shot in candidates)
            if actual_dynamic + _TIME_TOLERANCE < segment.dynamic_level:
                diagnostics.append(
                    _diagnostic(
                        "warning",
                        "storyboard_rhythm_dynamic_level_mismatch",
                        "标准分镜动态等级低于节奏时间轴要求",
                        f"rhythm.timeline[{index}].dynamic_level",
                        {
                            "expected_dynamic_level": segment.dynamic_level,
                            "actual_dynamic_level": actual_dynamic,
                        },
                    )
                )

    expected_event_ids = set(content.strong_dynamic_budget.event_ids)
    expected_event_ids.update(event.event_id for event in content.strong_dynamic_budget.events)
    expected_event_ids.update(
        segment.strong_dynamic_event_id
        for segment in content.timeline
        if segment.strong_dynamic_event_id
    )
    actual_event_ids = {event_id for shot in output.shots for event_id in _dynamic_event_ids(shot)}
    missing_event_ids = sorted(expected_event_ids - actual_event_ids)
    if missing_event_ids:
        diagnostics.append(
            _diagnostic(
                "blocking",
                "storyboard_rhythm_strong_dynamic_event_id_missing",
                "标准分镜缺少节奏阶段要求兑现的强动态 event id",
                "shots",
                {"missing_event_ids": missing_event_ids},
            )
        )
    extra_event_ids = sorted(actual_event_ids - expected_event_ids)
    if extra_event_ids:
        diagnostics.append(
            _diagnostic(
                "blocking",
                "storyboard_rhythm_strong_dynamic_event_id_extra",
                "标准分镜包含节奏产物之外的强动态事件",
                "shots",
                {"extra_event_ids": extra_event_ids},
            )
        )

    anonymous_strong_count = sum(
        1 for shot in output.shots if shot.dynamic_level >= 0.8 and not _dynamic_event_ids(shot)
    )
    event_count = len(actual_event_ids) + anonymous_strong_count
    max_events = content.strong_dynamic_budget.max_events
    if event_count > max_events:
        diagnostics.append(
            _diagnostic(
                "blocking",
                "storyboard_rhythm_dynamic_budget_exceeded",
                "标准分镜强动态事件数量超过节奏预算",
                "shots",
                {"event_count": event_count, "max_events": max_events},
            )
        )


# ---------------------------------------------------------------------------
# 语义一致性诊断（分镜门禁扩展）
#
# 目标：把"状态描述互相打架"类缺陷拦在媒体生成之前。规则沉淀自 2026-08
# 两组真实连续性评审（雨夜地铁口首帧冲突 / 青云山松下问剑站位冲突）的共性：
# 站位与动作起点矛盾、断裂残句、"空镜"误用、时段漂移、持有道具失联、
# 易渲染成文字的表述。全部为只读启发式检查，统一输出 warning 级诊断，
# 不改变既有 blocking 判定口径。
# ---------------------------------------------------------------------------

_POSITION_FACING_RE = re.compile(r"(?:面向|朝向|面对|望向|看向)([^，。；、\s]{1,8})")
_TURN_ACTION_RE = re.compile(r"回头|回望|转过头|转过身|侧过脸|侧头|转头|回首")
_DUPLICATED_PHRASE_RE = re.compile(r"([^，。；、！？\s]{4,16})，\1")
_FRAGMENTED_SENTENCE_RES = (
    re.compile(r"，没有；"),
    re.compile(r"[，,]\s*[；;]"),
    re.compile(r"[；;]\s*。"),
)
_TIME_PHASE_PATTERNS: dict[str, re.Pattern[str]] = {
    "夜": re.compile(r"夜露|深夜|夜晚|入夜|凌晨|午夜|黑夜"),
    "晨": re.compile(r"清晨|黎明|破晓|晨光|拂晓"),
    "昼": re.compile(r"正午|烈日"),
    "暮": re.compile(r"黄昏|傍晚|暮色|夕阳"),
}
_PROP_HELD_RE_TEMPLATE = (
    "{prop}[^。；\n]{0,10}(?:持|握|捏|拿|抱|攥)|(?:持|握|捏|抱|攥)[^。；\n]{0,6}{prop}"
)
_PROP_RELEASE_RE = re.compile(r"放下|收起|收进|收回|交给|递给|塞回|插回|留在")
_READABLE_TEXT_TERM = "符文"
# 武器类道具常见别名词表：正文出现别名而未带绑定名时，视为道具身份漂移风险
_WEAPON_ALIAS_TERMS = ("长剑", "佩剑", "利剑", "宝剑")
_NON_FACING_TARGETS = frozenset({"镜头", "远方", "前方", "云海", "远处"})
_SEMANTIC_TEXT_FIELDS = ("start_state", "action", "end_state", "narration")


def _semantic_field_texts(shot: StoryboardShot) -> str:
    """聚合一个镜头的四段正文文本，供语义扫描使用。"""

    values: list[str] = []
    for name in _SEMANTIC_TEXT_FIELDS:
        value = getattr(shot, name, "")
        if isinstance(value, str) and value.strip():
            values.append(value)
    return "\n".join(values)


def _semantic_consistency_diagnostics(shots: Sequence[StoryboardShot]) -> list[Diagnostic]:
    """扫描标准分镜的语义一致性缺陷，返回 warning 级诊断列表；只读输入。"""

    diagnostics: list[Diagnostic] = []

    def add(path: str, code: str, message: str, **details: Any) -> None:
        diagnostics.append(_diagnostic("warning", code, message, path, dict(details)))

    for index, shot in enumerate(shots):
        path = f"shots[{index}]"
        body = _semantic_field_texts(shot)

        # 规则 1：站位锁定 0 秒状态——某角色的站位写明"面向另一角色"时，
        # 核心动作里再出现回头/侧头动作，说明 0 秒朝向与动作起点互相矛盾。
        cast_names = [name for name in shot.character_names if name]
        conflict_reported = False
        for position_item in shot.positions:
            if conflict_reported:
                break
            if isinstance(position_item, Mapping):
                owner = str(position_item.get("character") or "")
                texts = [str(value) for value in position_item.values() if isinstance(value, str)]
            elif isinstance(position_item, str):
                owner = ""
                texts = [position_item]
            else:
                continue
            for facing in _POSITION_FACING_RE.finditer("；".join(texts)):
                target = facing.group(1)
                if target in _NON_FACING_TARGETS:
                    continue
                others = [name for name in cast_names if name != owner]
                if not any(target in name or name in target for name in others):
                    continue
                # 时间轴任意分段出现"回头/侧头看她"，都说明 0 秒不应已面向她
                turn = _TURN_ACTION_RE.search(shot.action)
                if turn is None:
                    continue
                add(
                    f"{path}.positions",
                    "storyboard_position_action_conflict",
                    "人物站位描述 0 秒已面向其他角色，核心动作又执行回头/侧头动作，状态互相矛盾",
                    owner=owner,
                    facing_target=target,
                    turn_excerpt=turn.group(0),
                )
                conflict_reported = True
                break

        # 规则 2：断裂残句 / 相邻重复短语
        for field_name in _SEMANTIC_TEXT_FIELDS:
            text = getattr(shot, field_name, "")
            if not isinstance(text, str) or not text:
                continue
            matched = None
            for pattern in (*_FRAGMENTED_SENTENCE_RES, _DUPLICATED_PHRASE_RE):
                matched = pattern.search(text)
                if matched is not None:
                    break
            if matched is not None:
                add(
                    f"{path}.{field_name}",
                    "storyboard_fragmented_sentence",
                    "文本存在断裂残句或相邻重复短语，视频模型可能误读动作边界",
                    excerpt=matched.group(0),
                )

        # 规则 3：已有角色时禁用"空镜"表述
        if shot.character_names and "空镜" in body:
            add(
                path,
                "storyboard_empty_shot_with_cast",
                "镜头内已有角色却使用'空镜'表述，可能生成无人镜头",
                characters=list(shot.character_names),
            )

        # 规则 4：时段锁定——time_weather 已定时段后，正文不得出现其他时段暗示词
        declared_phases = {
            phase
            for phase, pattern in _TIME_PHASE_PATTERNS.items()
            if pattern.search(shot.time_weather or "")
        }
        if declared_phases:
            conflict_terms: dict[str, str] = {}
            for phase, pattern in _TIME_PHASE_PATTERNS.items():
                if phase in declared_phases:
                    continue
                for found in pattern.finditer(body):
                    conflict_terms.setdefault(found.group(0), phase)
            if conflict_terms:
                add(
                    path,
                    "storyboard_time_phase_drift",
                    "正文出现与本镜时段冲突的时间暗示词，可能导致昼夜突变",
                    time_weather=shot.time_weather,
                    conflict_terms=sorted(conflict_terms),
                )

        # 规则 5："符文"等易被模型渲染成可读文字的表述（否定语境除外）
        for matched in re.finditer(_READABLE_TEXT_TERM, body):
            prefix = body[max(0, matched.start() - 8) : matched.start()]
            if re.search(r"(?:无|禁|不)[^。，；]{0,6}$", prefix):
                continue
            add(
                path,
                "storyboard_readable_text_risk",
                "正文出现'符文'等易渲染成可读文字的表述，建议改为'能量纹路'并在禁止项覆盖",
                excerpt=body[max(0, matched.start() - 10) : matched.end() + 10],
            )
            break

        # 规则 6：道具称谓漂移——正文用别名指称已绑定道具，且该句未出现绑定名
        for prop in shot.prop_names:
            if not prop or "剑" not in prop:
                continue
            drifted = None
            for clause in re.split(r"[。；！？\n]", body):
                if not clause.strip() or prop in clause:
                    continue
                for alias in _WEAPON_ALIAS_TERMS:
                    if alias != prop and alias in clause:
                        drifted = (alias, clause.strip()[:40])
                        break
                if drifted is not None:
                    break
            if drifted is not None:
                alias_term, excerpt = drifted
                add(
                    path,
                    "storyboard_prop_alias_drift",
                    "正文以别名指称已绑定的剑类道具，容易造成道具身份漂移，应统一使用绑定名",
                    bound_prop=prop,
                    alias_term=alias_term,
                    excerpt=excerpt,
                )

    # 规则 7：跨镜持有道具追踪——上一镜结尾仍持有的道具，下一镜必须出现或明确放下
    for index in range(1, len(shots)):
        previous, current = shots[index - 1], shots[index]
        if current.transition_from_prev not in ("continuous", "same_scene_cut"):
            continue
        if previous.scene_id and current.scene_id and previous.scene_id != current.scene_id:
            continue
        holder_absent = bool(previous.character_names) and all(
            name not in current.character_names for name in previous.character_names
        )
        if holder_absent:
            # 持有人整体离场时允许道具随人出画，不再追查
            continue
        previous_text = previous.end_state or previous.action
        current_text = _semantic_field_texts(current)
        for prop in previous.prop_names:
            if not prop:
                continue
            # 模板含正则量词大括号，不能用 str.format，改用字面替换
            held_pattern = re.compile(_PROP_HELD_RE_TEMPLATE.replace("{prop}", re.escape(prop)))
            if held_pattern.search(previous_text) is None:
                continue
            if prop in current_text:
                continue
            if _PROP_RELEASE_RE.search(previous_text) is not None:
                # 上一镜明确写了放下/交出，属于合法离手
                continue
            add(
                f"shots[{index}]",
                "storyboard_held_prop_untracked",
                "上一镜结束仍持有的道具在本镜未交代位置，后续镜头可能凭空消失或凭空复活",
                prop=prop,
                previous_shot_no=previous.shot_no,
                current_shot_no=current.shot_no,
            )

    return diagnostics


def validate_storyboard_output(
    output: StoryboardOutput | Mapping[str, Any],
    *,
    script: ScriptOutput | Mapping[str, Any] | None = None,
    rhythm: RhythmOutput | Mapping[str, Any] | None = None,
    target_duration_sec: float | None = None,
) -> list[Diagnostic]:
    """返回阶段 4 门禁诊断；只读输入，不修改 output。"""

    try:
        content = (
            output
            if isinstance(output, StoryboardOutput)
            else StoryboardOutput.model_validate(output)
        )
    except (ValidationError, TypeError, ValueError) as exc:
        return [
            _diagnostic(
                "blocking",
                "storyboard_output_invalid",
                "标准分镜不符合阶段 4 schema",
                None,
                {"errors": getattr(exc, "errors", lambda: [])()},
            )
        ]

    diagnostics: list[Diagnostic] = []
    shots = list(content.shots)
    if not shots:
        diagnostics.append(
            _diagnostic("blocking", "storyboard_no_shots", "标准分镜至少需要一个镜头", "shots")
        )

    # 镜头数量预算：按分镜总预算折算建议区间与硬上限。仅当脚本各场时长
    # 总和与预算一致时启用（异常场景时长会让预算失真，交由时长门禁阻断），
    # 防止镜头上限干扰对白容量导致的合理拆镜。
    budget_sec = float(expected_duration_sec(content, target_duration_sec) or 0)
    script_total = _script_total_duration(script)
    if budget_sec > 0 and script_total is not None and _duration_matches(budget_sec, script_total):
        lower, suggested_cap = batched_shot_count_limits(budget_sec)
        if len(shots) > suggested_cap:
            diagnostics.append(
                _diagnostic(
                    "blocking",
                    "storyboard_shot_count_over_budget",
                    f"分镜总镜头数 {len(shots)} 超过 {budget_sec:g} 秒预算允许的 "
                    f"{suggested_cap} 个硬上限",
                    "shots",
                    {
                        "shot_count": len(shots),
                        "budget_sec": budget_sec,
                        "hard_cap": suggested_cap,
                    },
                )
            )
        elif len(shots) > lower + 2:
            diagnostics.append(
                _diagnostic(
                    "warning",
                    "storyboard_shot_count_over_suggested",
                    f"分镜总镜头数 {len(shots)} 高于 {budget_sec:g} 秒预算建议区间 "
                    f"上限 {lower + 2}，建议合并相邻镜头",
                    "shots",
                    {
                        "shot_count": len(shots),
                        "budget_sec": budget_sec,
                        "suggested_cap": lower + 2,
                        "hard_cap": suggested_cap,
                    },
                )
            )

    external_keys = [shot.external_key for shot in shots]
    duplicate_external_keys = sorted(
        key for key, count in Counter(external_keys).items() if count > 1
    )
    if duplicate_external_keys:
        diagnostics.append(
            _diagnostic(
                "blocking",
                "storyboard_duplicate_external_key",
                "标准分镜存在重复 external_key",
                "shots",
                {"external_keys": duplicate_external_keys},
            )
        )

    shot_nos = [shot.shot_no for shot in shots]
    duplicate_shot_nos = sorted(no for no, count in Counter(shot_nos).items() if count > 1)
    if duplicate_shot_nos:
        diagnostics.append(
            _diagnostic(
                "blocking",
                "storyboard_duplicate_shot_no",
                "标准分镜存在重复 shot_no",
                "shots",
                {"shot_nos": duplicate_shot_nos},
            )
        )
    for index in range(1, len(shots)):
        if shots[index].shot_no <= shots[index - 1].shot_no:
            diagnostics.append(
                _diagnostic(
                    "blocking",
                    "storyboard_shot_order_invalid",
                    "shot_no 必须按分镜顺序严格递增",
                    f"shots[{index}].shot_no",
                    {"previous_shot_no": shots[index - 1].shot_no, "shot_no": shots[index].shot_no},
                )
            )

    seen_scene_keys: set[tuple[str, int | None]] = set()
    previous_scene_key: tuple[str, int | None] | None = None
    previous_scene_no: int | None = None
    for index, shot in enumerate(shots):
        scene_key = (shot.scene_id, shot.scene_no)
        if (
            previous_scene_no is not None
            and shot.scene_no is not None
            and shot.scene_no < previous_scene_no
        ):
            diagnostics.append(
                _diagnostic(
                    "blocking",
                    "storyboard_scene_order_invalid",
                    "scene_no 必须按场景顺序不递减",
                    f"shots[{index}].scene_no",
                    {"previous_scene_no": previous_scene_no, "scene_no": shot.scene_no},
                )
            )
        if (
            previous_scene_key is not None
            and scene_key != previous_scene_key
            and scene_key in seen_scene_keys
        ):
            diagnostics.append(
                _diagnostic(
                    "blocking",
                    "storyboard_scene_order_invalid",
                    "场景顺序不能在切换后回到已结束的场景",
                    f"shots[{index}].scene_id",
                    {"scene_id": shot.scene_id, "scene_no": shot.scene_no},
                )
            )
        seen_scene_keys.add(scene_key)
        previous_scene_key = scene_key
        if shot.scene_no is not None:
            previous_scene_no = shot.scene_no

        path = f"shots[{index}]"
        for field in ("start_state", "action", "end_state"):
            if _is_blank(getattr(shot, field)):
                diagnostics.append(
                    _diagnostic(
                        "blocking",
                        f"storyboard_{field}_missing",
                        f"标准分镜缺少 {field}",
                        f"{path}.{field}",
                    )
                )

        for field in ("shot_type", "camera_angle", "camera_move"):
            if _is_blank(getattr(shot, field)):
                diagnostics.append(
                    _diagnostic(
                        "blocking",
                        f"storyboard_{field}_missing",
                        f"标准分镜缺少镜头语言字段 {field}",
                        f"{path}.{field}",
                    )
                )

        shot_duration = shot.duration_sec
        if shot_duration < 5.0 - _TIME_TOLERANCE or shot_duration > 15.0 + _TIME_TOLERANCE:
            diagnostics.append(
                _diagnostic(
                    "blocking",
                    "storyboard_shot_duration_out_of_range",
                    "单镜时长必须在 5-15 秒范围内",
                    f"{path}.duration_sec",
                    {"duration_sec": shot_duration},
                )
            )
        dialogue_chars = sum(len(_dialogue_text(item)) for item in shot.dialogue)
        capacity = 2.5 * shot_duration
        if dialogue_chars > capacity:
            diagnostics.append(
                _diagnostic(
                    "warning",
                    "storyboard_dialogue_capacity",
                    "单镜对白文本量超过 2.5 字/秒 承载，建议拆镜或压缩",
                    f"{path}.dialogue",
                    {"dialogue_chars": dialogue_chars, "capacity_chars": capacity},
                )
            )

        if shot.scene_name and not shot.scene_asset_id:
            diagnostics.append(
                _diagnostic(
                    "blocking",
                    "storyboard_scene_asset_id_missing",
                    "场景名称存在但没有显式 scene_asset_id，不能猜测素材 ID",
                    f"{path}.scene_asset_id",
                    {"scene_name": shot.scene_name},
                )
            )
        for label, names, asset_ids in (
            ("character", shot.character_names, shot.character_asset_ids),
            ("prop", shot.prop_names, shot.prop_asset_ids),
        ):
            if names and len(asset_ids) < len(names):
                diagnostics.append(
                    _diagnostic(
                        "blocking",
                        f"storyboard_{label}_asset_ids_missing",
                        f"{label} 名称存在但缺少显式素材 ID，不能猜测素材 ID",
                        f"{path}.{label}_asset_ids",
                        {"name_count": len(names), "asset_id_count": len(asset_ids)},
                    )
                )

        for dialogue_index, dialogue in enumerate(shot.dialogue):
            line_id = _dialogue_id(dialogue)
            dialogue_path = f"{path}.dialogue[{dialogue_index}]"
            if not line_id:
                diagnostics.append(
                    _diagnostic(
                        "blocking",
                        "storyboard_dialogue_line_id_missing",
                        "对白缺少稳定 dialogue_line_id",
                        f"{dialogue_path}.dialogue_line_id",
                    )
                )
            if isinstance(dialogue, Mapping) and _is_blank(
                _pick(dialogue, "text", "content", "line", default="")
            ):
                diagnostics.append(
                    _diagnostic(
                        "blocking",
                        "storyboard_dialogue_text_missing",
                        "对白缺少 text",
                        f"{dialogue_path}.text",
                    )
                )

        transition = shot.transition_from_prev
        previous = shots[index - 1] if index else None
        if transition == "continuous":
            if previous is None:
                diagnostics.append(
                    _diagnostic(
                        "blocking",
                        "storyboard_continuous_previous_missing",
                        "continuous 镜头必须存在上一镜",
                        f"{path}.transition_from_prev",
                    )
                )
            else:
                if not _has_frame_reference(previous):
                    diagnostics.append(
                        _diagnostic(
                            "blocking",
                            "storyboard_continuous_previous_state_missing",
                            "continuous 镜头缺少上一镜 end_state 或末帧参考",
                            f"{path}.transition_from_prev",
                        )
                    )
                if (
                    previous.end_state
                    and shot.start_state
                    and not _text_matches(previous.end_state, [shot.start_state], strict=True)
                ):
                    diagnostics.append(
                        _diagnostic(
                            "blocking",
                            "storyboard_continuity_state_mismatch",
                            "continuous 镜头的 start_state 未承接上一镜 end_state",
                            f"{path}.start_state",
                            {
                                "previous_end_state": previous.end_state,
                                "start_state": shot.start_state,
                            },
                        )
                    )
                previous_state = _continuity_boundary(previous, "end")
                current_state = _continuity_boundary(shot, "start")
                if previous_state and current_state:
                    differences = _continuity_differences(previous_state, current_state)
                    if differences:
                        diagnostics.append(
                            _diagnostic(
                                "blocking",
                                "storyboard_continuity_structured_state_mismatch",
                                "continuous 镜头的结构化人物、道具或空间状态未承接上一镜",
                                f"{path}.continuity_state.start",
                                {"differences": differences},
                            )
                        )
                elif not previous.continuity_state or not shot.continuity_state:
                    diagnostics.append(
                        _diagnostic(
                            "blocking",
                            "storyboard_continuity_structured_state_missing",
                            "continuous 镜头必须提供可比较的 continuity_state start/end 边界",
                            f"{path}.continuity_state",
                            {
                                "previous_has_state": bool(previous.continuity_state),
                                "shot_has_state": bool(shot.continuity_state),
                            },
                        )
                    )
        elif transition == "elliptical" and _is_blank(shot.next_hook):
            diagnostics.append(
                _diagnostic(
                    "blocking",
                    "storyboard_elliptical_next_hook_missing",
                    "elliptical 镜头必须提供 next_hook",
                    f"{path}.next_hook",
                )
            )
        elif transition == "same_scene_cut" and _references_previous_frame(shot):
            diagnostics.append(
                _diagnostic(
                    "blocking",
                    "storyboard_same_scene_cut_inherits_previous_frame",
                    "same_scene_cut 不得继承上一镜末帧",
                    f"{path}.reference_bindings",
                )
            )
        elif transition == "scene_change" and _references_previous_frame(shot):
            diagnostics.append(
                _diagnostic(
                    "blocking",
                    "storyboard_scene_change_references_previous_shot",
                    "scene_change 不得引用上一镜",
                    f"{path}.reference_bindings",
                )
            )

        if shot.provider_duration_sec is not None and not _duration_matches(
            shot.provider_duration_sec, shot.duration_sec
        ):
            diagnostics.append(
                _diagnostic(
                    "warning",
                    "storyboard_provider_duration_mismatch",
                    "Provider 生成时长与镜头目标时长不一致",
                    f"{path}.provider_duration_sec",
                    {
                        "duration_sec": shot.duration_sec,
                        "provider_duration_sec": shot.provider_duration_sec,
                    },
                )
            )

    dialogue_ids = [
        line_id
        for shot in shots
        for dialogue in shot.dialogue
        if (line_id := _dialogue_id(dialogue))
    ]
    duplicate_dialogue_ids = sorted(
        line_id for line_id, count in Counter(dialogue_ids).items() if count > 1
    )
    if duplicate_dialogue_ids:
        diagnostics.append(
            _diagnostic(
                "blocking",
                "storyboard_duplicate_dialogue_line_id",
                "标准分镜存在重复 dialogue_line_id",
                "shots.dialogue",
                {"dialogue_line_ids": duplicate_dialogue_ids},
            )
        )

    expected_duration = (
        content.target_duration_sec if target_duration_sec is None else target_duration_sec
    )
    if isinstance(expected_duration, bool) or not isinstance(expected_duration, int | float):
        diagnostics.append(
            _diagnostic(
                "blocking",
                "storyboard_target_duration_invalid",
                "目标时长必须是数字",
                "target_duration_sec",
            )
        )
    elif expected_duration <= 0 or not math.isfinite(float(expected_duration)):
        diagnostics.append(
            _diagnostic(
                "blocking",
                "storyboard_target_duration_invalid",
                "目标时长必须是大于 0 的有限数字",
                "target_duration_sec",
            )
        )
    else:
        total_duration = sum(shot.duration_sec for shot in shots)
        if not _duration_matches(total_duration, float(expected_duration)):
            diagnostics.append(
                _diagnostic(
                    "warning",
                    "storyboard_duration_mismatch",
                    "标准分镜总时长与目标时长不一致",
                    "shots",
                    {"shot_total_sec": total_duration, "target_duration_sec": expected_duration},
                )
            )

    if script is not None:
        _script_diagnostics(content, script, diagnostics)
    if rhythm is not None:
        _rhythm_diagnostics(content, rhythm, diagnostics)
    diagnostics.extend(_semantic_consistency_diagnostics(shots))
    return diagnostics


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=True)
    return copy.deepcopy(value)


def _json_text(value: Any) -> str:
    return json.dumps(_json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _dialogue_payload(dialogue: StoryboardDialogue) -> dict[str, Any]:
    if isinstance(dialogue, ScriptDialogue):
        return dialogue.model_dump(mode="json", exclude_none=True)
    return copy.deepcopy(dict(dialogue))


def _action_events(shot: StoryboardShot) -> list[Any]:
    raw = _pick(shot.prompt_intent, "action_events", "actions", "timeline", default=_MISSING)
    if raw is not _MISSING and _usable(raw):
        return _as_list(raw)
    if not shot.action:
        return []
    return [
        {
            "action": shot.action,
            "start_state": shot.start_state,
            "end_state": shot.end_state,
            "duration_sec": shot.duration_sec,
        }
    ]


def storyboard_to_shot_payloads(
    output: StoryboardOutput | Mapping[str, Any],
) -> list[dict[str, Any]]:
    """将阶段 4 产物投影为现有 Shot 的增量字段，不写数据库、不生成媒体。"""

    content = (
        output if isinstance(output, StoryboardOutput) else StoryboardOutput.model_validate(output)
    )
    payloads: list[dict[str, Any]] = []
    for shot in content.shots:
        dialogue_values = [_dialogue_payload(item) for item in shot.dialogue]
        legacy_dialogue = [
            {
                key: value
                for key, value in {
                    "speaker": item.get("speaker", ""),
                    "text": item.get("text", ""),
                }.items()
            }
            for item in dialogue_values
        ]
        continuity = {
            "transition_from_prev": shot.transition_from_prev,
            "start_state": shot.start_state,
            "end_state": shot.end_state,
            "next_hook": shot.next_hook,
            "reference_bindings": copy.deepcopy(shot.reference_bindings),
            "continuity_state": copy.deepcopy(shot.continuity_state),
        }
        metadata = {
            "artifact_meta": content.artifact_meta.model_dump(mode="json"),
            "target_duration_sec": content.target_duration_sec,
            "provider_duration_sec": shot.provider_duration_sec,
            "scene_id": shot.scene_id,
            "scene_no": shot.scene_no,
            "dynamic_level": shot.dynamic_level,
            "dynamic_event_id": shot.dynamic_event_id,
            "plot_event_ids": list(shot.plot_event_ids),
            "continuity_state": copy.deepcopy(shot.continuity_state),
            "source_refs": list(shot.source_refs),
            "prompt_intent": copy.deepcopy(shot.prompt_intent),
        }
        payloads.append(
            {
                "shot_no": shot.shot_no,
                "shot_order": shot.shot_no,
                "source_type": shot.source_type,
                "source_ref": shot.source_ref or (shot.source_refs[0] if shot.source_refs else ""),
                "external_key": shot.external_key,
                "scene_id": shot.scene_id,
                "scene_no": shot.scene_no,
                "scene_name": shot.scene_name,
                "time_weather": shot.time_weather,
                "scene_asset_id": shot.scene_asset_id,
                "character_names": list(shot.character_names),
                "character_ids": list(shot.character_asset_ids),
                "character_asset_ids": list(shot.character_asset_ids),
                "prop_names": list(shot.prop_names),
                "prop_ids": list(shot.prop_asset_ids),
                "prop_asset_ids": list(shot.prop_asset_ids),
                "positions_json": _json_text(shot.positions),
                "start_state": shot.start_state,
                "action": shot.action,
                "action_events_json": _json_text(_action_events(shot)),
                "expressions_json": _json_text(shot.expressions),
                "dialogue_json": _json_text(legacy_dialogue),
                "dialogue_cues_json": _json_text(dialogue_values),
                "sound_cues_json": _json_text(shot.sound_cues),
                "narration": shot.narration,
                "shot_type": shot.shot_type,
                "camera_angle": shot.camera_angle,
                "camera_move": shot.camera_move,
                "duration_sec": shot.duration_sec,
                "provider_duration_sec": shot.provider_duration_sec,
                "end_state": shot.end_state,
                "next_hook": shot.next_hook,
                "transition_from_prev": shot.transition_from_prev,
                "continuity_json": _json_text(continuity),
                "pipeline_metadata_json": _json_text(metadata),
                "frame_description": shot.frame_description,
                "image_prompt": shot.image_prompt,
                "video_prompt": shot.video_prompt,
                "negative_prompt": shot.negative_prompt,
                "reference_bindings_json": _json_text(shot.reference_bindings),
            }
        )
    return payloads


@dataclass(slots=True)
class StoryboardBatchPlan:
    """4-storyboard 分批计划：一个批次覆盖一段连续的剧本场景。

    批次之间只有「上一批末镜状态」这一个衔接点，保证前后分镜连贯；
    各批必须覆盖本批场景的剧情事件与对白行，保证不遗漏剧情。
    """

    batch_index: int
    scene_indexes: list[int]
    scene_ids: list[str]
    start_sec: float
    end_sec: float
    required_plot_event_ids: list[str]
    required_dialogue_ids: list[str]
    focus_characters: list[str]


def batched_shot_count_limits(budget_sec: float) -> tuple[int, int]:
    """由一批的预算法秒数换算镜头数量区间：(建议下限, 硬上限)。

    与 legacy 分镜提取的密度治理同一口径（shot_service._shot_count_limits）：
    单镜最长 15 秒，覆盖 budget 秒至少需要 ceil(budget/15) 镜；
    上限在所需下限之上加 2 镜余量（建立与收束各 1 镜）。
    """
    budget = float(budget_sec)
    if not math.isfinite(budget) or budget <= 0:
        return 1, 3
    lower = max(1, math.ceil(budget / 15.0))
    suggested = lower + 2
    return lower, suggested


def expected_duration_sec(
    content: StoryboardOutput | Mapping[str, Any] | None,
    target_duration_sec: float | None,
) -> float | None:
    """返回分镜应覆盖的目标时长：显式参数优先，否则取产物自带目标时长。"""
    value = target_duration_sec
    if value is None and content is not None:
        if isinstance(content, StoryboardOutput):
            value = content.target_duration_sec
        elif isinstance(content, Mapping):
            value = content.get("target_duration_sec")
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(float(value)) or float(value) <= 0:
        return None
    return float(value)


def _script_total_duration(
    script: ScriptOutput | Mapping[str, Any] | None,
) -> float | None:
    """返回脚本各场次总时长；无脚本或解析失败时返回 None。"""
    content = _script_model(script)
    if content is None:
        return None
    return sum(float(scene.duration_sec) for scene in content.scenes)


def _make_batch_plan(
    script: ScriptOutput,
    scene_indexes: Sequence[int],
    start_sec: float,
) -> StoryboardBatchPlan:
    scenes = [script.scenes[index] for index in scene_indexes]
    duration = sum(float(scene.duration_sec) for scene in scenes)
    dialogue_ids: list[str] = []
    for scene in scenes:
        dialogue_ids.extend(line.dialogue_line_id for line in scene.dialogue)
    plot_ids: list[str] = []
    characters: list[str] = []
    for scene in scenes:
        for event_id in scene.plot_event_ids:
            if event_id not in plot_ids:
                plot_ids.append(event_id)
        for name in scene.characters:
            if name not in characters:
                characters.append(name)
    return StoryboardBatchPlan(
        batch_index=0,
        scene_indexes=list(scene_indexes),
        scene_ids=[scene.scene_id for scene in scenes],
        start_sec=start_sec,
        end_sec=start_sec + duration,
        required_plot_event_ids=plot_ids,
        required_dialogue_ids=dialogue_ids,
        focus_characters=characters,
    )


def plan_storyboard_batches(
    script: ScriptOutput | Mapping[str, Any] | None,
    rhythm: RhythmOutput | Mapping[str, Any] | None = None,
    *,
    max_batch_duration_sec: float = 120.0,
) -> list[StoryboardBatchPlan]:
    """按剧本场景顺序贪心分批；每批累计目标时长不超过上限。

    同一场景的镜头必须落在同一批内（批内全部上下文互相可见，保证连贯）。
    单场景超过上限时单独成批，避免切割场景语义。
    批次时间区间保证覆盖节奏时间轴全长：若剧本场景时长总和短于节奏目标
    时长（两阶段独立生成允许的偏差），把尾段并入最后一批，确保每个节奏段
    都有批次认领，杜绝「无批认领的段只在全局门禁兜底」导致的整章重跑。
    """

    content = _script_model(script)
    if content is None or not content.scenes:
        return []
    limit = max(1.0, float(max_batch_duration_sec))
    plans: list[StoryboardBatchPlan] = []
    cursor = 0.0
    group: list[int] = []
    group_duration = 0.0
    for index, scene in enumerate(content.scenes):
        duration = float(scene.duration_sec)
        room = limit - group_duration
        if group and (duration > room or room <= 0):
            # 当前组已满（再放会超过上限）或单场景本身超上限：
            # 先收掉当前组，再把该场景独立成批（单场景超限不许切割语义）。
            plans.append(_make_batch_plan(content, group, cursor))
            cursor += group_duration
            group = []
            group_duration = 0.0
        group.append(index)
        group_duration += duration
    if group:
        plans.append(_make_batch_plan(content, group, cursor))
    rhythm_content = _rhythm_model(rhythm)
    if plans and rhythm_content is not None:
        coverage_end = sum(float(scene.duration_sec) for scene in content.scenes)
        if rhythm_content.target_duration_sec > coverage_end:
            plans[-1].end_sec = rhythm_content.target_duration_sec
    for index, plan in enumerate(plans, start=1):
        plan.batch_index = index
    return plans


def _scene_payload(scene: ScriptScene) -> dict[str, Any]:
    sound_cues: list[dict[str, Any]] = []
    for cue in scene.sound_cues:
        if isinstance(cue, Mapping):
            sound_cues.append(
                {
                    "kind": _text(_pick(cue, "kind", default="")),
                    "description": _text(_pick(cue, "description", default="")),
                    "start_sec": cue.get("start_sec"),
                    "end_sec": cue.get("end_sec"),
                }
            )
        else:
            sound_cues.append({"description": _text(cue)})
    return {
        "scene_id": scene.scene_id,
        "ordinal": scene.ordinal,
        "scene_heading": scene.scene_heading,
        "time_weather": scene.time_weather,
        "location": scene.location,
        "characters": list(scene.characters),
        "objective": scene.objective,
        "action": scene.action,
        "plot_event_ids": list(scene.plot_event_ids),
        "dialogue": [
            {
                "dialogue_line_id": line.dialogue_line_id,
                "speaker": line.speaker,
                "text": line.text,
                "lip_sync_level": line.lip_sync_level,
                "acting_intent": line.acting_intent,
                "start_sec": line.start_sec,
                "end_sec": line.end_sec,
            }
            for line in scene.dialogue
        ],
        "narration": list(scene.narration),
        "emotion": scene.emotion,
        "dynamic_level": scene.dynamic_level,
        "sound_cues": sound_cues,
        "subtitle_style": scene.subtitle_style,
        "duration_sec": scene.duration_sec,
        "transition_to_next": scene.transition_to_next,
        "source_refs": list(scene.source_refs),
    }


def build_storyboard_batch_payload(
    plan: StoryboardBatchPlan,
    *,
    script: ScriptOutput | Mapping[str, Any] | None,
    rhythm: RhythmOutput | Mapping[str, Any] | None = None,
    previous_shot: StoryboardShot | Mapping[str, Any] | None = None,
    target_duration_sec: float,
    batch_count: int,
    asset_keys_by_scene: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """构造单批分镜的 current_stage_input 数据（只含本批事实，不含正文）。"""

    script_content = _script_model(script)
    rhythm_content = _rhythm_model(rhythm)
    asset_keys = dict(asset_keys_by_scene) if asset_keys_by_scene is not None else {}

    scenes_payload: list[dict[str, Any]] = []
    required_dialogue_lines: list[dict[str, str]] = []
    if script_content is not None:
        for index in plan.scene_indexes:
            if index >= len(script_content.scenes):
                continue
            scene = script_content.scenes[index]
            scene_data = _scene_payload(scene)
            keys = asset_keys.get(scene.scene_id)
            if keys:
                scene_data["required_asset_keys"] = [str(key) for key in keys]
            scenes_payload.append(scene_data)
            required_dialogue_lines.extend(
                {
                    "dialogue_line_id": line.dialogue_line_id,
                    "speaker": line.speaker,
                    "text": line.text,
                }
                for line in scene.dialogue
            )

    segments_payload: list[dict[str, Any]] = []
    if rhythm_content is not None:
        for segment in rhythm_content.timeline:
            if segment.end_sec > plan.start_sec and segment.start_sec < plan.end_sec:
                segments_payload.append(
                    {
                        "segment_id": segment.segment_id,
                        "ordinal": segment.ordinal,
                        "start_sec": segment.start_sec,
                        "end_sec": segment.end_sec,
                        "beat_type": segment.beat_type,
                        "narrative_task": segment.narrative_task,
                        "event_summary": segment.event_summary,
                        "visual_action": segment.visual_action,
                        "focus_characters": list(segment.focus_characters),
                        "dynamic_level": segment.dynamic_level,
                        "strong_dynamic_event_id": segment.strong_dynamic_event_id,
                        "source_refs": list(segment.source_refs),
                    }
                )

    events_payload: list[dict[str, Any]] = []
    if rhythm_content is not None:
        events_payload = [
            {
                "event_id": event.event_id,
                "summary": event.summary,
                "segment_id": event.segment_id,
                "dynamic_level": event.dynamic_level,
                "visual_action": event.visual_action,
            }
            for event in rhythm_content.strong_dynamic_budget.events
        ]

    overlapping_segment_ids: set[str] = {segment["segment_id"] for segment in segments_payload}
    dialogue_suggestions: list[dict[str, Any]] = []
    sound_suggestions: list[dict[str, Any]] = []
    if rhythm_content is not None:
        dialogue_suggestions = [
            {
                "segment_id": suggestion.segment_id,
                "speaker": suggestion.speaker,
                "text": suggestion.text,
                "purpose": suggestion.purpose,
            }
            for suggestion in rhythm_content.dialogue_suggestions
            if suggestion.segment_id in overlapping_segment_ids
        ]
        sound_suggestions = [
            {
                "segment_id": suggestion.segment_id,
                "kind": suggestion.kind,
                "description": suggestion.description,
                "intensity": suggestion.intensity,
            }
            for suggestion in rhythm_content.sound_suggestions
            if suggestion.segment_id in overlapping_segment_ids
        ]

    previous_payload: dict[str, Any] | None = None
    previous = _storyboard_shot_model(previous_shot)
    if previous is not None:
        previous_payload = {
            "scene_id": previous.scene_id,
            "shot_no": previous.shot_no,
            "time_weather": previous.time_weather,
            "action": previous.action,
            "end_state": previous.end_state,
            "transition_from_prev": previous.transition_from_prev,
            "continuity_end": _continuity_boundary(previous, "end"),
        }

    return {
        "stage_key": "4-storyboard",
        "batch_index": plan.batch_index,
        "batch_count": max(batch_count, 1),
        "start_shot_no": 1,
        "target_duration_sec": target_duration_sec,
        "batch_time_range_sec": [plan.start_sec, plan.end_sec],
        "expected_shot_count": list(
            batched_shot_count_limits(max(float(plan.end_sec) - float(plan.start_sec), 0.0))
        ),
        "batch_scenes": scenes_payload,
        "required_plot_event_ids": list(plan.required_plot_event_ids),
        "required_dialogue_ids": list(plan.required_dialogue_ids),
        "required_dialogue_lines": required_dialogue_lines,
        "rhythm_segments": segments_payload,
        "strong_dynamic_events": events_payload,
        "dialogue_suggestions": dialogue_suggestions,
        "sound_suggestions": sound_suggestions,
        "previous_shot": previous_payload,
    }


def source_asset_keys_by_scene(source_analysis: Any) -> dict[str, list[str]]:
    """从阶段 0 源文拆解提取每个场景的必需资产键（scene_id -> keys）。

    仅使用场景显式声明的 required_asset_keys；不猜名、不派生 ID。
    """

    if source_analysis is None:
        return {}
    if isinstance(source_analysis, BaseModel):
        scenes = getattr(source_analysis, "scenes", None)
    elif isinstance(source_analysis, Mapping):
        scenes = source_analysis.get("scenes")
    else:
        return {}
    if not isinstance(scenes, list):
        return {}
    result: dict[str, list[str]] = {}
    for item in scenes:
        if isinstance(item, BaseModel):
            scene_id = getattr(item, "scene_id", None)
            shootability = getattr(item, "shootability", None)
            keys = (
                getattr(shootability, "required_asset_keys", None)
                if shootability is not None
                else None
            )
        elif isinstance(item, Mapping):
            scene_id = item.get("scene_id")
            shootability = item.get("shootability")
            keys = (
                shootability.get("required_asset_keys")
                if isinstance(shootability, Mapping)
                else None
            )
        else:
            continue
        if scene_id and isinstance(keys, list) and keys:
            result[str(scene_id)] = [str(key) for key in keys]
    return result


def _storyboard_shot_model(
    value: StoryboardShot | Mapping[str, Any] | None,
) -> StoryboardShot | None:
    if value is None:
        return None
    try:
        return value if isinstance(value, StoryboardShot) else StoryboardShot.model_validate(value)
    except (ValidationError, TypeError, ValueError):
        return None


def _expected_script_ids(
    script: ScriptOutput | Mapping[str, Any] | None,
) -> tuple[dict[str, str], set[str]]:
    """返回全局剧本对白行 id→原文 与全部剧情事件 id。"""

    content = _script_model(script)
    if content is None:
        return {}, set()
    dialogues = {
        line.dialogue_line_id: line.text.strip()
        for scene in content.scenes
        for line in scene.dialogue
    }
    plot_ids = {event_id for scene in content.scenes for event_id in scene.plot_event_ids}
    return dialogues, plot_ids


def _expected_dynamic_event_ids(
    rhythm: RhythmOutput | Mapping[str, Any] | None,
) -> set[str]:
    content = _rhythm_model(rhythm)
    if content is None:
        return set()
    expected: set[str] = set(content.strong_dynamic_budget.event_ids)
    expected.update(event.event_id for event in content.strong_dynamic_budget.events)
    expected.update(
        segment.strong_dynamic_event_id
        for segment in content.timeline
        if segment.strong_dynamic_event_id
    )
    return expected


def validate_storyboard_batch(
    batch_output: StoryboardOutput | Mapping[str, Any],
    *,
    plan: StoryboardBatchPlan,
    script: ScriptOutput | Mapping[str, Any] | None,
    rhythm: RhythmOutput | Mapping[str, Any] | None = None,
    previous_shot: StoryboardShot | Mapping[str, Any] | None = None,
) -> list[Diagnostic]:
    """校验单批分镜的内容覆盖与批间衔接；只读输入。

    覆盖校验与阶段 4 全局门禁保持同一套判定口径（对白原文、顺序、
    plot_event_ids、强动态事件、continuous 衔接），失败只重试本批。
    """

    try:
        content = (
            batch_output
            if isinstance(batch_output, StoryboardOutput)
            else StoryboardOutput.model_validate(batch_output)
        )
    except (ValidationError, TypeError, ValueError) as exc:
        return [
            _diagnostic(
                "blocking",
                "storyboard_output_invalid",
                "标准分镜不符合阶段 4 schema",
                "shots",
                {"errors": getattr(exc, "errors", lambda: [])()},
            )
        ]

    diagnostics: list[Diagnostic] = []
    shots = list(content.shots)
    if not shots:
        diagnostics.append(
            _diagnostic("blocking", "storyboard_no_shots", "本批标准分镜至少需要一个镜头", "shots")
        )
        return diagnostics

    expected_dialogues, expected_plot_ids = _expected_script_ids(script)
    expected_event_ids = _expected_dynamic_event_ids(rhythm)

    allowed_scene_ids = set(plan.scene_ids)
    # 镜头数量预算：本批按 plan 覆盖时段折算建议区间；硬上限 blocking，
    # 建议区间 warning（仅供模型参考，不参与阻断）。
    batch_budget_sec = max(float(plan.end_sec) - float(plan.start_sec), 0.0)
    lower, suggested_cap = batched_shot_count_limits(batch_budget_sec)
    if len(shots) > suggested_cap:
        diagnostics.append(
            _diagnostic(
                "blocking",
                "storyboard_batch_shot_count_over_budget",
                f"本批镜头数 {len(shots)} 超过 {batch_budget_sec:g} 秒预算允许的 "
                f"{suggested_cap} 个硬上限",
                "shots",
                {
                    "shot_count": len(shots),
                    "budget_sec": batch_budget_sec,
                    "hard_cap": suggested_cap,
                },
            )
        )
    elif len(shots) > lower + 2:
        diagnostics.append(
            _diagnostic(
                "warning",
                "storyboard_batch_shot_count_over_suggested",
                f"本批镜头数 {len(shots)} 高于建议区间上限 {lower + 2}，建议合并相邻镜头",
                "shots",
                {
                    "shot_count": len(shots),
                    "budget_sec": batch_budget_sec,
                    "suggested_cap": lower + 2,
                    "hard_cap": suggested_cap,
                },
            )
        )
    for index, shot in enumerate(shots):
        if shot.scene_id not in allowed_scene_ids:
            diagnostics.append(
                _diagnostic(
                    "blocking",
                    "storyboard_batch_scene_out_of_scope",
                    f"本批镜头超出允许场景：{shot.scene_id}",
                    f"shots[{index}].scene_id",
                    {"allowed_scene_ids": sorted(allowed_scene_ids)},
                )
            )
        shot_path = f"shots[{index}]"
        for field in ("shot_type", "camera_angle", "camera_move"):
            if _is_blank(getattr(shot, field)):
                diagnostics.append(
                    _diagnostic(
                        "blocking",
                        f"storyboard_batch_{field}_missing",
                        f"本批标准分镜缺少镜头语言字段 {field}",
                        f"{shot_path}.{field}",
                    )
                )
        shot_duration = shot.duration_sec
        if shot_duration < 5.0 - _TIME_TOLERANCE or shot_duration > 15.0 + _TIME_TOLERANCE:
            diagnostics.append(
                _diagnostic(
                    "blocking",
                    "storyboard_batch_shot_duration_out_of_range",
                    "本批单镜时长必须在 5-15 秒范围内",
                    f"{shot_path}.duration_sec",
                    {"duration_sec": shot_duration},
                )
            )
        dialogue_chars = sum(len(_dialogue_text(item)) for item in shot.dialogue)
        capacity = 2.5 * shot_duration
        if dialogue_chars > capacity:
            diagnostics.append(
                _diagnostic(
                    "warning",
                    "storyboard_batch_dialogue_capacity",
                    "本批单镜对白文本量超过 2.5 字/秒 承载，建议拆镜或压缩",
                    f"{shot_path}.dialogue",
                    {"dialogue_chars": dialogue_chars, "capacity_chars": capacity},
                )
            )

    actual_plot_ids = {
        event_id for shot in shots for event_id in shot.plot_event_ids if event_id.strip()
    }
    missing_plot_ids = sorted(set(plan.required_plot_event_ids) - actual_plot_ids)
    if missing_plot_ids:
        diagnostics.append(
            _diagnostic(
                "blocking",
                "storyboard_batch_plot_event_missing",
                "本批标准分镜遗漏剧本剧情事件",
                "shots",
                {"missing_plot_event_ids": missing_plot_ids},
            )
        )
    extra_plot_ids = sorted(actual_plot_ids - expected_plot_ids)
    if extra_plot_ids:
        diagnostics.append(
            _diagnostic(
                "blocking",
                "storyboard_batch_plot_event_extra",
                "本批标准分镜包含剧本之外的新增剧情事件",
                "shots",
                {"extra_plot_event_ids": extra_plot_ids},
            )
        )

    actual_dialogues = [
        (_dialogue_id(item), _dialogue_text(item))
        for shot in shots
        for item in shot.dialogue
        if _dialogue_id(item)
    ]
    actual_dialogue_ids = [line_id for line_id, _ in actual_dialogues]
    missing_dialogue_ids = [
        line_id for line_id in plan.required_dialogue_ids if line_id not in actual_dialogue_ids
    ]
    if missing_dialogue_ids:
        diagnostics.append(
            _diagnostic(
                "blocking",
                "storyboard_batch_dialogue_missing",
                "本批标准分镜缺少剧本对白行",
                "shots.dialogue",
                {"missing_dialogue_line_ids": missing_dialogue_ids},
            )
        )
    extra_dialogue_ids = sorted(set(actual_dialogue_ids) - set(expected_dialogues))
    if extra_dialogue_ids:
        diagnostics.append(
            _diagnostic(
                "blocking",
                "storyboard_batch_dialogue_extra",
                "本批标准分镜包含剧本之外的对白行",
                "shots.dialogue",
                {"extra_dialogue_line_ids": extra_dialogue_ids},
            )
        )
    expected_present_order = [
        line_id for line_id in expected_dialogues if line_id in actual_dialogue_ids
    ]
    if actual_dialogue_ids != expected_present_order:
        diagnostics.append(
            _diagnostic(
                "blocking",
                "storyboard_batch_dialogue_order_mismatch",
                "本批标准分镜对白顺序与剧本不一致",
                "shots.dialogue",
                {
                    "script_order": expected_present_order,
                    "storyboard_order": actual_dialogue_ids,
                },
            )
        )
    for line_id, actual_text in actual_dialogues:
        expected_text = expected_dialogues.get(line_id)
        if expected_text is not None and actual_text != expected_text:
            diagnostics.append(
                _diagnostic(
                    "blocking",
                    "storyboard_batch_dialogue_text_mismatch",
                    f"本批标准分镜对白原文被改写：{line_id}",
                    "shots.dialogue",
                    {"script_text": expected_text, "storyboard_text": actual_text},
                )
            )

    # 旁白覆盖：本批场景的每条剧本旁白都必须被本批镜头 narration 保留
    script_content = _script_model(script)
    narration_values = [item for shot in shots for item in (shot.narration,) if _usable(item)]
    expected_narrations: list[str] = []
    if script_content is not None:
        for index in plan.scene_indexes:
            if index >= len(script_content.scenes):
                continue
            expected_narrations.extend(
                item.strip() for item in script_content.scenes[index].narration if item.strip()
            )
    for expected_narration in expected_narrations:
        if _text_matches(expected_narration, narration_values):
            continue
        diagnostics.append(
            _diagnostic(
                "blocking",
                "storyboard_batch_narration_missing",
                f"本批标准分镜缺少剧本旁白：{expected_narration[:20]}",
                "shots.narration",
                {"narration": expected_narration},
            )
        )
    for shot in shots:
        actual = shot.narration.strip()
        if not actual:
            continue
        if any(_same_text(expected, actual) for expected in expected_narrations):
            continue
        diagnostics.append(
            _diagnostic(
                "blocking",
                "storyboard_batch_narration_extra",
                f"本批标准分镜包含剧本之外的旁白：{actual[:20]}",
                f"shots[{shots.index(shot)}].narration",
                {"narration": actual},
            )
        )

    rhythm_content = _rhythm_model(rhythm)
    overlapping_segments: list[Any] = []
    if rhythm_content is not None:
        overlapping_segments = [
            segment
            for segment in rhythm_content.timeline
            if segment.end_sec > plan.start_sec and segment.start_sec < plan.end_sec
        ]
        overlapping_event_ids = {
            segment.strong_dynamic_event_id
            for segment in overlapping_segments
            if segment.strong_dynamic_event_id
        }
        actual_event_ids = {event_id for shot in shots for event_id in _dynamic_event_ids(shot)}
        missing_event_ids = sorted(
            overlapping_event_ids
            - actual_event_ids
            - {event_id for event_id in set(plan.required_plot_event_ids)}
        )
        if missing_event_ids:
            diagnostics.append(
                _diagnostic(
                    "blocking",
                    "storyboard_batch_strong_dynamic_event_missing",
                    "本批标准分镜缺少节奏阶段要求兑现的强动态 event id",
                    "shots",
                    {"missing_event_ids": missing_event_ids},
                )
            )
        extra_event_ids = sorted(actual_event_ids - expected_event_ids)
        if extra_event_ids:
            diagnostics.append(
                _diagnostic(
                    "blocking",
                    "storyboard_batch_strong_dynamic_event_extra",
                    "本批标准分镜包含节奏产物之外的强动态事件",
                    "shots",
                    {"extra_event_ids": extra_event_ids},
                )
            )
        # 节奏段事件摘要必须被本批镜头文本兑现（与全局门禁同一口径）
        values = [item for shot in shots for item in _shot_text_values(shot)]
        action_values = [item for shot in shots for item in _shot_action_values(shot)]
        for segment in overlapping_segments:
            action_fulfilled = not segment.visual_action or _text_matches(
                segment.visual_action, action_values
            )
            content_fulfilled = not segment.event_summary or _text_matches(
                segment.event_summary, values
            )
            task_fulfilled = not segment.narrative_task or _text_matches(
                segment.narrative_task, values
            )
            fulfilled = action_fulfilled and content_fulfilled
            if not segment.visual_action:
                fulfilled = fulfilled and task_fulfilled
            if not fulfilled:
                diagnostics.append(
                    _diagnostic(
                        "blocking",
                        "storyboard_batch_rhythm_event_unfulfilled",
                        "本批标准分镜未兑现节奏时间轴事件",
                        "shots",
                        {
                            "segment_id": segment.segment_id,
                            "event_summary": segment.event_summary,
                        },
                    )
                )

    previous = _storyboard_shot_model(previous_shot)
    first = shots[0]
    if previous is None:
        if first.transition_from_prev == "continuous":
            diagnostics.append(
                _diagnostic(
                    "blocking",
                    "storyboard_batch_continuous_without_previous",
                    "首批发首镜不得使用 continuous 转场（没有上一镜可承接）",
                    "shots[0].transition_from_prev",
                )
            )
    elif first.transition_from_prev == "continuous":
        if not _text_matches(previous.end_state, [first.start_state], strict=True):
            diagnostics.append(
                _diagnostic(
                    "blocking",
                    "storyboard_batch_continuity_state_mismatch",
                    "本批首镜 start_state 未承接上一批末镜 end_state",
                    "shots[0].start_state",
                    {
                        "previous_end_state": previous.end_state,
                        "start_state": first.start_state,
                    },
                )
            )
        previous_state = _continuity_boundary(previous, "end")
        current_state = _continuity_boundary(first, "start")
        if previous_state and current_state:
            differences = _continuity_differences(previous_state, current_state)
            if differences:
                diagnostics.append(
                    _diagnostic(
                        "blocking",
                        "storyboard_batch_continuity_structured_state_mismatch",
                        "本批首镜结构化人物、道具或空间状态未承接上一批末镜",
                        "shots[0].continuity_state.start",
                        {"differences": differences},
                    )
                )
        elif not previous.continuity_state or not first.continuity_state:
            diagnostics.append(
                _diagnostic(
                    "blocking",
                    "storyboard_batch_continuity_structured_state_missing",
                    "continuous 首镜必须提供可比较的 continuity_state start/end 边界",
                    "shots[0].continuity_state",
                    {
                        "previous_has_state": bool(previous.continuity_state),
                        "shot_has_state": bool(first.continuity_state),
                    },
                )
            )
    elif first.transition_from_prev == "elliptical" and _is_blank(first.next_hook):
        diagnostics.append(
            _diagnostic(
                "blocking",
                "storyboard_batch_elliptical_next_hook_missing",
                "本批首镜 elliptical 转场必须提供 next_hook",
                "shots[0].next_hook",
            )
        )

    diagnostics.extend(_semantic_consistency_diagnostics(shots))
    return diagnostics


def merge_storyboard_batches(
    batches: Sequence[StoryboardOutput | Mapping[str, Any]],
    *,
    artifact_meta: ArtifactMeta | Mapping[str, Any],
    target_duration_sec: float,
    run_id: str = "",
    chapter_id: str = "",
) -> StoryboardOutput:
    """把逐批生成的标准分镜合并为全局 StoryboardOutput。

    按批序重排 shot_no/ordinal/external_key，按镜头聚合 scenes；
    各批 LLM 输出的 assumptions/open_questions 汇总进最终产物。
    """

    scope = _text(run_id) or _text(chapter_id)
    merged_shots: list[StoryboardShot] = []
    serial = 1
    assumptions: list[str] = []
    open_questions: list[str] = []
    for batch in batches:
        content = (
            batch if isinstance(batch, StoryboardOutput) else StoryboardOutput.model_validate(batch)
        )
        for shot in content.shots:
            payload = shot.model_dump(mode="python")
            payload["shot_no"] = serial
            payload["ordinal"] = serial
            payload["external_key"] = stable_external_key(scope, shot.scene_id, serial, serial)
            merged_shots.append(StoryboardShot.model_validate(payload))
            serial += 1
        for item in content.assumptions:
            if item not in assumptions:
                assumptions.append(item)
        for item in content.open_questions:
            if item not in open_questions:
                open_questions.append(item)

    scenes: list[dict[str, Any]] = []
    by_key: dict[tuple[str, int | None, str], dict[str, Any]] = {}
    for shot in merged_shots:
        key = (shot.scene_id, shot.scene_no, shot.scene_name)
        scene = by_key.get(key)
        if scene is None:
            scene = {
                "scene_id": shot.scene_id,
                "scene_no": shot.scene_no,
                "scene_name": shot.scene_name,
                "time_weather": shot.time_weather,
                "shot_external_keys": [],
                "duration_sec": 0.0,
            }
            by_key[key] = scene
            scenes.append(scene)
        scene["shot_external_keys"].append(shot.external_key)
        scene["duration_sec"] += shot.duration_sec

    return StoryboardOutput(
        artifact_meta=ArtifactMeta.model_validate(artifact_meta),
        target_duration_sec=target_duration_sec,
        shots=merged_shots,
        scenes=scenes,
        assumptions=assumptions,
        open_questions=open_questions,
    )


__all__ = [
    "StoryboardOutput",
    "StoryboardShot",
    "StoryboardTransition",
    "StoryboardBatchPlan",
    "batched_shot_count_limits",
    "build_storyboard_batch_payload",
    "expected_duration_sec",
    "merge_storyboard_batches",
    "normalize_storyboard_output",
    "plan_storyboard_batches",
    "source_asset_keys_by_scene",
    "stable_external_key",
    "storyboard_to_shot_payloads",
    "validate_storyboard_batch",
    "validate_storyboard_output",
]
