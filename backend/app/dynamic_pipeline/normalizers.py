from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from app.dynamic_pipeline.schemas import (
    CharacterBiosOutput,
    RhythmOutput,
    SourceAnalysisOutput,
)
from app.services.signature_service import calculate_signature

NORMALIZER_VERSION = "dynamic-normalizer-v1"


class NormalizationError(ValueError):
    """Provider 输出无法转换为阶段 canonical schema 时抛出的错误。"""


_MISSING = object()
_ModelT = TypeVar("_ModelT", bound=BaseModel)


def _plain(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _plain(value.model_dump(mode="python"))
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    if isinstance(value, set | frozenset):
        return [_plain(item) for item in sorted(value, key=repr)]
    if isinstance(value, str):
        return value.strip()
    return value


def _decode_json_text(value: str) -> Any:
    text = value.strip().lstrip("\ufeff")
    if text.startswith("```"):
        lines = text.splitlines()
        opening = lines[0].strip().lower() if lines else ""
        if opening not in {"```", "```json"} or len(lines) < 3:
            raise NormalizationError("JSON 代码块格式错误")
        if lines[-1].strip() != "```":
            raise NormalizationError("JSON 代码块缺少结束标记")
        text = "\n".join(lines[1:-1]).strip()
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise NormalizationError("Provider JSON 输出无法解析") from exc


def _decode_raw(value: Any) -> Any:
    if isinstance(value, str):
        return _plain(_decode_json_text(value))
    return _plain(value)


def _prepare_raw(raw: Any) -> dict[str, Any]:
    value = _decode_raw(raw)
    if isinstance(value, Mapping):
        for envelope_key in ("content", "data", "output"):
            if envelope_key in value:
                value = _decode_raw(value[envelope_key])
                break
    if not isinstance(value, Mapping):
        raise NormalizationError("Provider 输出必须是对象或对象 JSON")
    return dict(value)


def _has_value(value: Any) -> bool:
    if value is _MISSING or value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping | list | tuple | set | frozenset):
        return bool(value)
    return True


def _pick(mapping: Mapping[str, Any], *names: str, default: Any = _MISSING) -> Any:
    first_present = _MISSING
    for name in names:
        if name not in mapping:
            continue
        value = mapping[name]
        if first_present is _MISSING:
            first_present = value
        if _has_value(value):
            return value
    if first_present is not _MISSING:
        return first_present
    return default


def _pick_from(mappings: list[Mapping[str, Any]], *names: str) -> Any:
    first_present = _MISSING
    for mapping in mappings:
        value = _pick(mapping, *names, default=_MISSING)
        if value is _MISSING:
            continue
        if first_present is _MISSING:
            first_present = value
        if _has_value(value):
            return value
    return first_present


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _as_list(value: Any) -> list[Any]:
    if value is _MISSING or value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set | frozenset):
        return sorted(value, key=repr)
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [value]


def _string_list(value: Any) -> list[str] | Any:
    if value is _MISSING or value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if not isinstance(value, list | tuple | set | frozenset):
        return value
    values = list(value) if not isinstance(value, set | frozenset) else sorted(value, key=repr)
    result: list[Any] = []
    for item in values:
        if isinstance(item, str):
            text = item.strip()
            if text:
                result.append(text)
        else:
            result.append(item)
    return result


def _text_or_none(value: Any) -> Any:
    if value is _MISSING or value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return value


def _number(value: Any) -> Any:
    if value is _MISSING or value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return value
    return value


def _integer(value: Any, fallback: int) -> Any:
    if value is _MISSING or value is None:
        return fallback
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else value
    if isinstance(value, str):
        text = value.strip()
        try:
            parsed = float(text)
        except ValueError:
            return value
        return int(parsed) if parsed.is_integer() else value
    return value


def _text_param(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NormalizationError(f"{name} 不能为空")
    return value.strip()


def _optional_text_param(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise NormalizationError(f"{name} 必须是字符串")
    value = value.strip()
    if not value:
        return None
    return value


def _importance(value: Any, default: Any = _MISSING) -> Any:
    value = _text_or_none(value)
    if not isinstance(value, str):
        return default if value is None else value
    aliases = {
        "core": "core",
        "supporting": "supporting",
        "ensemble": "ensemble",
        "main": "core",
        "lead": "core",
        "primary": "core",
        "protagonist": "core",
        "主角": "core",
        "核心角色": "core",
        "secondary": "supporting",
        "support": "supporting",
        "minor": "supporting",
        "配角": "supporting",
        "群像": "ensemble",
        "crowd": "ensemble",
        "group": "ensemble",
    }
    return aliases.get(value.lower(), value)


def _shootability_status(value: Any) -> Any:
    if isinstance(value, bool):
        return "shootable" if value else "unshootable"
    if not isinstance(value, str):
        return value
    aliases = {
        "可拍": "shootable",
        "可拍摄": "shootable",
        "ready": "shootable",
        "adapt": "needs_adaptation",
        "需改编": "needs_adaptation",
        "需要改编": "needs_adaptation",
        "不可拍": "unshootable",
        "不可拍摄": "unshootable",
        "not_shootable": "unshootable",
    }
    return aliases.get(value, value)


def _conflict_kind(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    return {
        "冲突": "conflict",
        "转折": "turn",
        "爽点": "payoff",
    }.get(value, value)


def _beat_type(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    return {
        "开场": "hook",
        "钩子": "hook",
        "铺垫": "setup",
        "升级": "escalation",
        "冲突": "conflict",
        "转折": "turn",
        "爽点": "payoff",
        "高潮": "payoff",
        "喘息": "breath",
        "伏笔": "foreshadowing",
        "过渡": "transition",
        "悬念": "cliffhanger",
    }.get(value, value)


def _normalize_source_character(value: Any, *, default_story_function: str = "场景角色") -> Any:
    if isinstance(value, str):
        name = value.strip()
        if not name:
            return None
        return {
            "character_key": name,
            "canonical_name": name,
            "aliases": [],
            "importance": "supporting",
            "story_function": default_story_function,
            "source_refs": [],
        }
    if not isinstance(value, Mapping):
        return value

    key = _text_or_none(
        _pick(value, "character_key", "key", "character_id", "id", "slug", default=_MISSING)
    )
    name = _text_or_none(
        _pick(
            value,
            "canonical_name",
            "name",
            "character_name",
            "display_name",
            default=_MISSING,
        )
    )
    if key is None:
        key = name
    if name is None:
        name = key
    return {
        "character_key": key,
        "canonical_name": name,
        "aliases": _string_list(_pick(value, "aliases", "alias", "other_names", default=_MISSING)),
        "importance": _importance(
            _pick(value, "importance", "role", "priority", default=_MISSING), "supporting"
        ),
        "story_function": _text_or_none(
            _pick(value, "story_function", "function", "plot_role", default=_MISSING)
        )
        or default_story_function,
        "source_refs": _string_list(
            _pick(value, "source_refs", "references", "source_references", default=_MISSING)
        ),
    }


def _normalize_source_characters(value: Any) -> list[Any]:
    result = []
    for item in _as_list(value):
        normalized = _normalize_source_character(item)
        if normalized is not None:
            result.append(normalized)
    return result


def _normalize_shootability(scene: Mapping[str, Any]) -> Any:
    raw = _pick(scene, "shootability", "shootable_info", "production", default=_MISSING)
    if isinstance(raw, Mapping):
        source = raw
    elif isinstance(raw, str) or isinstance(raw, bool):
        source = {"status": raw}
    else:
        source = {}

    status = _pick(
        source,
        "status",
        "shootability_status",
        "production_status",
        default=_MISSING,
    )
    if status is _MISSING:
        status = _pick(scene, "shootability_status", "shootable", "production_status", default=None)
    visible_action = _pick(
        source,
        "visible_action",
        "action",
        "visible_event",
        default=_MISSING,
    )
    if visible_action is _MISSING:
        visible_action = _pick(scene, "visible_action", "action", default=None)
    return {
        "status": _shootability_status(status),
        "visible_action": _text_or_none(visible_action),
        "required_asset_keys": _string_list(
            _pick(
                source,
                "required_asset_keys",
                "required_assets",
                "asset_keys",
                "assets",
                default=_MISSING,
            )
        ),
        "production_risks": _string_list(
            _pick(source, "production_risks", "risks", "risk", default=_MISSING)
        ),
        "adaptation_note": _text_or_none(
            _pick(source, "adaptation_note", "adaptation", "rewrite_note", default=_MISSING)
        ),
    }


def _normalize_source_scene(value: Any, index: int) -> Any:
    if not isinstance(value, Mapping):
        return value

    ordinal = _integer(_pick(value, "ordinal", "scene_no", "no", "index", default=_MISSING), index)
    raw_scene_id = _text_or_none(
        _pick(value, "scene_id", "id", "scene_key", "key", default=_MISSING)
    )
    scene_id = raw_scene_id or f"scene-{ordinal if isinstance(ordinal, int) else index}"
    location = _text_or_none(
        _pick(
            value,
            "location",
            "name",
            "scene_name",
            "setting",
            "location_name",
            default=_MISSING,
        )
    )
    time_weather = _text_or_none(
        _pick(value, "time_weather", "time_and_weather", "time_weather_condition", default=_MISSING)
    )
    if time_weather is None:
        time_value = _text_or_none(_pick(value, "time", "time_of_day", default=_MISSING))
        weather_value = _text_or_none(
            _pick(value, "weather", "weather_condition", default=_MISSING)
        )
        time_weather = "；".join(item for item in (time_value, weather_value) if item) or None

    return {
        "scene_id": scene_id,
        "ordinal": ordinal,
        "location": location,
        "time_weather": time_weather,
        "characters": _normalize_source_characters(
            _pick(value, "characters", "character_list", "cast", default=_MISSING)
        ),
        "narrative_task": _text_or_none(
            _pick(value, "narrative_task", "objective", "task", "story_goal", default=_MISSING)
        ),
        "plot_event": _text_or_none(
            _pick(value, "plot_event", "action", "event", "summary", default=_MISSING)
        ),
        "conflict_or_turn": _text_or_none(
            _pick(
                value,
                "conflict_or_turn",
                "conflict",
                "turn",
                "turning_point",
                "tension",
                default=_MISSING,
            )
        ),
        "visual_focus": _text_or_none(
            _pick(
                value,
                "visual_focus",
                "visual",
                "visual_description",
                "focus",
                "shot_focus",
                default=_MISSING,
            )
        ),
        "estimated_duration_sec": _number(
            _pick(
                value,
                "estimated_duration_sec",
                "duration_sec",
                "duration",
                "estimated_duration",
                default=_MISSING,
            )
        ),
        "shootability": _normalize_shootability(value),
        "source_refs": _string_list(
            _pick(value, "source_refs", "references", "source_references", default=_MISSING)
        ),
    }


def _normalize_conflict_turn(value: Any, index: int) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        return {
            "kind": "conflict",
            "summary": text,
            "scene_id": None,
            "participants": [],
            "source_refs": [],
        }
    if not isinstance(value, Mapping):
        return value
    return {
        "kind": _conflict_kind(_pick(value, "kind", "type", "category", default="conflict")),
        "summary": _text_or_none(
            _pick(value, "summary", "event", "description", "text", default=_MISSING)
        ),
        "scene_id": _text_or_none(_pick(value, "scene_id", "scene", "scene_key", default=_MISSING)),
        "participants": _string_list(
            _pick(value, "participants", "characters", "character_refs", default=_MISSING)
        ),
        "source_refs": _string_list(
            _pick(value, "source_refs", "references", "source_references", default=_MISSING)
        ),
    }


def _normalize_source_content(raw: Mapping[str, Any], **metadata: Any) -> dict[str, Any]:
    scenes_value = _pick(raw, "scenes", "scene_list", "scene", "sequences", default=_MISSING)
    conflict_value = _pick(
        raw,
        "conflict_turns",
        "conflicts",
        "conflict_and_turns",
        "turning_points",
        default=_MISSING,
    )
    return {
        "metadata": metadata,
        "episode_logline": _text_or_none(
            _pick(
                raw,
                "episode_logline",
                "logline",
                "summary",
                "synopsis",
                "one_sentence_summary",
                "episode_summary",
                default=_MISSING,
            )
        ),
        "scenes": [
            normalized
            for index, item in enumerate(_as_list(scenes_value), start=1)
            if (normalized := _normalize_source_scene(item, index)) is not None
        ],
        "character_roster": _normalize_source_characters(
            _pick(
                raw,
                "character_roster",
                "characters",
                "character_list",
                "roster",
                default=_MISSING,
            )
        ),
        "conflict_turns": [
            normalized
            for index, item in enumerate(_as_list(conflict_value), start=1)
            if (normalized := _normalize_conflict_turn(item, index)) is not None
        ],
        "visualizable_content": _string_list(
            _pick(raw, "visualizable_content", "visualizable", "visual_content", default=_MISSING)
        ),
        "hard_to_visualize": _string_list(
            _pick(
                raw,
                "hard_to_visualize",
                "hard_to_visualize_content",
                "difficult_to_visualize",
                "non_visualizable",
                default=_MISSING,
            )
        ),
        "rewrite_requirements": _string_list(
            _pick(
                raw,
                "rewrite_requirements",
                "rewrite_needs",
                "adaptation_requirements",
                "rewrite",
                default=_MISSING,
            )
        ),
        "assumptions": _string_list(_pick(raw, "assumptions", "assumption", default=_MISSING)),
        "open_questions": _string_list(
            _pick(raw, "open_questions", "questions", "unresolved_questions", default=_MISSING)
        ),
        "continuation_outline": _string_list(
            _pick(
                raw,
                "continuation_outline",
                "continuation",
                "next_episode_outline",
                "follow_up_outline",
                default=_MISSING,
            )
        ),
    }


def _facts_from_value(value: Any) -> list[str]:
    if value is _MISSING or value is None:
        return []
    if isinstance(value, str):
        return _string_list(value)
    if isinstance(value, list | tuple | set | frozenset):
        values = value if not isinstance(value, set | frozenset) else sorted(value, key=repr)
        result: list[str] = []
        for item in values:
            result.extend(_facts_from_value(item))
        return result
    if not isinstance(value, Mapping):
        return []

    result: list[str] = []
    for key in (
        "visual_facts",
        "facts",
        "features",
        "description",
        "visual_description",
        "appearance",
        "face",
        "hair",
        "body",
        "costume",
        "accessories",
    ):
        if key in value:
            result.extend(_facts_from_value(value[key]))
    return result


def _unique_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def _normalize_visual_anchor(character: Mapping[str, Any]) -> dict[str, Any]:
    raw_anchor_value = _pick(
        character,
        "visual_anchor",
        "visual_anchor_data",
        "appearance_anchor",
        default=_MISSING,
    )
    anchor = _as_mapping(raw_anchor_value) or {}
    appearance = _pick_from(
        [anchor, character],
        "appearance",
        "visual_description",
        "appearance_description",
    )
    appearance_mapping = _as_mapping(appearance) or {}

    facts: list[str] = []
    for source in (
        _pick(anchor, "visual_facts", "facts", default=_MISSING),
        _pick(character, "visual_facts", "facts", default=_MISSING),
        appearance,
        _pick(character, "visual_description", default=_MISSING),
    ):
        facts.extend(_facts_from_value(source))

    field_aliases = {
        "face": ("face", "face_shape", "facial_features"),
        "hair": ("hair", "hairstyle", "hair_style"),
        "body": ("body", "body_shape", "physique", "figure", "build"),
        "costume": ("costume", "clothing", "outfit", "clothes"),
    }
    direct_values: dict[str, Any] = {}
    for field, aliases in field_aliases.items():
        direct_values[field] = _text_or_none(
            _pick_from([anchor, appearance_mapping, character], *aliases)
        )

    direct_facts = [value for value in direct_values.values() if isinstance(value, str) and value]
    facts = _unique_strings(facts or direct_facts)
    fallback = "；".join(facts) or None
    for field in field_aliases:
        if direct_values[field] is None:
            direct_values[field] = fallback

    makeup = _text_or_none(
        _pick_from(
            [anchor, character],
            "makeup_keyword_string",
            "makeup_keywords",
            "visual_keyword_string",
            "appearance_keywords",
        )
    )
    if isinstance(makeup, list | tuple):
        makeup_values = _string_list(makeup)
        makeup = "|".join(makeup_values) if isinstance(makeup_values, list) else makeup
    if makeup is None:
        makeup = "|".join(facts) or fallback

    return {
        "visual_facts": facts,
        **direct_values,
        "accessories": _string_list(
            _pick_from([anchor, appearance_mapping, character], "accessories", "accessory")
        ),
        "immutable_features": _string_list(
            _pick_from(
                [anchor, character],
                "immutable_features",
                "immutable",
                "identity_features",
            )
        ),
        "forbidden_changes": _string_list(
            _pick_from([anchor, character], "forbidden_changes", "forbidden", "forbidden_variants")
        ),
        "makeup_keyword_string": makeup,
        "variants": _string_list(
            _pick_from([anchor, character], "variants", "variant", "appearance_variants")
        ),
        "current_variant": _text_or_none(
            _pick_from([anchor, character], "current_variant", "active_variant")
        ),
        "approved_reference_ids": _string_list(
            _pick_from(
                [anchor, character],
                "approved_reference_ids",
                "approved_references",
                "reference_ids",
            )
        ),
    }


def _character_items(value: Any) -> list[Any]:
    items = _as_list(value)
    if len(items) != 1 or not isinstance(items[0], Mapping):
        return items
    candidate = items[0]
    known_keys = {
        "basic_info",
        "character_key",
        "canonical_name",
        "name",
        "background",
        "backstory",
        "appearance",
        "visual_anchor",
        "personality",
    }
    if any(key in candidate for key in known_keys):
        return items
    if candidate and all(isinstance(item, Mapping) for item in candidate.values()):
        result = []
        for key, item in candidate.items():
            record = dict(item)
            record.setdefault("character_key", key)
            result.append(record)
        return result
    return items


def _normalize_behavior_evidence(character: Mapping[str, Any]) -> dict[str, Any]:
    raw_evidence = _pick(
        character,
        "behavior_evidence",
        "behavior_evidence_data",
        "acting_evidence",
        "performance_evidence",
        default=_MISSING,
    )
    evidence = _as_mapping(raw_evidence) or {}
    maps = [evidence, character]
    return {
        "trigger": _text_or_none(
            _pick_from(
                maps,
                "trigger",
                "behavior_trigger",
                "trigger_condition",
                "reaction_trigger",
                "触发",
                "触发条件",
            )
        )
        or "",
        "action": _text_or_none(
            _pick_from(
                maps,
                "action",
                "behavior_action",
                "visible_action",
                "response_action",
                "动作",
                "行为",
            )
        )
        or "",
        "expression": _text_or_none(
            _pick_from(maps, "expression", "facial_expression", "emotion", "表情")
        )
        or "",
        "voice": _text_or_none(
            _pick_from(
                maps,
                "voice",
                "voice_evidence",
                "voice_style",
                "speech_style",
                "sound",
                "声音",
                "语气",
            )
        )
        or "",
        "source_refs": _string_list(
            _pick_from(maps, "source_refs", "source_ref", "references", "source_references")
        ),
    }


def _normalize_character(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return value
    basic_value = _pick(value, "basic_info", "basic", "profile", default=_MISSING)
    basic = _as_mapping(basic_value) or {}
    maps = [basic, value]

    key = _text_or_none(_pick_from(maps, "character_key", "key", "character_id", "id", "slug"))
    name = _text_or_none(
        _pick_from(maps, "canonical_name", "name", "character_name", "display_name")
    )
    key = key or name
    name = name or key
    importance = _importance(_pick_from(maps, "importance", "role", "priority", "level"), _MISSING)

    return {
        "basic_info": {
            "character_key": key,
            "canonical_name": name,
            "aliases": _string_list(_pick_from(maps, "aliases", "alias", "other_names")),
            "importance": importance,
            "identity": _text_or_none(
                _pick_from(maps, "identity", "identity_description", "occupation")
            )
            or "",
            "age": _text_or_none(_pick_from(maps, "age", "age_description")) or "",
            "gender": _text_or_none(_pick_from(maps, "gender", "sex")) or "",
        },
        "background": _text_or_none(
            _pick_from(maps, "background", "backstory", "history", "origin", "core_background")
        ),
        "personality_behavior_logic": _text_or_none(
            _pick_from(
                maps,
                "personality_behavior_logic",
                "personality",
                "behavior_logic",
                "behavior",
                "personality_logic",
            )
        ),
        "dynamic_comic_setting": _text_or_none(
            _pick_from(
                maps,
                "dynamic_comic_setting",
                "dynamic_comic_settings",
                "comic_setting",
                "dynamic_setting",
                "comic_dynamics",
            )
        ),
        "plot_relation": _text_or_none(
            _pick_from(
                maps,
                "plot_relation",
                "plot_relationship",
                "relationship",
                "plot_role",
                "story_relation",
            )
        ),
        "visual_anchor": _normalize_visual_anchor(value),
        "behavior_evidence": _normalize_behavior_evidence(value),
    }


def _normalize_ensemble_anchor(value: Any, index: int) -> Any:
    if isinstance(value, str):
        name = value.strip()
        if not name:
            return None
        return {
            "ensemble_key": name,
            "canonical_name": name,
            "visual_facts": [],
            "usage_notes": "",
        }
    if not isinstance(value, Mapping):
        return value
    name = _text_or_none(
        _pick(value, "canonical_name", "name", "group_name", "ensemble_name", default=_MISSING)
    )
    key = _text_or_none(_pick(value, "ensemble_key", "group_key", "key", "id", default=_MISSING))
    name = name or key
    key = key or name or f"ensemble-{index}"
    facts = _facts_from_value(
        _pick(value, "visual_facts", "facts", "appearance", "visual_description", default=_MISSING)
    )
    return {
        "ensemble_key": key,
        "canonical_name": name,
        "visual_facts": _unique_strings(facts),
        "usage_notes": _text_or_none(
            _pick(value, "usage_notes", "usage", "notes", default=_MISSING)
        )
        or "",
    }


def _ensemble_items(value: Any) -> list[Any]:
    items = _as_list(value)
    if len(items) == 1 and isinstance(items[0], Mapping):
        candidate = items[0]
        known_keys = {"ensemble_key", "group_key", "canonical_name", "name", "visual_facts"}
        if not any(key in candidate for key in known_keys) and all(
            isinstance(item, Mapping) for item in candidate.values()
        ):
            return [dict(item, ensemble_key=key) for key, item in candidate.items()]
    return items


def _normalize_character_content(raw: Mapping[str, Any]) -> dict[str, Any]:
    recognized_keys = {
        "characters",
        "bios",
        "character_bios",
        "character_list",
        "ensemble_anchors",
        "ensemble",
        "group_anchors",
        "groups",
        "group",
        "unresolved_character_refs",
        "unresolved_characters",
        "unresolved",
        "unknown_characters",
    }
    if not recognized_keys.intersection(raw):
        raise NormalizationError("人物小传输出缺少必要内容")
    character_value = _pick(
        raw,
        "characters",
        "bios",
        "character_bios",
        "character_list",
        default=_MISSING,
    )
    ensemble_value = _pick(
        raw,
        "ensemble_anchors",
        "ensemble",
        "group_anchors",
        "groups",
        "group",
        default=_MISSING,
    )
    return {
        "characters": [
            normalized
            for item in _character_items(character_value)
            if (normalized := _normalize_character(item)) is not None
        ],
        "ensemble_anchors": [
            normalized
            for index, item in enumerate(_ensemble_items(ensemble_value), start=1)
            if (normalized := _normalize_ensemble_anchor(item, index)) is not None
        ],
        "unresolved_character_refs": _string_list(
            _pick(
                raw,
                "unresolved_character_refs",
                "unresolved_characters",
                "unresolved",
                "unknown_characters",
                default=_MISSING,
            )
        ),
    }


def _normalize_dynamic_event(value: Any, index: int, default_segment_id: Any = None) -> Any:
    if isinstance(value, str):
        summary = value.strip()
        if not summary:
            return None
        return {
            "event_id": f"dynamic-{index}",
            "summary": summary,
            "segment_id": default_segment_id,
            "dynamic_level": 1.0,
            "visual_action": "",
            "source_refs": [],
        }
    if not isinstance(value, Mapping):
        return value
    event_id = (
        _text_or_none(_pick(value, "event_id", "id", "key", "dynamic_id", default=_MISSING))
        or f"dynamic-{index}"
    )
    return {
        "event_id": event_id,
        "summary": _text_or_none(
            _pick(
                value, "summary", "event_summary", "event", "description", "text", default=_MISSING
            )
        ),
        "segment_id": _text_or_none(
            _pick(value, "segment_id", "segment", "segment_key", default=default_segment_id)
        ),
        "dynamic_level": _number(_pick(value, "dynamic_level", "intensity", "level", default=1.0)),
        "visual_action": _text_or_none(
            _pick(value, "visual_action", "action", "visible_action", default="")
        )
        or "",
        "source_refs": _string_list(
            _pick(value, "source_refs", "references", "source_references", default=_MISSING)
        ),
    }


def _normalize_rhythm_segment(value: Any, index: int, previous_end: Any) -> tuple[Any, Any]:
    if not isinstance(value, Mapping):
        return value, previous_end
    ordinal = _integer(
        _pick(value, "ordinal", "segment_no", "no", "index", default=_MISSING), index
    )
    raw_segment_id = _text_or_none(
        _pick(value, "segment_id", "id", "segment_key", "key", default=_MISSING)
    )
    segment_id = raw_segment_id or f"segment-{ordinal if isinstance(ordinal, int) else index}"
    start = _number(
        _pick(
            value,
            "start_sec",
            "start",
            "start_time",
            "from_sec",
            "time_start",
            default=_MISSING,
        )
    )
    if start is None:
        start = previous_end if previous_end is not None else 0.0
    end = _number(
        _pick(
            value,
            "end_sec",
            "end",
            "end_time",
            "to_sec",
            "time_end",
            default=_MISSING,
        )
    )
    duration = _number(_pick(value, "duration", "duration_sec", "length", default=_MISSING))
    if end is None and isinstance(start, int | float) and not isinstance(start, bool):
        if isinstance(duration, int | float) and not isinstance(duration, bool):
            end = start + duration

    normalized = {
        "segment_id": segment_id,
        "ordinal": ordinal,
        "start_sec": start,
        "end_sec": end,
        "beat_type": _beat_type(
            _pick(value, "beat_type", "beat", "beat_kind", "type", default=_MISSING)
        ),
        "narrative_task": _text_or_none(
            _pick(value, "narrative_task", "task", "story_task", "objective", default=_MISSING)
        ),
        "event_summary": _text_or_none(
            _pick(value, "event_summary", "event", "summary", "beat_summary", default=_MISSING)
        ),
        "visual_action": _text_or_none(
            _pick(
                value,
                "visual_action",
                "action",
                "visual",
                "action_description",
                default=_MISSING,
            )
        ),
        "focus_characters": _string_list(
            _pick(
                value,
                "focus_characters",
                "characters",
                "character_refs",
                "subjects",
                default=_MISSING,
            )
        ),
        "dynamic_level": _number(
            _pick(
                value, "dynamic_level", "intensity", "dynamic", "intensity_level", default=_MISSING
            )
        ),
        "strong_dynamic_event_id": _text_or_none(
            _pick(
                value,
                "strong_dynamic_event_id",
                "dynamic_event_id",
                "strong_event_id",
                default=_MISSING,
            )
        ),
        "source_refs": _string_list(
            _pick(value, "source_refs", "references", "source_references", default=_MISSING)
        ),
    }
    next_end = end if isinstance(end, int | float) and not isinstance(end, bool) else previous_end
    return normalized, next_end


def _normalize_beat(value: Any, index: int) -> Any:
    if isinstance(value, str):
        return value.strip()
    return _normalize_dynamic_event(value, index)


def _normalize_budget(raw: Mapping[str, Any]) -> dict[str, Any]:
    budget_value = _pick(
        raw,
        "strong_dynamic_budget",
        "strong_dynamic_events",
        "dynamic_budget",
        "strong_dynamic",
        "dynamic_events",
        "budget",
        default=_MISSING,
    )
    budget = _as_mapping(budget_value)
    if budget is None:
        event_values = _as_list(budget_value)
        budget = {}
    else:
        event_values = _pick(
            budget,
            "events",
            "strong_dynamic_events",
            "dynamic_events",
            "items",
            default=_MISSING,
        )
    events = [
        normalized
        for index, item in enumerate(_as_list(event_values), start=1)
        if (normalized := _normalize_dynamic_event(item, index)) is not None
    ]
    event_ids = _string_list(
        _pick(budget, "event_ids", "ids", "dynamic_event_ids", default=_MISSING)
    )
    if not event_ids and isinstance(events, list):
        event_ids = [item["event_id"] for item in events if isinstance(item, Mapping)]
    max_events = _integer(
        _pick(budget, "max_events", "max", "count", "limit", default=_MISSING),
        len(events),
    )
    return {
        "max_events": max_events,
        "event_ids": event_ids,
        "events": events,
    }


def _normalize_dialogue(value: Any) -> Any:
    if isinstance(value, str):
        return {
            "segment_id": None,
            "speaker": "",
            "text": value.strip(),
            "purpose": "",
            "source_refs": [],
        }
    if not isinstance(value, Mapping):
        return value
    return {
        "segment_id": _text_or_none(
            _pick(value, "segment_id", "segment", "segment_key", default=None)
        ),
        "speaker": _text_or_none(_pick(value, "speaker", "character", "name", default="")) or "",
        "text": _text_or_none(
            _pick(value, "text", "line", "content", "dialogue", default=_MISSING)
        ),
        "purpose": _text_or_none(_pick(value, "purpose", "intent", "function", default="")) or "",
        "source_refs": _string_list(
            _pick(value, "source_refs", "references", "source_references", default=_MISSING)
        ),
    }


def _normalize_sound(value: Any) -> Any:
    if isinstance(value, str):
        return {
            "segment_id": None,
            "kind": "sound",
            "description": value.strip(),
            "intensity": None,
            "source_refs": [],
        }
    if not isinstance(value, Mapping):
        return value
    return {
        "segment_id": _text_or_none(
            _pick(value, "segment_id", "segment", "segment_key", default=None)
        ),
        "kind": _text_or_none(_pick(value, "kind", "type", "category", default="sound")) or "sound",
        "description": _text_or_none(
            _pick(value, "description", "sound", "event", "summary", "text", default=_MISSING)
        ),
        "intensity": _number(_pick(value, "intensity", "dynamic_level", "level", default=None)),
        "source_refs": _string_list(
            _pick(value, "source_refs", "references", "source_references", default=_MISSING)
        ),
    }


def _normalize_rhythm_content(raw: Mapping[str, Any], target_duration_sec: float) -> dict[str, Any]:
    timeline_value = _pick(
        raw, "timeline", "segments", "time_segments", "rhythm_timeline", default=_MISSING
    )
    timeline: list[Any] = []
    previous_end = None
    for index, item in enumerate(_as_list(timeline_value), start=1):
        normalized, previous_end = _normalize_rhythm_segment(item, index, previous_end)
        timeline.append(normalized)

    core_payoff = _pick(raw, "core_payoff", "payoff", "core_beat", "main_payoff", default=_MISSING)
    small_beats = _pick(raw, "small_beats", "minor_beats", "small_payoffs", default=_MISSING)
    dialogue_value = _pick(
        raw,
        "dialogue_suggestions",
        "dialogue",
        "dialogues",
        "dialogue_cues",
        "lines",
        default=_MISSING,
    )
    sound_value = _pick(
        raw,
        "sound_suggestions",
        "sound",
        "sounds",
        "sound_cues",
        "audio_suggestions",
        default=_MISSING,
    )
    return {
        "target_duration_sec": target_duration_sec,
        "timeline": timeline,
        "core_payoff": _normalize_beat(core_payoff, 1),
        "small_beats": [
            normalized
            for index, item in enumerate(_as_list(small_beats), start=1)
            if (normalized := _normalize_beat(item, index)) is not None
        ],
        "strong_dynamic_budget": _normalize_budget(raw),
        "foreshadowing": _string_list(
            _pick(raw, "foreshadowing", "foreshadow", "setups", default=_MISSING)
        ),
        "open_space": _string_list(
            _pick(raw, "open_space", "open_spaces", "留白", default=_MISSING)
        ),
        "dialogue_suggestions": [
            normalized
            for item in _as_list(dialogue_value)
            if (normalized := _normalize_dialogue(item)) is not None
        ],
        "sound_suggestions": [
            normalized
            for item in _as_list(sound_value)
            if (normalized := _normalize_sound(item)) is not None
        ],
        "assumptions": _string_list(_pick(raw, "assumptions", "assumption", default=_MISSING)),
    }


def _validate(model_type: type[_ModelT], payload: Mapping[str, Any], label: str) -> _ModelT:
    try:
        return model_type.model_validate(payload)
    except ValidationError as exc:
        raise NormalizationError(f"{label} 结构校验失败") from exc


def _build_output(
    model_type: type[_ModelT],
    content: Mapping[str, Any],
    *,
    schema_version: str,
    input_signature: Any,
    source_attempt: Any,
    provider_id: Any,
    provider_model: Any,
) -> _ModelT:
    input_signature = _text_param(input_signature, "input_signature")
    provider_id = _optional_text_param(provider_id, "provider_id")
    provider_model = _optional_text_param(provider_model, "provider_model")
    if (
        isinstance(source_attempt, bool)
        or not isinstance(source_attempt, int)
        or source_attempt < 1
    ):
        raise NormalizationError("source_attempt 必须是大于等于 1 的整数")

    placeholder_meta = {
        "schema_version": schema_version,
        "normalizer_version": NORMALIZER_VERSION,
        "input_signature": input_signature,
        "content_signature": "pending",
        "source_attempt": source_attempt,
        "provider_id": provider_id,
        "provider_model": provider_model,
    }
    preliminary_payload = dict(content)
    preliminary_payload["artifact_meta"] = placeholder_meta
    preliminary = _validate(model_type, preliminary_payload, model_type.__name__)
    canonical_content = preliminary.model_dump(mode="json", exclude={"artifact_meta"})
    # input_signature 是归一化上下文的一部分，变化时必须产生新的内容签名。
    content_signature = calculate_signature(canonical_content, input_signature)
    final_payload = preliminary.model_dump(mode="python")
    final_payload["artifact_meta"]["content_signature"] = content_signature
    return _validate(model_type, final_payload, model_type.__name__)


def normalize_source_output(
    raw: Any,
    *,
    input_signature: str,
    source_kind: Any,
    title: str,
    chapter_title: str,
    target_duration_sec: float,
    aspect_ratio: str,
    source_signature: str,
    source_attempt: int = 1,
    provider_id: str | None = None,
    provider_model: str | None = None,
) -> SourceAnalysisOutput:
    """把阶段 0 的 Provider 输出归一为严格的源文拆解模型。"""
    try:
        raw_mapping = _prepare_raw(raw)
        metadata = {
            "source_kind": source_kind,
            "title": _text_param(title, "title"),
            "chapter_title": _text_param(chapter_title, "chapter_title"),
            "source_signature": _text_param(source_signature, "source_signature"),
            "target_duration_sec": target_duration_sec,
            "aspect_ratio": _text_param(aspect_ratio, "aspect_ratio"),
        }
        content = _normalize_source_content(raw_mapping, **metadata)
        return _build_output(
            SourceAnalysisOutput,
            content,
            schema_version="dynamic-source-v1",
            input_signature=input_signature,
            source_attempt=source_attempt,
            provider_id=provider_id,
            provider_model=provider_model,
        )
    except NormalizationError:
        raise
    except (AttributeError, IndexError, KeyError, OverflowError, TypeError, ValueError) as exc:
        raise NormalizationError("源文拆解输出归一化失败") from exc


def normalize_character_bios_output(
    raw: Any,
    *,
    input_signature: str,
    source_attempt: int = 1,
    provider_id: str | None = None,
    provider_model: str | None = None,
) -> CharacterBiosOutput:
    """把阶段 1 的 Provider 输出归一为人物小传模型。"""
    try:
        content = _normalize_character_content(_prepare_raw(raw))
        return _build_output(
            CharacterBiosOutput,
            content,
            schema_version="dynamic-character-bios-v1",
            input_signature=input_signature,
            source_attempt=source_attempt,
            provider_id=provider_id,
            provider_model=provider_model,
        )
    except NormalizationError:
        raise
    except (AttributeError, IndexError, KeyError, OverflowError, TypeError, ValueError) as exc:
        raise NormalizationError("人物小传输出归一化失败") from exc


def normalize_rhythm_output(
    raw: Any,
    *,
    input_signature: str,
    target_duration_sec: float,
    source_attempt: int = 1,
    provider_id: str | None = None,
    provider_model: str | None = None,
) -> RhythmOutput:
    """把阶段 2 的 Provider 输出归一为节奏与爽点模型。"""
    try:
        content = _normalize_rhythm_content(_prepare_raw(raw), target_duration_sec)
        return _build_output(
            RhythmOutput,
            content,
            schema_version="dynamic-rhythm-v1",
            input_signature=input_signature,
            source_attempt=source_attempt,
            provider_id=provider_id,
            provider_model=provider_model,
        )
    except NormalizationError:
        raise
    except (AttributeError, IndexError, KeyError, OverflowError, TypeError, ValueError) as exc:
        raise NormalizationError("节奏输出归一化失败") from exc


def normalize_stage_output(
    stage_key: str, raw: Any, **kwargs: Any
) -> SourceAnalysisOutput | CharacterBiosOutput | RhythmOutput:
    """按稳定阶段键分发归一化，Phase 2A 之外的阶段不会被静默接受。"""
    required_kwargs = {
        "0-source": (
            "input_signature",
            "source_kind",
            "title",
            "chapter_title",
            "target_duration_sec",
            "aspect_ratio",
            "source_signature",
        ),
        "1-character-bios": ("input_signature",),
        "2-rhythm": ("input_signature", "target_duration_sec"),
    }
    if not isinstance(stage_key, str) or stage_key not in required_kwargs:
        raise NormalizationError(f"不支持的阶段键：{stage_key}")
    missing = [name for name in required_kwargs[stage_key] if name not in kwargs]
    if missing:
        raise NormalizationError(f"阶段 {stage_key} 缺少必要参数：{', '.join(missing)}")
    try:
        if stage_key == "0-source":
            return normalize_source_output(raw, **kwargs)
        if stage_key == "1-character-bios":
            return normalize_character_bios_output(raw, **kwargs)
        return normalize_rhythm_output(raw, **kwargs)
    except TypeError as exc:
        raise NormalizationError(f"阶段 {stage_key} 参数无效") from exc
