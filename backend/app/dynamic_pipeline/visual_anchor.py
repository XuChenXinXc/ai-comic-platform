from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal
from urllib.parse import urlsplit

from app.dynamic_pipeline.prompt_ir import (
    AssetVisualAnchor,
    MaterialSpec,
    VisualStyleSpec,
    prompt_input_hash,
)
from app.dynamic_pipeline.schemas import CharacterBio, Diagnostic
from app.dynamic_pipeline.visual_validator import DYNAMIC_REALISM_NEGATIVE_RULES

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
    "medium": "媒介与质感完全遵循项目统一画风（style_prompt）的要求",
    "line_style": "线条表现完全遵循项目统一画风（style_prompt）的要求",
    "color_palette": "配色完全遵循项目统一画风（style_prompt）的要求",
    "lighting_style": "光影处理遵循项目统一画风（style_prompt）的要求，并符合场景光源逻辑",
    "material_style": "材质表现完全遵循项目统一画风（style_prompt）的要求",
    "realism_level": "写实度与结构比例遵循项目统一画风（style_prompt）的要求",
    "mood": "情绪氛围通过构图、光线与表演细节表达，不另行限定画风",
    "text_policy": "画面默认不生成字幕、对白、标题、标识、水印或无意义文字；文字由后期处理",
    "global_negative_rules": [
        "真人摄影、影视实拍、产品摄影",
        "三维写实渲染、游戏 CG 质感",
        *DYNAMIC_REALISM_NEGATIVE_RULES,
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
_STRUCTURED_STYLE_FIELDS = (
    "line",
    "coloring",
    "shading",
    "material",
    "lighting",
    "depth_of_field",
    "realism",
    "texture",
    "forbidden_styles",
)
_BASE_TWO_D_FORBIDDEN_STYLES = [
    "3D",
    "photorealistic",
    "PBR",
    "cel-shading",
    "concept art",
    "oil painting",
]


def _derive_structured_style(style_prompt: str) -> dict[str, Any]:
    """从项目画风文本推导结构化视觉 DNA 默认值（只覆盖二维/国漫/水墨类画风）。

    未命中任何二维关键词、或画风明确偏向写实/3D/真人时返回空 dict，
    不强行给用户画风套用二维规则。半写实漫风按二维处理，但不禁用写实明暗。
    """

    prompt = style_prompt.strip()
    if not prompt:
        return {}
    folded = prompt.casefold()
    two_dimensional = any(
        key in folded for key in ("二维", "国漫", "漫画", "动漫", "2d", "漫风", "仿真人")
    )
    ink = any(key in folded for key in ("水墨", "写意", "国画"))
    semi_realistic = "半写实" in folded
    realistic = (
        ("写实" in folded and not semi_realistic)
        or ("真人" in folded and "仿真人" not in folded)
        or any(
            key in folded
            for key in ("3d", "三维", "实拍", "摄影", "电影级", "cg渲染")
        )
    )
    if realistic:
        # 写实/3D 画风不套用二维禁则，改为锁定「摄影写实 / 三维CG」的正向指纹：
        # 用户自由文本（style_prompt）仍然唯一可信，但推导出的结构化字段把宽泛的
        # 写实语义压成一组稳定视觉参数，防止逐镜滑向二维手绘或真人实拍互相漂移。
        realistic_3d = any(
            key in folded for key in ("3d", "三维", "cg渲染", "游戏cg", "建模")
        )
        cinematic = "电影级" in folded or "影视级" in folded or "电影感" in folded
        if realistic_3d:
            # 渲染描述压缩：每个渲染概念只声明一次。媒介与排除（CG 动画质感、
            # 非真人实拍、非二维手绘）由渲染指纹声明；PBR、全局光照、写实明暗、
            # 写实度各占一个结构化字段，不在「上色/材质/光影/指纹」里重复锁多遍。
            return {
                "line": {
                    "type": "无描线（三维模型轮廓）",
                    "thickness": "无",
                    "continuity": "不适用",
                },
                "coloring": {
                    "method": "材质着色自然色彩",
                    "saturation": "自然",
                    "color_count": "自然过渡",
                },
                "shading": {
                    "method": "写实体积明暗",
                    "gradient": "柔和渐变",
                    "realistic_shading": True,
                },
                "material": {
                    "method": "PBR 材质",
                    "pbr": True,
                    "photographic_reflection": True,
                },
                "lighting": {
                    "realism": "三维全局光照",
                    "volumetric": None,
                    "cinematic_glow": False,
                },
                "depth_of_field": {"enabled": True},
                "realism": {"character": 1.0, "environment": 1.0},
                "texture": "真实材质纹理与模型精度",
                "forbidden_styles": [
                    "二维平涂动漫",
                    "赛璐璐",
                    "手绘水墨",
                    "手绘线稿",
                    "真人实拍",
                ],
                # 三维CG项目不沿用默认的反写实化负向（默认列表含
                # 「三维写实渲染、游戏 CG 质感」「PBR 材质」等反3D条款）。
                "global_negative_rules": [
                    "真人摄影、影视实拍、产品摄影",
                    "二维平涂动漫、赛璐璐、手绘水墨、手绘线稿",
                    "角色身份漂移、资产结构漂移、镜像翻转",
                    "错误透视、结构错位、无来源光线",
                    "字幕、对白、标题、Logo、签名、水印、乱码",
                ],
                "render_fingerprint": (
                    "画面为三维建模渲染的 CG 动画质感，不是真人实拍、不是二维手绘动漫；"
                    "人物与场景采用三维渲染表达，模型轮廓干净、无手绘描线；"
                    "光影、材质与反射表现按上述结构化规范渲染，保持全片一致；"
                    "保持三维建模渲染质感，但避免写实到像真人实拍，"
                    "避免夸张游戏宣传海报式高反光、重特效、过饱和与塑料感。"
                ),
            }
        return {
            "line": {
                "type": "无描线（摄影写实轮廓）",
                "thickness": "无",
                "continuity": "不适用",
            },
            "coloring": {
                "method": "自然色彩与肤色还原",
                "saturation": "自然",
                "color_count": "自然过渡",
            },
            "shading": {
                "method": "写实体积明暗",
                "gradient": "柔和渐变",
                "realistic_shading": True,
            },
            "material": {
                "method": "真实物理材质",
                "pbr": None,
                "photographic_reflection": None,
            },
            "lighting": {
                "realism": "写实电影布光" if cinematic else "写实自然布光",
                "volumetric": None,
                "cinematic_glow": False,
            },
            "depth_of_field": {"enabled": True},
            "realism": {"character": 1.0, "environment": 1.0},
            "texture": "真实皮肤与面料质感",
            "forbidden_styles": [
                "二维平涂动漫",
                "赛璐璐",
                "手绘水墨",
                "卡通渲染",
            ],
            # 真人写实项目排除二维手绘与三维CG漂移，不沿用默认的反3D条款。
            "global_negative_rules": [
                "二维平涂动漫、赛璐璐、手绘水墨、卡通渲染、三维CG",
                "角色身份漂移、资产结构漂移、镜像翻转",
                "错误透视、结构错位、无来源光线",
                "字幕、对白、标题、Logo、签名、水印、乱码",
            ],
            "render_fingerprint": (
                "画面为真人实拍质感的写实影像，不是二维手绘动漫、不是三维CG；"
                "人物皮肤与衣物呈现真实物理质感，五官与肤色按真人比例还原；"
                "光影符合真实光学规律，不使用二维平涂明暗或手绘笔触；"
                "材质与反射表现保持全片一致。"
            ),
        }
    if not two_dimensional and not ink:
        return {}
    result: dict[str, Any] = {
        "material": {"method": "绘画概括", "pbr": False, "photographic_reflection": False},
        "lighting": {
            "realism": "写意" if ink else "",
            "volumetric": False,
            "cinematic_glow": False,
        },
        "depth_of_field": {"enabled": False},
        "shading": {
            "method": "墨色明暗块" if ink else "",
            "gradient": "弱" if ink else "",
            # 半写实画风需要写实明暗，不输出「禁用写实明暗」限制。
            "realistic_shading": None if semi_realistic else False,
        },
    }
    # 赛璐璐/平涂类画风禁用 cel-shading 会反向禁用自身表达方式，
    # 画风原文声明该词时从默认禁则中剔除。
    forbidden_styles = copy.deepcopy(_BASE_TWO_D_FORBIDDEN_STYLES)
    if "赛璐璐" in folded or "平涂" in folded:
        forbidden_styles = [
            item for item in forbidden_styles if item.casefold() != "cel-shading"
        ]
    result["forbidden_styles"] = forbidden_styles
    if not ink and not semi_realistic:
        # 普通二维（国漫/漫画/动漫/2d/漫风）默认：清晰描线 + 平涂上色，
        # 让视觉圣经面板的线条/上色分组也有推导回显，而不是永远空白。
        result["line"] = {
            "type": "清晰描线",
            "thickness": "中等",
            "continuity": "连贯",
        }
        result["coloring"] = {
            "method": "平涂上色",
            "saturation": "自然",
            "color_count": "自然",
        }
    if semi_realistic:
        # 半写实仿真人漫风：主体仍是二维绘画，但保留写实明暗、真实比例与
        # 细腻肤色渲染；「影视级/电影感」变体额外放开写实电影布光与浅景深，
        # 让结构化视觉圣经对这类画风产出完整的正向约束而不是只有禁用条目。
        cinematic = "影视级" in folded or "电影感" in folded
        result["line"] = {
            "type": "精细写实勾线",
            "thickness": "细",
            "continuity": "连贯",
        }
        result["coloring"] = {
            "method": "写实肤色与自然色上色",
            "saturation": "中高",
            "color_count": "自然过渡",
        }
        result["shading"] = {
            "method": "写实体积明暗",
            "gradient": "柔和渐变",
            "realistic_shading": True,
        }
        result["material"] = {
            "method": "半写实绘画质感",
            "pbr": False,
            "photographic_reflection": False,
        }
        result["lighting"] = {
            "realism": "写实电影布光" if cinematic else "自然写实布光",
            "volumetric": None if cinematic else False,
            "cinematic_glow": None if cinematic else False,
        }
        result["depth_of_field"] = {"enabled": True if cinematic else False}
        result["realism"] = {
            "character": 0.85 if cinematic else 0.8,
            "environment": 0.8 if cinematic else 0.7,
        }
        result["texture"] = (
            "细腻皮肤质感、电影级光影过渡、半写实绘画笔触"
            if cinematic
            else "细腻皮肤质感、半写实绘画笔触"
        )
        # 渲染方式指纹：把宽泛的半写实仿真人漫拆成一组稳定视觉参数，
        # 每镜逐字重复同一套指纹，才能压住场景、灯光或新增角色参考图带来的漂移。
        result["render_fingerprint"] = (
            "人物采用接近真人比例的半写实人漫造型，五官真实但保留轻微漫画概括；"
            "皮肤为细腻哑光手绘质感，不出现真人毛孔摄影质感；"
            "轮廓线极弱，仅在局部结构转折处可见；"
            "头发采用成束塑形与细碎发丝结合；"
            "衣物保持真实褶皱但带适度绘制概括；"
            "场景材质真实但不过度PBR化；"
            "阴影柔和自然，不采用硬边赛璐璐阴影；"
            f"整体为{'影视动画' if cinematic else '手绘动画'}渲染，"
            "不是真人照片，不是3D游戏CG，不是二维平涂动漫。"
        )
        return result
    if ink:
        line_type = "工笔细线" if "工笔" in folded else "粗笔焦墨"
        result["line"] = {
            "type": line_type,
            "thickness": "中粗" if "粗笔" in folded or "焦墨" in folded else "",
            "continuity": "允许自然断续",
        }
        result["coloring"] = {
            "method": "重彩点染+湿墨铺色" if "重彩" in folded else "湿墨铺色",
            "saturation": "低至中",
            "color_count": "克制",
        }
        result["texture"] = "宣纸、水墨晕染、粗笔笔触"
    return result
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
    """构造视觉规范：画风唯一来源是项目统一画风（style_prompt）。

    默认字段只提供不指定具体画风的中性占位与一致性技术约束，
    不引入任何与统一画风无关的线条、配色、材质或写实度要求。
    """

    values = copy.deepcopy(_DEFAULT_STYLE)
    structured_supplied = False
    if overrides:
        for key, value in overrides.items():
            # 结构化字段直接作为结构化覆盖（值为 Mapping 时）；其余键按别名走旧字段。
            if key in _STRUCTURED_STYLE_FIELDS and isinstance(value, Mapping):
                values[key] = copy.deepcopy(dict(value))
                structured_supplied = True
                continue
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
    if not structured_supplied and supplemental:
        derived = _derive_structured_style(supplemental)
        for field, field_value in derived.items():
            values[field] = copy.deepcopy(field_value)
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


def parse_visual_bible(project: Mapping[str, Any]) -> dict[str, Any] | None:
    """从项目记录中读取结构化视觉圣经。

    兼容两种来源：ProjectService._public_record 解析后的 ``visual_bible`` 键，
    以及直接查表得到的原始 ``visual_bible_json`` 字符串。空值返回 None（自动推导）。
    """
    visual_bible = project.get("visual_bible")
    if visual_bible is None:
        raw = project.get("visual_bible_json")
        if isinstance(raw, str) and raw.strip():
            try:
                visual_bible = json.loads(raw)
            except (TypeError, ValueError):
                visual_bible = None
        elif isinstance(raw, Mapping):
            visual_bible = dict(raw)
    if not isinstance(visual_bible, Mapping) or not visual_bible:
        return None
    return dict(visual_bible)


def compile_visual_bible(
    style_prompt: str,
    visual_bible: Mapping[str, Any] | None,
) -> VisualStyleSpec:
    """合并画风原文与结构化覆盖，得到可渲染的视觉规范。

    visual_bible 为空时按 style_prompt 关键词自动推导（只覆盖二维/水墨类画风）。
    """
    return build_visual_style_spec(
        style_prompt=style_prompt or "",
        overrides=visual_bible or None,
    )


def _tri_state(value: bool | None, label: str) -> str:
    if value is None:
        return ""
    return f"{'启用' if value else '禁用'}{label}"


def _material_style_pairs(material: MaterialSpec) -> list[tuple[str, Any]]:
    """材质分句：方式文本已含 PBR 时不再重复输出「启用PBR」开关，避免同一概念锁多遍。"""
    pairs: list[tuple[str, Any]] = [("方式", material.method)]
    if material.pbr is not None and "PBR" not in (material.method or ""):
        pairs.append(("", _tri_state(material.pbr, "PBR")))
    if material.photographic_reflection is not None:
        pairs.append(
            ("", _tri_state(material.photographic_reflection, "摄影级反射"))
        )
    return pairs


def _render_style_clause(
    label: str,
    pairs: Sequence[tuple[str, Any]],
) -> str:
    """把一个结构化分组的已设置字段渲染为「标签：字段「值」」分句。

    name 为空的条目表示已是完整文本（三态开关渲染为「禁用PBR」），直接追加。
    """
    bits: list[str] = []
    for name, value in pairs:
        text = str(value).strip() if value is not None else ""
        if not text:
            continue
        bits.append(text if not name else f"{name}「{text}」")
    return f"{label}：" + "、".join(bits) if bits else ""


def render_visual_style_spec(spec: VisualStyleSpec) -> str:
    """把已设置的结构化字段渲染成提示词约束文本；无结构化内容时返回空串。

    只渲染用户设置过或关键词推导出的字段，不渲染默认占位文本；
    bool 三态渲染为「启用/禁用」，避免把关闭语义写成正向要求。
    """
    clauses = [
        clause
        for clause in (
            _render_style_clause(
                "线条",
                (
                    ("类型", spec.line.type),
                    ("粗细", spec.line.thickness),
                    ("连贯性", spec.line.continuity),
                ),
            ),
            _render_style_clause(
                "上色",
                (
                    ("方式", spec.coloring.method),
                    ("饱和度", spec.coloring.saturation),
                    ("用色", spec.coloring.color_count),
                ),
            ),
            _render_style_clause(
                "明暗",
                (
                    ("方式", spec.shading.method),
                    ("渐变", spec.shading.gradient),
                    ("", _tri_state(spec.shading.realistic_shading, "写实明暗")),
                ),
            ),
            _render_style_clause(
                "材质",
                _material_style_pairs(spec.material),
            ),
            _render_style_clause(
                "光影",
                (
                    ("写实度", spec.lighting.realism),
                    ("", _tri_state(spec.lighting.volumetric, "体积光")),
                    ("", _tri_state(spec.lighting.cinematic_glow, "电影级辉光")),
                ),
            ),
        )
        if clause
    ]
    if spec.depth_of_field.enabled is not None:
        clauses.append(
            f"景深：{'启用' if spec.depth_of_field.enabled else '禁用'}"
        )
    realism_bits = []
    for name, value in (
        ("角色写实度", spec.realism.character),
        ("环境写实度", spec.realism.environment),
    ):
        if value is not None:
            realism_bits.append(f"{name}「{value:g}」")
    if realism_bits:
        clauses.append("写实度：" + "、".join(realism_bits))
    if _text(spec.texture):
        clauses.append(f"画面质感「{_text(spec.texture)}」")
    if _text(spec.render_fingerprint):
        clauses.append(
            f"渲染方式锁定：{_text(spec.render_fingerprint).rstrip('。')}"
        )
    return "结构化画风规范：" + "；".join(clauses) + "。" if clauses else ""


def visual_style_negative_terms(spec: VisualStyleSpec) -> list[str]:
    """把禁用风格列表归一化为负面提示词条目。"""
    return [str(item).strip() for item in spec.forbidden_styles if str(item).strip()]


def merge_visual_bible_negative(
    negative_prompt: str,
    spec: VisualStyleSpec,
) -> str:
    """把禁用风格幂等地并入负面提示词（兼容顿号/逗号/分号分隔）。"""
    terms = visual_style_negative_terms(spec)
    if not terms:
        return negative_prompt
    existing = [
        item.strip()
        for item in re.split(r"[、，,；;]", str(negative_prompt or ""))
        if item.strip()
    ]
    merged = list(existing)
    for term in terms:
        if term not in merged:
            merged.append(term)
    return "、".join(merged)


def visual_bible_brief(project: Mapping[str, Any]) -> str:
    """LLM 系统提示词引用的画风结构化约束片段；无内容时返回空串。"""
    brief = render_visual_style_spec(
        compile_visual_bible(
            project.get("style_prompt") or "",
            parse_visual_bible(project),
        )
    )
    return f"项目画风结构化约束：{brief}\n" if brief else ""


__all__ = [
    "anchor_version",
    "asset_profile_to_anchor",
    "build_visual_style_spec",
    "character_bio_to_anchor",
    "compile_visual_bible",
    "filter_generation_references",
    "merge_visual_bible_negative",
    "parse_visual_bible",
    "ReferenceDiagnostic",
    "reference_is_usable",
    "render_visual_style_spec",
    "visual_bible_brief",
    "visual_style_negative_terms",
]
