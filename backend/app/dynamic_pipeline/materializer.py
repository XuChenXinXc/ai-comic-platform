"""Phase 4.2 的纯 canonical 物化合同。

本模块只把已经确认的阶段产物转换为后续事务可以使用的 payload。它不持有
Database，不查询已有记录，也不执行 insert/update；revision 的实际并发校验由
调用方在同一个事务中完成。
"""

from __future__ import annotations

import copy
import json
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from app.dynamic_pipeline.schemas import (
    CharacterBiosOutput,
    Diagnostic,
    ScriptOutput,
    SourceAnalysisOutput,
)
from app.dynamic_pipeline.script_stage import script_output_to_legacy_screenplay
from app.dynamic_pipeline.storyboard_stage import (
    StoryboardOutput,
    storyboard_to_shot_payloads,
)
from app.dynamic_pipeline.visual_anchor import character_bio_to_anchor
from app.schemas import NovelAnalysisOutput
from app.services.signature_service import calculate_signature, canonical_json, screenplay_signature

_MISSING = object()
_MATERIALIZER_VERSION = "dynamic-materializer-v1"
_MATERIALIZABLE_STAGES = frozenset(
    {"0-source", "1-character-bios", "2-rhythm", "3-script", "4-storyboard"}
)
_LEGACY_ANALYSIS_FIELDS = (
    "summary",
    "genre",
    "time_period",
    "locations",
    "worldview",
    "main_characters",
    "character_relations",
    "core_conflict",
    "plot_structure",
    "emotion_curve",
    "foreshadowing",
    "visualizable_content",
    "hard_to_visualize",
    "production_risks",
)


def _plain(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return copy.deepcopy(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return copy.deepcopy(value)


def _root(value: Any) -> dict[str, Any]:
    value = _plain(value)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise TypeError("阶段产物必须是对象或对象 JSON") from exc
    if not isinstance(value, Mapping):
        raise TypeError("阶段产物必须是对象")
    for key in ("content", "data", "output"):
        nested = value.get(key, _MISSING)
        if isinstance(nested, Mapping):
            return copy.deepcopy(dict(nested))
    return copy.deepcopy(dict(value))


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    return ""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _items(value: Any) -> list[Any]:
    if value is _MISSING or value is None:
        return []
    if isinstance(value, list | tuple):
        return copy.deepcopy(list(value))
    if isinstance(value, set | frozenset):
        return copy.deepcopy(sorted(value, key=repr))
    return [copy.deepcopy(value)]


def _strings(value: Any) -> list[str]:
    result: list[str] = []
    for item in _items(value):
        if isinstance(item, Mapping):
            item = (
                item.get("name")
                or item.get("canonical_name")
                or item.get("character")
                or item.get("text")
                or item.get("value")
            )
        text = _text(item)
        if text and text not in result:
            result.append(text)
    return result


def _first(mapping: Mapping[str, Any], *keys: str, default: Any = _MISSING) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return default


def _diag(
    code: str,
    message: str,
    path: str | None = None,
    details: Mapping[str, Any] | None = None,
    *,
    level: str = "blocking",
) -> Diagnostic:
    return Diagnostic(
        level=level,  # type: ignore[arg-type]
        code=code,
        message=message,
        path=path,
        details=copy.deepcopy(dict(details or {})),
    )


class MaterializationError(ValueError):
    """阶段产物不能无歧义转换为 canonical payload。"""

    def __init__(self, message: str, diagnostics: Iterable[Diagnostic] = ()) -> None:
        self.diagnostics = list(diagnostics)
        super().__init__(message)


class MaterializationConflict(ValueError):
    """调用方提供的 expected revision 与事务内当前 revision 不一致。"""

    def __init__(
        self,
        message: str = "canonical 内容在物化期间已被修改",
        *,
        expected_revision: int | None = None,
        actual_revision: int | None = None,
        diagnostics: Iterable[Diagnostic] = (),
    ) -> None:
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision
        self.expected = expected_revision
        self.actual = actual_revision
        self.diagnostics = list(diagnostics)
        super().__init__(message)


@dataclass
class MaterializationResult:
    stage_key: str
    chapter_id: str
    project_id: str
    created_asset_ids: list[str] = field(default_factory=list)
    updated_asset_ids: list[str] = field(default_factory=list)
    shot_payloads: list[dict[str, Any]] = field(default_factory=list)
    canonical_payload: dict[str, Any] = field(default_factory=dict)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    input_signature: str = ""
    revision: int = 0


class MaterializationPlan(list[dict[str, Any]]):
    """保持 list 兼容，同时让后续事务可以读取 builder 诊断。"""

    def __init__(
        self,
        values: Iterable[dict[str, Any]] = (),
        *,
        diagnostics: Iterable[Diagnostic] = (),
    ) -> None:
        super().__init__(values)
        self.diagnostics = list(diagnostics)


def _validate_id(value: Any, name: str) -> str:
    text = _text(value)
    if not text:
        raise MaterializationError(
            f"{name} 不能为空",
            [_diag("materialization_missing_identifier", f"{name} 不能为空", name)],
        )
    return text


def _validate_expected_revision(expected_revision: Any) -> int:
    if type(expected_revision) is not int or expected_revision < 1:
        raise MaterializationError(
            "expected_revision 必须是大于等于 1 的整数",
            [
                _diag(
                    "materialization_invalid_expected_revision",
                    "expected_revision 必须是大于等于 1 的整数",
                    "expected_revision",
                )
            ],
        )
    return expected_revision


def _validate_source_signature(source_signature: Any) -> str:
    text = _text(source_signature)
    if not text:
        raise MaterializationError(
            "source_signature 不能为空",
            [_diag("materialization_missing_source_signature", "source_signature 不能为空")],
        )
    return text


def _validation_error(label: str, exc: Exception) -> MaterializationError:
    errors = exc.errors() if isinstance(exc, ValidationError) else []
    return MaterializationError(
        f"{label} 不符合物化合同",
        [
            _diag(
                "materialization_invalid_output",
                f"{label} 不符合物化合同",
                details={"errors": errors},
            )
        ],
    )


def _model(value: Any, model_type: type[BaseModel], label: str) -> BaseModel:
    if isinstance(value, model_type):
        return value.model_copy(deep=True)
    try:
        return model_type.model_validate(_root(value))
    except (TypeError, ValueError, ValidationError) as exc:
        raise _validation_error(label, exc) from exc


def _input_signature(value: Any, fallback: str) -> str:
    try:
        root = _root(value)
    except (TypeError, ValueError):
        return _validate_source_signature(fallback)
    artifact_meta = _mapping(root.get("artifact_meta"))
    return (
        _text(root.get("input_signature"))
        or _text(artifact_meta.get("input_signature"))
        or _validate_source_signature(fallback)
    )


def _source_validation_diagnostics(root: Mapping[str, Any]) -> list[Diagnostic]:
    validation_root = copy.deepcopy(dict(root))
    # Mapping 输入允许同时携带已经准备好的 legacy 字段和物化 provenance；这些
    # 字段不是 SourceAnalysisOutput 的输入合同，但不能因此阻断确定性投影。
    ignored_root_fields = {
        *_LEGACY_ANALYSIS_FIELDS,
        "input_signature",
        "source_signature",
        "project_id",
        "chapter_id",
        "run_id",
        "stage_key",
        "revision",
        "provenance",
        "materialized",
        "content_json",
        "own_signature",
    }
    for key in ignored_root_fields:
        validation_root.pop(key, None)
    metadata = validation_root.get("metadata")
    if isinstance(metadata, Mapping):
        validation_root["metadata"] = {
            key: value
            for key, value in metadata.items()
            if key
            in {
                "source_kind",
                "title",
                "chapter_title",
                "source_signature",
                "target_duration_sec",
                "aspect_ratio",
            }
        }
    try:
        SourceAnalysisOutput.model_validate(validation_root)
    except ValidationError as exc:
        diagnostics: list[Diagnostic] = []
        for error in exc.errors():
            location = tuple(str(item) for item in error.get("loc", ()))
            path = ".".join(location) or None
            top_level = location[0] if location else "source_output"
            if error.get("type") == "missing":
                code = {
                    "episode_logline": "source_episode_logline_missing",
                    "metadata": "source_metadata_missing",
                    "scenes": "source_scenes_missing",
                }.get(top_level, "source_field_missing")
                message = f"阶段 0 缺少字段：{path or top_level}"
            else:
                code = "source_field_invalid"
                message = f"阶段 0 字段无效：{path or top_level}"
            diagnostics.append(_diag(code, message, path, {"error": copy.deepcopy(error)}))
        if "scenes" not in root:
            diagnostics.append(_diag("source_scenes_missing", "阶段 0 缺少 scenes", "scenes"))
        elif not _items(root.get("scenes")):
            diagnostics.append(_diag("source_scenes_empty", "阶段 0 至少需要一个 scene", "scenes"))
        return diagnostics
    if "scenes" not in root:
        return [_diag("source_scenes_missing", "阶段 0 缺少 scenes", "scenes")]
    if not _items(root.get("scenes")):
        return [_diag("source_scenes_empty", "阶段 0 至少需要一个 scene", "scenes")]
    return []


def _source_shape_projection(root: Mapping[str, Any]) -> dict[str, Any]:
    metadata = _mapping(root.get("metadata"))
    scenes = [_mapping(item) for item in _items(root.get("scenes", _MISSING))]
    roster = [_mapping(item) for item in _items(root.get("character_roster", _MISSING))]
    conflicts = [_mapping(item) for item in _items(root.get("conflict_turns", _MISSING))]

    scene_locations: list[str] = []
    scene_times: list[str] = []
    scene_characters: list[str] = []
    risks: list[str] = []
    for scene in scenes:
        for target, source in (
            (scene_locations, scene.get("location")),
            (scene_times, scene.get("time_weather")),
        ):
            text = _text(source)
            if text and text not in target:
                target.append(text)
        scene_characters.extend(_strings(scene.get("characters")))
        shootability = _mapping(scene.get("shootability"))
        for risk in _strings(shootability.get("production_risks")):
            if risk not in risks:
                risks.append(risk)

    main_characters = _strings(root.get("main_characters", _MISSING))
    if not main_characters:
        main_characters = [
            name
            for item in roster
            if (name := _text(item.get("canonical_name") or item.get("name")))
            and name not in main_characters
        ]
    for name in scene_characters:
        if name not in main_characters:
            main_characters.append(name)
    for conflict in conflicts:
        for name in _strings(conflict.get("participants")):
            if name not in main_characters:
                main_characters.append(name)

    character_relations = root.get("character_relations", _MISSING)
    if character_relations is _MISSING:
        character_relations = []
        for conflict in conflicts:
            participants = _strings(conflict.get("participants"))
            relation: dict[str, Any] = {
                "relation": _text(conflict.get("summary")),
                "kind": _text(conflict.get("kind")),
            }
            if participants:
                relation["from"] = participants[0]
            if len(participants) > 1:
                relation["to"] = participants[1]
            if conflict.get("scene_id"):
                relation["scene_id"] = _text(conflict.get("scene_id"))
            character_relations.append(relation)

    core_conflict = _first(root, "core_conflict", default=_MISSING)
    if core_conflict is _MISSING:
        core_conflict = next(
            (_text(item.get("summary")) for item in conflicts if _text(item.get("summary"))),
            "",
        )
        if not core_conflict:
            core_conflict = next(
                (
                    _text(item.get("conflict_or_turn"))
                    for item in scenes
                    if _text(item.get("conflict_or_turn"))
                ),
                "",
            )

    plot_structure = root.get("plot_structure", _MISSING)
    if plot_structure is _MISSING:
        events = [
            _text(scene.get("plot_event"))
            or _text(scene.get("narrative_task"))
            or _text(scene.get("visual_focus"))
            for scene in scenes
        ]
        events = [event for event in events if event]
        plot_structure = {
            "beginning": events[0] if events else "",
            "middle": events[len(events) // 2] if events else "",
            "end": events[-1] if events else "",
        }

    emotion_curve = root.get("emotion_curve", _MISSING)
    if emotion_curve is _MISSING:
        emotion_curve = [
            {
                "scene_no": scene.get("ordinal"),
                "emotion": _text(scene.get("conflict_or_turn"))
                or _text(scene.get("narrative_task")),
            }
            for scene in scenes
            if _text(scene.get("conflict_or_turn")) or _text(scene.get("narrative_task"))
        ]

    foreshadowing = root.get("foreshadowing", _MISSING)
    if foreshadowing is _MISSING:
        foreshadowing = root.get("continuation_outline", [])

    locations = _strings(root.get("locations", _MISSING)) or scene_locations
    time_period = _text(root.get("time_period", metadata.get("time_period", _MISSING))) or (
        scene_times[0] if scene_times else "未设定"
    )
    genre = _text(root.get("genre", metadata.get("genre", _MISSING))) or "剧情"
    worldview = _text(root.get("worldview", metadata.get("worldview", _MISSING))) or "动态漫世界观"

    summary = _text(root.get("summary")) or _text(root.get("episode_logline"))
    visualizable_content = _strings(root.get("visualizable_content", _MISSING))
    if not visualizable_content:
        for scene in scenes:
            visualizable_content.extend(
                item
                for item in (
                    _text(scene.get("visual_focus")),
                    _text(_mapping(scene.get("shootability")).get("visible_action")),
                )
                if item and item not in visualizable_content
            )

    return {
        "summary": summary,
        "genre": genre,
        "time_period": time_period,
        "locations": locations,
        "worldview": worldview,
        "main_characters": main_characters,
        "character_relations": _plain(character_relations),
        "core_conflict": _text(core_conflict),
        "plot_structure": _plain(plot_structure),
        "emotion_curve": _plain(emotion_curve),
        "foreshadowing": _strings(foreshadowing),
        "visualizable_content": visualizable_content,
        "hard_to_visualize": _strings(root.get("hard_to_visualize", _MISSING)),
        "production_risks": _strings(root.get("production_risks", _MISSING)) or risks,
    }


def build_novel_analysis_projection(
    source_output: SourceAnalysisOutput | Mapping[str, Any],
    *,
    chapter_id: str,
    source_signature: str,
    expected_revision: int,
) -> dict[str, Any]:
    """将阶段 0 显式投影为 legacy ``NovelAnalysisOutput`` 内容。"""

    _validate_id(chapter_id, "chapter_id")
    _validate_source_signature(source_signature)
    _validate_expected_revision(expected_revision)
    try:
        root = _root(source_output)
    except (TypeError, ValueError) as exc:
        raise _validation_error("SourceAnalysisOutput", exc) from exc
    diagnostics: list[Diagnostic] = []

    legacy_like = any(field in root for field in _LEGACY_ANALYSIS_FIELDS)
    if legacy_like and "metadata" not in root and "episode_logline" not in root:
        payload: dict[str, Any] = {}
        for field in _LEGACY_ANALYSIS_FIELDS:
            value = root.get(field, _MISSING)
            payload[field] = _MISSING if value is _MISSING else _plain(value)
        missing = [field for field, value in payload.items() if value is _MISSING]
        for field in missing:
            diagnostics.append(
                _diag(
                    "novel_analysis_field_missing",
                    f"legacy NovelAnalysisOutput 缺少 {field}",
                    field,
                )
            )
    else:
        diagnostics.extend(_source_validation_diagnostics(root))
        if diagnostics:
            raise MaterializationError("阶段 0 输出字段不完整，不能物化", diagnostics)
        payload = _source_shape_projection(root)

    for field in ("summary", "genre", "time_period", "worldview", "core_conflict"):
        if payload.get(field, _MISSING) in (_MISSING, None, ""):
            diagnostics.append(
                _diag(
                    "novel_analysis_field_missing",
                    f"legacy NovelAnalysisOutput 缺少 {field}",
                    field,
                )
            )
    if diagnostics:
        raise MaterializationError("阶段 0 不能物化为 NovelAnalysisOutput", diagnostics)

    try:
        return NovelAnalysisOutput.model_validate(payload).model_dump(mode="json")
    except (TypeError, ValueError, ValidationError) as exc:
        raise _validation_error("NovelAnalysisOutput", exc) from exc


def _reuse_index(
    reuse_mapping: Iterable[Any],
) -> tuple[dict[str, Mapping[str, Any]], list[Diagnostic]]:
    result: dict[str, Mapping[str, Any]] = {}
    diagnostics: list[Diagnostic] = []
    for index, raw in enumerate(reuse_mapping):
        item = _plain(raw)
        if not isinstance(item, Mapping):
            diagnostics.append(
                _diag(
                    "asset_mapping_invalid",
                    "asset mapping 必须是对象",
                    f"reuse_mapping[{index}]",
                )
            )
            continue
        workflow_asset_id = _text(item.get("workflow_asset_id"))
        if not workflow_asset_id:
            diagnostics.append(
                _diag(
                    "asset_mapping_workflow_id_missing",
                    "asset mapping 缺少 workflow_asset_id",
                    f"reuse_mapping[{index}].workflow_asset_id",
                )
            )
            continue
        if workflow_asset_id in result:
            diagnostics.append(
                _diag(
                    "asset_mapping_duplicate_workflow_id",
                    "asset mapping 存在重复 workflow_asset_id",
                    f"reuse_mapping[{index}].workflow_asset_id",
                    {"workflow_asset_id": workflow_asset_id},
                )
            )
            continue
        result[workflow_asset_id] = dict(item)
    return result, diagnostics


def build_asset_materialization_plan(
    character_output: CharacterBiosOutput | Mapping[str, Any],
    *,
    project_id: str,
    run_id: str,
    reuse_mapping: Iterable[Any] = (),
) -> list[dict[str, Any]]:
    """从阶段 1 角色小传生成确定性的 Asset create/update payload。"""

    project_id = _validate_id(project_id, "project_id")
    run_id = _validate_id(run_id, "run_id")
    content = _model(character_output, CharacterBiosOutput, "CharacterBiosOutput")
    assert isinstance(content, CharacterBiosOutput)
    source_signature = _input_signature(content, "")
    mappings, diagnostics = _reuse_index(reuse_mapping)

    character_keys = [item.basic_info.character_key for item in content.characters]
    duplicate_keys = sorted(key for key, count in Counter(character_keys).items() if count > 1)
    if duplicate_keys:
        diagnostics.append(
            _diag(
                "asset_character_key_duplicate",
                "CharacterBiosOutput 存在重复 character_key",
                "characters",
                {"character_keys": duplicate_keys},
            )
        )
        raise MaterializationError("阶段 1 角色资产存在重复 character_key", diagnostics)

    payloads: list[dict[str, Any]] = []
    for character in content.characters:
        key = character.basic_info.character_key
        workflow_asset_id = f"dynamic-asset:{run_id}:{key}"
        mapping = mappings.get(workflow_asset_id) or mappings.get(key)
        reuse_mode = "new"
        asset_id = workflow_asset_id
        locked_revision: int | None = None
        operation = "create"
        status = "draft"

        if mapping is not None and _text(mapping.get("reuse_mode")) == "approved_existing":
            asset_id = _text(mapping.get("asset_id"))
            locked_revision = mapping.get("locked_revision")
            if not asset_id or type(locked_revision) is not int or locked_revision < 1:
                diagnostics.append(
                    _diag(
                        "asset_mapping_approval_incomplete",
                        "approved_existing 必须同时提供 asset_id 和 locked_revision",
                        f"characters[{len(payloads)}]",
                        {"workflow_asset_id": workflow_asset_id},
                    )
                )
                continue
            reuse_mode = "approved_existing"
            operation = "update"
            status = "confirmed"
        elif mapping is not None and mapping.get("asset_id"):
            diagnostics.append(
                _diag(
                    "asset_mapping_reuse_not_approved",
                    "未明确 approved_existing 的 asset mapping 不会复用已有素材",
                    f"characters[{len(payloads)}]",
                    {"workflow_asset_id": workflow_asset_id},
                    level="warning",
                )
            )

        try:
            anchor = character_bio_to_anchor(
                character,
                asset_id=asset_id,
                asset_revision=locked_revision or 1,
            )
        except (TypeError, ValueError, ValidationError) as exc:
            diagnostics.append(
                _diag(
                    "asset_visual_anchor_invalid",
                    "角色视觉锚点不能转换为 AssetVisualAnchor",
                    f"characters[{len(payloads)}].visual_anchor",
                    {"error": str(exc)},
                )
            )
            continue

        anchor_payload = anchor.model_dump(mode="json")
        profile_payload = character.model_dump(mode="json")
        profile_payload.update(
            {
                "asset_id": asset_id,
                "asset_type": "character",
                "canonical_name": character.basic_info.canonical_name,
                "character_key": key,
            }
        )
        own_signature = calculate_signature(
            "dynamic-asset-v1",
            {
                "asset_id": asset_id,
                "asset_type": "character",
                "profile": profile_payload,
                "visual_anchor": anchor_payload,
                "importance": "core" if character.basic_info.importance == "core" else "normal",
                "source_signature": source_signature,
            },
        )
        payloads.append(
            {
                "operation": operation,
                "id": asset_id,
                "asset_id": asset_id,
                "workflow_asset_id": workflow_asset_id,
                "project_id": project_id,
                "asset_type": "character",
                "name": character.basic_info.canonical_name,
                "profile": copy.deepcopy(profile_payload),
                "profile_json": canonical_json(profile_payload),
                "visual_anchor": copy.deepcopy(anchor_payload),
                "visual_anchor_json": canonical_json(anchor_payload),
                "importance": "core" if character.basic_info.importance == "core" else "normal",
                "status": status,
                "source_signature": source_signature,
                "own_signature": own_signature,
                "revision": locked_revision or 1,
                "reuse_mode": reuse_mode,
                "locked_revision": locked_revision,
            }
        )

    if any(item.level == "blocking" for item in diagnostics):
        raise MaterializationError("阶段 1 资产映射存在 blocking 诊断", diagnostics)
    return MaterializationPlan(payloads, diagnostics=diagnostics)


def build_screenplay_projection(
    script_output: ScriptOutput | Mapping[str, Any],
    *,
    chapter_id: str,
    source_signature: str,
    expected_revision: int,
) -> dict[str, Any]:
    """把阶段 3 产物投影为 legacy screenplay，并计算稳定 own_signature。"""

    chapter_id = _validate_id(chapter_id, "chapter_id")
    source_signature = _validate_source_signature(source_signature)
    expected_revision = _validate_expected_revision(expected_revision)
    content = _model(script_output, ScriptOutput, "ScriptOutput")
    assert isinstance(content, ScriptOutput)
    legacy = script_output_to_legacy_screenplay(content)
    own_signature = screenplay_signature(legacy)
    provenance = {
        "materializer_version": _MATERIALIZER_VERSION,
        "stage_key": "3-script",
        "chapter_id": chapter_id,
        "source_signature": source_signature,
        "input_signature": _input_signature(content, source_signature),
        "revision": expected_revision + 1,
    }
    result = copy.deepcopy(legacy)
    result.update(
        {
            "content": copy.deepcopy(legacy),
            "content_json": canonical_json(legacy),
            "chapter_id": chapter_id,
            "source_signature": source_signature,
            "own_signature": own_signature,
            "revision": expected_revision + 1,
            "provenance": provenance,
        }
    )
    return result


def _shot_provenance(
    *,
    project_id: str,
    chapter_id: str,
    source_signature: str,
    external_key: str,
    shot_no: int,
) -> dict[str, Any]:
    return {
        "materializer_version": _MATERIALIZER_VERSION,
        "stage_key": "4-storyboard",
        "project_id": project_id,
        "chapter_id": chapter_id,
        "source_signature": source_signature,
        "external_key": external_key,
        "shot_no": shot_no,
    }


def build_shot_materialization_plan(
    storyboard_output: StoryboardOutput | Mapping[str, Any],
    *,
    chapter_id: str,
    project_id: str,
    source_signature: str,
    existing_shot_numbers: Iterable[int] = (),
) -> list[dict[str, Any]]:
    """为当前章节生成隔离的 Shot payload，不创建 prompt 或媒体。"""

    chapter_id = _validate_id(chapter_id, "chapter_id")
    project_id = _validate_id(project_id, "project_id")
    source_signature = _validate_source_signature(source_signature)
    content = _model(storyboard_output, StoryboardOutput, "StoryboardOutput")
    assert isinstance(content, StoryboardOutput)
    try:
        existing = list(existing_shot_numbers)
    except TypeError as exc:
        raise MaterializationError(
            "existing_shot_numbers 必须是整数序列",
            [_diag("shot_numbers_invalid", "existing_shot_numbers 必须是整数序列")],
        ) from exc
    if any(type(number) is not int or number < 1 for number in existing):
        raise MaterializationError(
            "existing_shot_numbers 必须只包含大于等于 1 的整数",
            [_diag("shot_numbers_invalid", "existing_shot_numbers 必须只包含大于等于 1 的整数")],
        )

    raw_payloads = storyboard_to_shot_payloads(content)
    diagnostics: list[Diagnostic] = []
    external_keys = [str(item.get("external_key") or "") for item in raw_payloads]
    duplicate_external_keys = sorted(
        key for key, count in Counter(external_keys).items() if key and count > 1
    )
    source_number_counts: Counter[int] = Counter()
    for item in raw_payloads:
        shot_no = item.get("shot_no")
        if type(shot_no) is int:
            source_number_counts[shot_no] += 1
    duplicate_source_numbers = sorted(
        number for number, count in source_number_counts.items() if count > 1
    )
    if duplicate_external_keys:
        diagnostics.append(
            _diag(
                "shot_external_key_duplicate",
                "标准分镜存在重复 external_key",
                "shots",
                {"external_keys": duplicate_external_keys},
            )
        )
    if duplicate_source_numbers:
        diagnostics.append(
            _diag(
                "shot_no_duplicate",
                "标准分镜存在重复 shot_no",
                "shots",
                {"shot_nos": duplicate_source_numbers},
            )
        )

    payloads: list[dict[str, Any]] = []
    next_shot_no = max(existing, default=0) + 1
    for index, raw in enumerate(raw_payloads):
        payload = copy.deepcopy(raw)
        path = f"shots[{index}]"
        scene_name = _text(payload.get("scene_name"))
        scene_asset_id = _text(payload.get("scene_asset_id"))
        if (scene_name or _text(payload.get("scene_id"))) and not scene_asset_id:
            diagnostics.append(
                _diag(
                    "shot_scene_asset_id_missing",
                    "场景名称存在但缺少显式 scene_asset_id",
                    f"{path}.scene_asset_id",
                )
            )
        for label, names_key, ids_key in (
            ("character", "character_names", "character_ids"),
            ("prop", "prop_names", "prop_ids"),
        ):
            names = _strings(payload.get(names_key, []))
            ids = _strings(payload.get(ids_key, []))
            if names and (len(ids) < len(names) or any(not _text(item) for item in ids)):
                diagnostics.append(
                    _diag(
                        f"shot_{label}_asset_ids_missing",
                        f"{label} 名称存在但缺少显式素材 ID",
                        f"{path}.{ids_key}",
                        {"name_count": len(names), "asset_id_count": len(ids)},
                    )
                )
            if ids:
                payload[ids_key] = ids

        assigned_shot_no = next_shot_no
        next_shot_no += 1
        external_key = _text(payload.get("external_key"))
        provenance = _shot_provenance(
            project_id=project_id,
            chapter_id=chapter_id,
            source_signature=source_signature,
            external_key=external_key,
            shot_no=assigned_shot_no,
        )
        metadata: dict[str, Any]
        try:
            metadata_value = json.loads(payload.get("pipeline_metadata_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata_value = {}
        metadata = (
            copy.deepcopy(dict(metadata_value)) if isinstance(metadata_value, Mapping) else {}
        )
        metadata["provenance"] = provenance
        metadata["materialization_revision"] = 1
        payload.update(
            {
                "id": f"dynamic-shot:{chapter_id}:{external_key}",
                "project_id": project_id,
                "chapter_id": chapter_id,
                "shot_no": assigned_shot_no,
                "shot_order": assigned_shot_no,
                "source_signature": source_signature,
                "pipeline_metadata_json": canonical_json(metadata),
                "provenance": provenance,
                "revision": 1,
            }
        )
        signature_values = {
            key: copy.deepcopy(value)
            for key, value in payload.items()
            if key not in {"own_signature", "provenance"}
        }
        payload["own_signature"] = calculate_signature("dynamic-shot-v1", signature_values)
        payloads.append(payload)

    if any(item.level == "blocking" for item in diagnostics):
        raise MaterializationError("阶段 4 分镜存在 blocking 诊断", diagnostics)
    return MaterializationPlan(payloads, diagnostics=diagnostics)


def extract_shot_asset_requirements(
    shot_payloads: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """纯函数提取分镜显式素材绑定，实际存在性校验由事务调用方完成。"""

    requirements: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for shot_index, payload in enumerate(shot_payloads):
        if not isinstance(payload, Mapping):
            continue
        fields = (
            ("scene_asset_id", "scene", "scene_asset_id"),
            ("character_ids_json", "character", "character_asset_ids"),
            ("prop_ids_json", "prop", "prop_asset_ids"),
        )
        for field_name, expected_type, path_field in fields:
            raw = payload.get(field_name)
            if raw is None and field_name.endswith("_json"):
                raw = payload.get(field_name.removesuffix("_json"))
            if field_name == "scene_asset_id":
                values = [raw] if _text(raw) else []
            else:
                decoded = raw
                if isinstance(raw, str):
                    try:
                        decoded = json.loads(raw)
                    except (TypeError, json.JSONDecodeError):
                        decoded = []
                values = _items(decoded)
            for value_index, value in enumerate(values):
                asset_id = _text(value)
                if not asset_id:
                    continue
                key = (asset_id, expected_type, f"shots[{shot_index}].{path_field}")
                if key in seen:
                    continue
                seen.add(key)
                requirements.append(
                    {
                        "asset_id": asset_id,
                        "expected_asset_type": expected_type,
                        "path": (
                            f"shots[{shot_index}].{path_field}"
                            if field != "scene_asset_id"
                            else f"shots[{shot_index}].scene_asset_id"
                        ),
                        "value_index": value_index,
                    }
                )
        raw_references = payload.get(
            "reference_bindings_json",
            payload.get("reference_bindings", []),
        )
        if isinstance(raw_references, str):
            try:
                raw_references = json.loads(raw_references)
            except (TypeError, json.JSONDecodeError):
                raw_references = []
        purpose_types = {
            "clean_identity": "character",
            "layout_reference": "scene",
            "prop_identity": "prop",
        }
        for reference_index, reference in enumerate(_items(raw_references)):
            if not isinstance(reference, Mapping):
                continue
            asset_id = _text(reference.get("asset_id"))
            purpose = _text(reference.get("purpose") or reference.get("reference_purpose"))
            expected_type = purpose_types.get(purpose)
            if not asset_id or expected_type is None:
                continue
            requirements.append(
                {
                    "asset_id": asset_id,
                    "expected_asset_type": expected_type,
                    "path": f"shots[{shot_index}].reference_bindings[{reference_index}].asset_id",
                    "value_index": reference_index,
                }
            )
    return requirements


def build_prompt_stage_signature_payload(
    *,
    stage_key: str,
    run_input_signature: str,
    shots: Iterable[Mapping[str, Any]],
    asset_versions: Iterable[Mapping[str, Any]],
    reference_plan_hashes: Mapping[str, Any],
    provider_id: str,
    provider_model: str,
    provider_capabilities: Mapping[str, Any],
    compiler_version: str,
    normalizer_version: str,
    gate_version: str,
) -> dict[str, Any]:
    """构造 5a/5b 共用的、可序列化的输入快照。"""

    return {
        "stage_key": stage_key,
        "run_input_signature": run_input_signature,
        "shots": _plain(list(shots)),
        "asset_versions": _plain(list(asset_versions)),
        "reference_plan_hashes": _plain(dict(reference_plan_hashes)),
        "provider": {
            "id": provider_id,
            "model": provider_model,
            "capabilities": _plain(dict(provider_capabilities)),
            "capability_version": provider_capabilities.get("capability_version", ""),
        },
        "compiler_version": compiler_version,
        "normalizer_version": normalizer_version,
        "gate_version": gate_version,
    }


def calculate_prompt_stage_input_signature(payload: Mapping[str, Any]) -> str:
    """使用项目统一 hash 算法计算 Prompt stage 输入签名。"""

    return calculate_signature("dynamic-prompt-stage-input-v2", dict(payload))


def ensure_materialization_revision(actual_revision: Any, expected_revision: Any) -> int:
    """由事务调用方比较已读取的 canonical revision，冲突时不覆盖旧内容。"""

    expected = _validate_expected_revision(expected_revision)
    if actual_revision != expected:
        diagnostic = _diag(
            "materialization_revision_conflict",
            "canonical 内容在物化期间已被修改",
            "revision",
            {"expected_revision": expected, "actual_revision": actual_revision},
        )
        raise MaterializationConflict(
            expected_revision=expected,
            actual_revision=actual_revision if type(actual_revision) is int else None,
            diagnostics=[diagnostic],
        )
    return expected


def _canonical_payload(
    *,
    stage_key: str,
    project_id: str,
    chapter_id: str,
    run_id: str,
    source_signature: str,
    input_signature: str,
    revision: int,
    content: Any,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    provenance = {
        "materializer_version": _MATERIALIZER_VERSION,
        "stage_key": stage_key,
        "project_id": project_id,
        "chapter_id": chapter_id,
        "run_id": run_id,
        "source_signature": source_signature,
        "input_signature": input_signature,
        "revision": revision,
    }
    payload: dict[str, Any] = {
        "stage_key": stage_key,
        "project_id": project_id,
        "chapter_id": chapter_id,
        "run_id": run_id,
        "source_signature": source_signature,
        "input_signature": input_signature,
        "revision": revision,
        "provenance": provenance,
        "content": _plain(content),
    }
    if isinstance(content, Mapping):
        payload.update(copy.deepcopy(dict(content)))
    if extra:
        payload.update(copy.deepcopy(dict(extra)))
    return payload


def materialize_confirmed_stage(
    stage_key: str,
    content: Any,
    *,
    project_id: str,
    chapter_id: str,
    run_id: str,
    source_signature: str,
    expected_revision: int,
    existing_shot_numbers: Iterable[int] = (),
    reuse_mapping: Iterable[Any] = (),
) -> MaterializationResult:
    """构造确认阶段的事务 payload；本函数自身不访问数据库。"""

    if stage_key not in _MATERIALIZABLE_STAGES:
        raise MaterializationError(
            f"不支持物化阶段：{stage_key}",
            [
                _diag(
                    "materialization_stage_unsupported", f"不支持物化阶段：{stage_key}", "stage_key"
                )
            ],
        )
    project_id = _validate_id(project_id, "project_id")
    chapter_id = _validate_id(chapter_id, "chapter_id")
    run_id = _validate_id(run_id, "run_id")
    source_signature = _validate_source_signature(source_signature)
    expected_revision = _validate_expected_revision(expected_revision)
    if stage_key == "2-rhythm":
        try:
            input_signature = _input_signature(content, source_signature)
        except (TypeError, ValueError):
            input_signature = source_signature
        return MaterializationResult(
            stage_key=stage_key,
            chapter_id=chapter_id,
            project_id=project_id,
            canonical_payload={},
            input_signature=input_signature,
            revision=expected_revision,
        )

    input_signature = _input_signature(content, source_signature)
    if stage_key == "0-source":
        projection = build_novel_analysis_projection(
            content,
            chapter_id=chapter_id,
            source_signature=source_signature,
            expected_revision=expected_revision,
        )
        canonical = _canonical_payload(
            stage_key=stage_key,
            project_id=project_id,
            chapter_id=chapter_id,
            run_id=run_id,
            source_signature=source_signature,
            input_signature=input_signature,
            revision=expected_revision + 1,
            content=projection,
        )
        return MaterializationResult(
            stage_key=stage_key,
            chapter_id=chapter_id,
            project_id=project_id,
            canonical_payload=canonical,
            input_signature=input_signature,
            revision=expected_revision + 1,
        )

    if stage_key == "1-character-bios":
        plan = build_asset_materialization_plan(
            content,
            project_id=project_id,
            run_id=run_id,
            reuse_mapping=reuse_mapping,
        )
        created = [item["asset_id"] for item in plan if item.get("operation") == "create"]
        updated = [item["asset_id"] for item in plan if item.get("operation") == "update"]
        canonical = _canonical_payload(
            stage_key=stage_key,
            project_id=project_id,
            chapter_id=chapter_id,
            run_id=run_id,
            source_signature=source_signature,
            input_signature=input_signature,
            revision=expected_revision + 1,
            content={"assets": plan},
            extra={"asset_mapping": [copy.deepcopy(item) for item in plan]},
        )
        return MaterializationResult(
            stage_key=stage_key,
            chapter_id=chapter_id,
            project_id=project_id,
            created_asset_ids=created,
            updated_asset_ids=updated,
            canonical_payload=canonical,
            diagnostics=list(getattr(plan, "diagnostics", ())),
            input_signature=input_signature,
            revision=expected_revision + 1,
        )

    if stage_key == "3-script":
        projection = build_screenplay_projection(
            content,
            chapter_id=chapter_id,
            source_signature=source_signature,
            expected_revision=expected_revision,
        )
        screenplay_content = projection.get("content", projection)
        canonical = _canonical_payload(
            stage_key=stage_key,
            project_id=project_id,
            chapter_id=chapter_id,
            run_id=run_id,
            source_signature=source_signature,
            input_signature=input_signature,
            revision=expected_revision + 1,
            content=screenplay_content,
            extra={
                "own_signature": projection["own_signature"],
                "content_json": projection["content_json"],
            },
        )
        return MaterializationResult(
            stage_key=stage_key,
            chapter_id=chapter_id,
            project_id=project_id,
            canonical_payload=canonical,
            input_signature=input_signature,
            revision=expected_revision + 1,
        )

    plan = build_shot_materialization_plan(
        content,
        chapter_id=chapter_id,
        project_id=project_id,
        source_signature=source_signature,
        existing_shot_numbers=existing_shot_numbers,
    )
    canonical = _canonical_payload(
        stage_key=stage_key,
        project_id=project_id,
        chapter_id=chapter_id,
        run_id=run_id,
        source_signature=source_signature,
        input_signature=input_signature,
        revision=expected_revision + 1,
        content={"shots": plan},
        extra={"shot_payloads": [copy.deepcopy(item) for item in plan]},
    )
    return MaterializationResult(
        stage_key=stage_key,
        chapter_id=chapter_id,
        project_id=project_id,
        shot_payloads=[copy.deepcopy(item) for item in plan],
        canonical_payload=canonical,
        diagnostics=list(getattr(plan, "diagnostics", ())),
        input_signature=input_signature,
        revision=expected_revision + 1,
    )


__all__ = [
    "MaterializationConflict",
    "MaterializationError",
    "MaterializationPlan",
    "MaterializationResult",
    "build_asset_materialization_plan",
    "build_novel_analysis_projection",
    "build_screenplay_projection",
    "build_shot_materialization_plan",
    "extract_shot_asset_requirements",
    "build_prompt_stage_signature_payload",
    "calculate_prompt_stage_input_signature",
    "ensure_materialization_revision",
    "materialize_confirmed_stage",
]
