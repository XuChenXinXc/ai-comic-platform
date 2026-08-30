from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal
from urllib.parse import urlsplit

from app.dynamic_pipeline.prompt_ir import (
    AssetVisualAnchor,
    VisualStyleSpec,
    prompt_input_hash,
)
from app.dynamic_pipeline.schemas import CharacterBio, Diagnostic

AssetType = Literal["character", "scene", "prop"]
ReferencePurpose = Literal[
    "clean_identity",
    "layout_reference",
    "prop_identity",
    "keyframe_reference",
]

_ALLOWED_REFERENCE_PURPOSES = frozenset(
    {
        "clean_identity",
        "layout_reference",
        "prop_identity",
        "keyframe_reference",
    }
)
_DEFAULT_STYLE: dict[str, Any] = {
    "medium": "二维动画/插画管线，角色、场景与道具使用同一媒介",
    "line_style": "干净连续的中细线，轮廓明确，结构线克制，细节线随材质变化",
    "color_palette": "低至中饱和的统一综合色盘，主体色明确，少量受控强调色",
    "lighting_style": "符合场景光源逻辑的柔和主光与轻微轮廓光，保留阴影层次和关键细节",
    "material_style": "以二维上色表现可区分的布料、木石、金属、玻璃和皮革纹理，不使用塑料化高光",
    "realism_level": "稳定的半写实二维动画比例与结构，不是真人摄影或三维写实渲染",
    "mood": "克制、清晰、具有叙事氛围，情绪通过色彩、光线和表演细节表达",
    "text_policy": "画面默认不生成字幕、对白、标题、标识、水印或无意义文字；文字由后期处理",
    "global_negative_rules": [
        "真人摄影、影视实拍、产品摄影",
        "三维写实渲染、游戏 CG 质感",
        "角色身份漂移、资产结构漂移、镜像翻转",
        "错误透视、结构错位、无来源光线",
        "字幕、对白、标题、Logo、签名、水印、乱码",
    ],
    "immutable_rules": [
        "同一资产的身份、核心轮廓、比例、颜色、材质和关键识别特征保持一致",
        "只使用当前镜头明确绑定的资产，不从名称猜测或引入其他资产",
        "图片只表现一个静止关键时刻，视频只表现一个主要动作及其明确结束状态",
    ],
}

_CHARACTER_FIELDS = {
    "face": ("face", "face_shape", "facial_features", "appearance"),
    "hair": ("hair", "hairstyle", "hair_style"),
    "body": ("body", "body_shape", "physique", "figure", "build"),
    "costume": ("costume", "clothing", "outfit", "clothes"),
}
_SCENE_FIELDS = {
    "layout": ("layout", "space_keywords"),
    "axis": ("axis", "main_view", "observer_reference"),
    "entrances": ("entrances", "entrance_position"),
    "fixed_furnishings": ("fixed_furnishings", "furnishings"),
    "lighting_direction": ("lighting_direction", "lighting", "main_light"),
    "weather": ("weather",),
    "time_of_day": ("time_of_day",),
}
_PROP_FIELDS = {
    "shape": ("shape", "silhouette", "description"),
    "material": ("material",),
    "color": ("color", "main_color"),
    "wear": ("wear", "usage_marks"),
    "owner": ("owner", "user"),
    "current_holder": ("current_holder", "holder"),
    "visibility": ("visibility",),
}
_STYLE_OVERRIDE_ALIASES = {
    "line": "line_style",
    "color": "color_palette",
    "lighting": "lighting_style",
    "material": "material_style",
    "realism": "realism_level",
}
_REFERENCE_TYPE_BY_PURPOSE = {
    "clean_identity": frozenset({"character", "character_identity", "clean_identity"}),
    "layout_reference": frozenset({"scene", "scene_layout", "layout_reference"}),
    "prop_identity": frozenset({"prop", "prop_identity"}),
    "keyframe_reference": frozenset({"keyframe", "keyframe_version", "keyframe_reference"}),
}


class ReferenceDiagnostic(Diagnostic):
    """参考图筛选诊断，既保留结构化字段，也暴露原始列表索引。"""

    index: int
    purpose: str = ""

    def __getitem__(self, key: str) -> Any:
        if key == "index":
            return self.index
        if key == "purpose":
            return self.purpose
        try:
            return getattr(self, key)
        except AttributeError as exc:
            raise KeyError(key) from exc

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="python")
        if isinstance(dumped, Mapping):
            return dumped
    raise TypeError("输入必须是 Mapping 或支持 model_dump 的模型")


def _mapping_from_value(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping) or hasattr(value, "model_dump"):
        return _as_mapping(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        if isinstance(decoded, Mapping):
            return decoded
    return {}


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    return ""


def _items(value: Any) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, list | tuple | set | frozenset):
        result: list[str] = []
        for item in value:
            if isinstance(item, Mapping):
                item_text = _text(
                    item.get("description")
                    or item.get("name")
                    or item.get("value")
                    or item.get("text")
                )
            else:
                item_text = _text(item)
            if item_text and item_text not in result:
                result.append(item_text)
        return result
    item_text = _text(value)
    return [item_text] if item_text else []


def _list_items(value: Any) -> list[str]:
    """读取兼容旧 profile 的列表字段，支持常见中文/英文分隔符。"""

    result: list[str] = []
    for item in _items(value):
        for part in re.split(r"[、，,；;|]", item):
            text = part.strip()
            if text and text not in result:
                result.append(text)
    return result


def _first_text(
    source: Mapping[str, Any],
    keys: tuple[str, ...],
    *,
    _depth: int = 0,
) -> str:
    for key in keys:
        value = source.get(key)
        if isinstance(value, Mapping) and _depth < 3:
            nested = _first_text(value, keys, _depth=_depth + 1)
            if not nested:
                nested = _first_text(
                    value,
                    ("value", "description", "text", "content"),
                    _depth=_depth + 1,
                )
            if nested:
                return nested
        text = _text(value)
        if text:
            return text
    return ""


def _first_value(source: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = source.get(key)
        if value is not None and value != "" and value != [] and value != {}:
            return value
    return None


def _canonical_name(profile: Mapping[str, Any]) -> str:
    name = _first_text(profile, ("canonical_name", "name", "asset_name", "title"))
    if name:
        return name
    basic_info = profile.get("basic_info")
    if isinstance(basic_info, Mapping) or hasattr(basic_info, "model_dump"):
        return _first_text(_as_mapping(basic_info), ("canonical_name", "name"))
    return ""


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _mapping_value(source: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    value = _first_value(source, keys)
    if value is None:
        return None
    if isinstance(value, Mapping):
        return copy.deepcopy(dict(value))
    if isinstance(value, list | tuple | set | frozenset):
        return copy.deepcopy(list(value))
    text = _text(value)
    return text if text else None


def _reference_sources(reference: Mapping[str, Any]) -> list[str]:
    return [
        value
        for key in ("relative_path", "file_path", "provider_url", "url")
        if (value := _text(reference.get(key)))
    ]


def _is_http_url(value: str) -> bool:
    parsed = urlsplit(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _is_relative_path(value: str) -> bool:
    if not value or _is_http_url(value):
        return False
    if value.startswith(("/", "\\")) or ":" in value.split("/", 1)[0]:
        return False
    return "\x00" not in value and not any(
        part == ".." for part in value.replace("\\", "/").split("/")
    )


def _has_valid_reference_source(reference: Mapping[str, Any]) -> bool:
    for key in ("relative_path", "file_path", "provider_url", "url"):
        source = _text(reference.get(key))
        if source and (
            _is_http_url(source)
            or (key in {"relative_path", "file_path"} and _is_relative_path(source))
        ):
            return True
    return False


def _approved(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float) and not isinstance(value, bool):
        return value == 1
    return _text(value).casefold() in {"true", "1"}


def _reference_purpose(reference: Mapping[str, Any]) -> str:
    return _text(reference.get("purpose") or reference.get("reference_purpose"))


def _reference_type(reference: Mapping[str, Any]) -> str:
    value = _text(
        reference.get("type")
        or reference.get("reference_type")
        or reference.get("kind")
        or reference.get("reference_kind")
        or reference.get("asset_type")
        or reference.get("media_type")
    )
    value = value.casefold()
    if value in {
        "image",
        "media",
        "asset_image",
        "image_version",
        "png",
        "jpg",
        "jpeg",
        "webp",
    } or value.startswith("image/"):
        return ""
    return value


def _reference_payloads(references: Any) -> list[dict[str, Any]]:
    return [
        copy.deepcopy(dict(reference))
        for reference in references or ()
        if isinstance(reference, Mapping)
    ]


def _expand_nested_profile_fields(source: dict[str, Any]) -> None:
    """兼容旧 profile 中 appearance/layout/description 的嵌套写法。"""

    nested_aliases = {
        "appearance": {
            "face": ("face", "face_shape", "facial_features"),
            "hair": ("hair", "hairstyle", "hair_style"),
            "body": ("body", "body_shape", "physique", "figure", "build"),
        },
        "layout": {
            "space_keywords": ("space_keywords",),
            "regions": ("regions",),
            "entrance_position": ("entrance_position", "entrances"),
            "window_position": ("window_position",),
            "movement_path": ("movement_path",),
        },
        "description": {
            "silhouette": ("silhouette",),
            "dimensions": ("dimensions", "size"),
            "material": ("material",),
            "color": ("color", "main_color"),
            "wear": ("wear", "usage_marks"),
        },
    }
    for container, fields in nested_aliases.items():
        nested = _mapping_from_value(source.get(container))
        for target, aliases in fields.items():
            if target in source:
                continue
            value = _first_value(nested, aliases)
            if value is not None:
                source[target] = value


def build_visual_style_spec(
    *,
    style_prompt: str = "",
    style_version: str = "dynamic-style-v1",
    overrides: Mapping[str, Any] | None = None,
) -> VisualStyleSpec:
    """构造可执行的默认视觉规范，用户文本只作为补充信息。"""

    values = copy.deepcopy(_DEFAULT_STYLE)
    if overrides:
        for key, value in overrides.items():
            field_name = _STYLE_OVERRIDE_ALIASES.get(key, key)
            if field_name not in VisualStyleSpec.model_fields or field_name in {
                "style_prompt",
                "style_version",
            }:
                continue
            if value is not None:
                values[field_name] = copy.deepcopy(value)
    values["style_version"] = _text(style_version) or "dynamic-style-v1"
    supplemental = _text(style_prompt)
    values["style_prompt"] = supplemental or None
    return VisualStyleSpec.model_validate(values)


def asset_profile_to_anchor(
    profile: Mapping[str, Any],
    *,
    asset_id: str,
    asset_revision: int | str | None = None,
    asset_type: AssetType | None = None,
    approved_references: list[Mapping[str, Any]] | None = None,
) -> AssetVisualAnchor:
    """把单个 canonical Asset 的 profile 投影为视觉锚点，不进行资产查找或合并。"""

    raw = _as_mapping(profile)
    profile_value = raw.get("profile")
    profile_mapping = _mapping_from_value(profile_value)
    if not profile_mapping:
        profile_mapping = _mapping_from_value(raw.get("profile_json"))
    source = dict(profile_mapping) if profile_mapping else dict(raw)
    for key in (
        "canonical_name",
        "name",
        "asset_name",
        "title",
        "asset_type",
        "asset_revision",
        "revision",
        "asset_version",
        "version",
        "approved_references",
    ):
        if key in raw and key not in source:
            source[key] = raw[key]
    if "consistency_keywords" not in source:
        consistency = raw.get("consistency_keywords")
        if consistency is None:
            consistency_json = raw.get("consistency_json")
            if isinstance(consistency_json, str):
                try:
                    consistency = json.loads(consistency_json)
                except (TypeError, ValueError):
                    consistency = None
        if consistency is not None:
            source["consistency_keywords"] = consistency
    nested_visual = _mapping_from_value(source.get("visual_anchor"))
    if not nested_visual:
        nested_visual = _mapping_from_value(raw.get("visual_anchor_json"))
    for key, value in nested_visual.items():
        if key not in source or source[key] in (None, "", [], {}):
            source[key] = value
    _expand_nested_profile_fields(source)
    canonical_id = _text(asset_id)
    if not canonical_id:
        raise ValueError("asset_id 不能为空")
    resolved_type = asset_type or _text(source.get("asset_type")) or None
    if resolved_type not in {"character", "scene", "prop", None}:
        raise ValueError("asset_type 必须是 character、scene 或 prop")
    canonical_name = _canonical_name(source)
    if not canonical_name:
        raise ValueError("profile 缺少 canonical_name/name")

    facts: list[str] = []
    for key in (
        "identity",
        "concept",
        "description",
        "appearance",
        "architecture",
        "layout",
        "space_keywords",
        "furnishings",
        "special_features",
        "consistency_keywords",
        "visual_facts",
        "facts",
        "visual_description",
    ):
        facts.extend(_items(source.get(key)))
    if resolved_type == "character":
        for keys in _CHARACTER_FIELDS.values():
            facts.extend(_items(_first_value(source, keys)))
        facts.extend(_items(source.get("accessories")))
    elif resolved_type == "scene":
        for keys in _SCENE_FIELDS.values():
            facts.extend(_items(_first_value(source, keys)))
    elif resolved_type == "prop":
        for keys in _PROP_FIELDS.values():
            facts.extend(_items(_first_value(source, keys)))

    values: dict[str, Any] = {
        "canonical_name": canonical_name,
        "asset_id": canonical_id,
        "asset_type": resolved_type,
        "asset_revision": (
            asset_revision
            if asset_revision is not None
            else _first_value(source, ("asset_revision", "revision"))
        ),
        "asset_version": _first_value(source, ("asset_version", "version")),
        "visual_facts": _dedupe(facts),
        "immutable_features": _list_items(source.get("immutable_features")),
        "forbidden_changes": _list_items(
            source.get("forbidden_changes")
            or source.get("forbidden")
            or source.get("forbidden_variants")
            or source.get("negative_prompt")
        ),
        "makeup_keyword_string": _first_text(
            source,
            (
                "makeup_keyword_string",
                "makeup",
                "makeup_keywords",
                "visual_keyword_string",
                "appearance_keywords",
                "costume_keyword_string",
            ),
        )
        or "|".join(_list_items(source.get("consistency_keywords"))),
        "variants": _list_items(
            source.get("variants")
            or source.get("costume_variants")
            or source.get("appearance_variants")
            or source.get("variant")
        ),
        "current_variant": _first_text(source, ("current_variant", "active_variant")) or None,
        "approved_references": _reference_payloads(
            approved_references
            if approved_references is not None
            else source.get("approved_references", [])
        ),
        "details": copy.deepcopy(dict(source)),
    }

    if resolved_type == "character":
        for field, keys in _CHARACTER_FIELDS.items():
            values[field] = _first_text(source, keys)
        values["accessories"] = _list_items(source.get("accessories") or source.get("accessory"))
        values["character_details"] = copy.deepcopy(dict(source))
    elif resolved_type == "scene":
        for field, keys in _SCENE_FIELDS.items():
            values[field] = (
                _list_items(_first_value(source, keys))
                if field in {"entrances", "fixed_furnishings"}
                else _mapping_value(source, keys)
            )
        values["scene_details"] = copy.deepcopy(dict(source))
    elif resolved_type == "prop":
        for field, keys in _PROP_FIELDS.items():
            values[field] = _mapping_value(source, keys)
        values["prop_details"] = copy.deepcopy(dict(source))

    return AssetVisualAnchor.model_validate(values)


def character_bio_to_anchor(
    character: Mapping[str, Any] | CharacterBio,
    *,
    asset_id: str,
    asset_revision: int | str | None = None,
    approved_references: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...] = (),
) -> AssetVisualAnchor:
    """把 Phase 2 CharacterBio 的视觉锚点投影为带 canonical ID 的资产锚点。"""

    source = _as_mapping(character)
    basic_info = source.get("basic_info")
    visual_anchor = source.get("visual_anchor")
    basic = (
        _as_mapping(basic_info)
        if isinstance(basic_info, Mapping) or hasattr(basic_info, "model_dump")
        else {}
    )
    visual = (
        _as_mapping(visual_anchor)
        if isinstance(visual_anchor, Mapping) or hasattr(visual_anchor, "model_dump")
        else source
    )
    canonical_name = _first_text(basic, ("canonical_name", "name")) or _first_text(
        source, ("canonical_name", "name")
    )
    if not canonical_name:
        raise ValueError("CharacterBio 缺少 canonical_name")
    canonical_id = _text(asset_id)
    if not canonical_id:
        raise ValueError("asset_id 不能为空")

    visual_facts = _list_items(visual.get("visual_facts"))
    immutable = _list_items(visual.get("immutable_features"))
    forbidden = _list_items(visual.get("forbidden_changes"))
    variants = _list_items(visual.get("variants"))
    values = {
        "canonical_name": canonical_name,
        "asset_id": canonical_id,
        "asset_type": "character",
        "asset_revision": asset_revision,
        "visual_facts": _dedupe(visual_facts),
        "immutable_features": immutable,
        "forbidden_changes": forbidden,
        "makeup_keyword_string": _first_text(visual, ("makeup_keyword_string",)),
        "face": _first_text(visual, ("face", "appearance")),
        "hair": _first_text(visual, ("hair",)),
        "body": _first_text(visual, ("body",)),
        "costume": _first_text(visual, ("costume",)),
        "accessories": _list_items(visual.get("accessories")),
        "variants": variants,
        "current_variant": _first_text(visual, ("current_variant",)) or None,
        "approved_references": _reference_payloads(approved_references),
        "details": copy.deepcopy(dict(source)),
        "character_details": copy.deepcopy(dict(source)),
    }
    return AssetVisualAnchor.model_validate(values)


def reference_is_usable(
    reference: Mapping[str, Any],
    *,
    allowed_purposes: tuple[str, ...] = (
        "clean_identity",
        "layout_reference",
        "prop_identity",
        "keyframe_reference",
    ),
) -> bool:
    """判断参考图是否获批、用途合法且至少有一个有效来源。"""

    if not _approved(reference.get("approved")):
        return False
    purpose = _reference_purpose(reference)
    allowed = set(allowed_purposes)
    if purpose not in allowed or purpose == "design_sheet":
        return False
    reference_type = _reference_type(reference)
    if reference_type in {"design_sheet", "setting_sheet"}:
        return False
    if (
        reference_type
        and purpose in _REFERENCE_TYPE_BY_PURPOSE
        and reference_type not in _REFERENCE_TYPE_BY_PURPOSE[purpose]
    ):
        return False
    return _has_valid_reference_source(reference)


def filter_generation_references(
    references: Sequence[Mapping[str, Any]],
    *,
    purpose: str,
    video: bool = False,
) -> tuple[list[dict[str, Any]], list[ReferenceDiagnostic]]:
    """按原顺序筛选可用于生成的参考图，并为每个排除项保留原始 index。"""

    usable: list[dict[str, Any]] = []
    diagnostics: list[ReferenceDiagnostic] = []
    for index, reference in enumerate(references):
        item = dict(reference) if isinstance(reference, Mapping) else {}
        raw_index = item.get("index", index)
        try:
            reference_index = int(raw_index)
        except (TypeError, ValueError):
            reference_index = index
        item_purpose = _reference_purpose(item)
        reference_type = _reference_type(item)
        code = ""
        message = ""
        if item_purpose == "design_sheet":
            code = "design_sheet_as_reference"
            message = (
                "视频不能使用 design_sheet 参考图"
                if video
                else "design_sheet 仅用于设定展示，不能作为生成身份参考"
            )
        elif reference_type in {"design_sheet", "setting_sheet"}:
            code = "design_sheet_as_reference"
            message = "设定板仅用于展示，不能作为生成参考图"
        elif item_purpose != purpose or item_purpose not in _ALLOWED_REFERENCE_PURPOSES:
            code = "reference_wrong_type"
            message = f"参考图用途 {item_purpose or '为空'} 不符合当前用途 {purpose}"
        elif (
            reference_type
            and item_purpose in _REFERENCE_TYPE_BY_PURPOSE
            and reference_type not in _REFERENCE_TYPE_BY_PURPOSE[item_purpose]
        ):
            code = "reference_wrong_type"
            message = f"参考图类型 {reference_type} 不符合用途 {item_purpose}"
        elif not _approved(item.get("approved")):
            code = "reference_missing"
            message = "参考图尚未 approved"
        elif not _reference_sources(item):
            code = "reference_missing"
            message = "参考图缺少 relative_path/file_path 或 http/https URL"
        elif not _has_valid_reference_source(item):
            code = "reference_missing"
            message = "参考图来源不是有效的相对路径/file_path 或 http/https URL"
        if code:
            diagnostics.append(
                ReferenceDiagnostic(
                    level="warning",
                    code=code,
                    message=message,
                    path=f"references[{reference_index}]",
                    details={"index": reference_index, "purpose": item_purpose},
                    index=reference_index,
                    purpose=item_purpose,
                )
            )
            continue
        usable.append(copy.deepcopy(item))
    return usable, diagnostics


def anchor_version(anchor: AssetVisualAnchor | Mapping[str, Any]) -> str:
    """为资产 ID、revision 和视觉事实计算稳定版本签名。"""

    values = (
        anchor.model_dump(mode="json") if isinstance(anchor, AssetVisualAnchor) else dict(anchor)
    )
    return prompt_input_hash(values)


__all__ = [
    "anchor_version",
    "asset_profile_to_anchor",
    "build_visual_style_spec",
    "character_bio_to_anchor",
    "filter_generation_references",
    "ReferenceDiagnostic",
    "reference_is_usable",
]
