"""动态合成前的纯输入预检。

本模块只消费结构化快照和本地文件系统，不访问数据库、网络或 FFmpeg。
旧的 ``CompositionService`` 仍由调用方负责执行；动态合成必须先使用本模块的
结果作门禁判断。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from app.dynamic_pipeline.schemas import Diagnostic

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400

MediaKind = Literal[
    "references",
    "keyframe",
    "video",
    "audio",
    "subtitle",
    "bgm",
    "composition",
]
_MEDIA_KINDS = MediaKind
_KIND_ORDER = ("references", "keyframe", "video", "audio", "subtitle", "bgm", "composition")
_FAILED_STATUSES = frozenset(
    {
        "failed",
        "error",
        "cancelled",
        "canceled",
        "interrupted",
        "partial_failed",
        "invalid",
    }
)
_READY_STATUSES = frozenset(
    {
        "completed",
        "complete",
        "confirmed",
        "ready",
        "available",
        "success",
        "succeeded",
        "ok",
    }
)
_PENDING_STATUSES = frozenset(
    {
        "pending",
        "queued",
        "running",
        "processing",
        "waiting_provider",
        "downloading",
        "draft",
        "missing",
    }
)
_HARD_PATH_CODES = frozenset(
    {
        "project_root_invalid",
        "path_absolute",
        "path_traversal",
        "path_outside_project",
        "path_symlink",
        "path_reparse_point",
        "path_type",
        "path_invalid",
    }
)
_KIND_ALIASES: dict[str, tuple[str, ...]] = {
    "keyframe": ("keyframes", "keyframe", "keyframe_versions", "keyframe_version"),
    "video": ("videos", "video", "video_versions", "video_version"),
    "audio": ("audios", "audio", "shot_audios", "shot_audio"),
    "subtitle": ("subtitles", "subtitle", "shot_subtitles", "shot_subtitle"),
    "bgm": ("bgms", "bgm", "project_bgms", "project_bgm"),
    "composition": ("compositions", "composition", "chapter_composition"),
}


class PreflightError(ValueError):
    """合成预检快照无法解析时抛出的错误。"""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PreflightItem(_StrictModel):
    """一个合成输入或合成目标的检查结果。"""

    kind: _MEDIA_KINDS
    id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    path: str | None = None
    reason: str = ""
    required: bool = True


class CompositionPreflightResult(_StrictModel):
    """动态合成预检的可持久化结果。"""

    workflow_run_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    chapter_id: str = Field(min_length=1)
    strict: bool
    allow_partial: bool
    status: Literal["ready", "warning", "blocked"]
    items: list[PreflightItem] = Field(default_factory=list)
    missing: list[PreflightItem] = Field(default_factory=list)
    failed: list[PreflightItem] = Field(default_factory=list)
    skipped: list[PreflightItem] = Field(default_factory=list)
    estimated_jobs: int = Field(default=0, ge=0)
    input_signature: str = ""
    provenance: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class _Evaluation:
    item: PreflightItem
    original: PreflightItem | None
    category: Literal["missing", "failed"] | None
    diagnostics: tuple[Diagnostic, ...]
    hard_block: bool
    required_unavailable: bool


def _diagnostic(
    code: str,
    message: str,
    *,
    path: str | None = None,
    details: Mapping[str, Any] | None = None,
    level: Literal["warning", "blocking"] = "blocking",
) -> Diagnostic:
    return Diagnostic(
        level=level,
        code=code,
        message=message,
        path=path,
        details=dict(details or {}),
    )


def _is_reparse_point(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return os.path.islink(path) or bool(
        getattr(metadata, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
    )


def _relative_parts(relative_path: str | Path) -> tuple[tuple[str, ...], Diagnostic | None]:
    if isinstance(relative_path, Path):
        value = os.fspath(relative_path)
    elif isinstance(relative_path, str):
        value = relative_path
    else:
        return (), _diagnostic("path_type", "媒体路径必须是字符串或 Path")

    value = value.strip()
    if not value:
        return (), _diagnostic("path_invalid", "媒体路径不能为空")
    if "\x00" in value:
        return (), _diagnostic("path_invalid", "媒体路径包含非法字符")

    windows_path = PureWindowsPath(value)
    posix_path = PurePosixPath(value.replace("\\", "/"))
    if (
        Path(value).is_absolute()
        or windows_path.is_absolute()
        or posix_path.is_absolute()
        or bool(windows_path.drive)
        or value[:1] in {"/", "\\"}
    ):
        return (), _diagnostic("path_absolute", "媒体路径必须是项目相对路径")

    parts = tuple(part for part in value.replace("\\", "/").split("/") if part not in {"", "."})
    if not parts:
        return (), _diagnostic("path_invalid", "媒体路径不能为空")
    if ".." in parts:
        return (), _diagnostic("path_traversal", "媒体路径不能包含 ..")
    return parts, None


def validate_relative_media_path(
    project_root: str | Path,
    relative_path: str | Path,
    *,
    require_file: bool = True,
) -> Path | Diagnostic:
    """校验项目内媒体路径并返回解析后的本地路径。

    校验逐级使用 ``lstat``，因此不会跟随符号链接或 Windows reparse point。
    路径越界使用 ``Path.relative_to`` 判断，绝不使用字符串前缀判断，也不会发起
    网络请求。
    """

    parts, path_error = _relative_parts(relative_path)
    if path_error is not None:
        return path_error

    root_value = Path(project_root)
    try:
        if _is_reparse_point(root_value):
            return _diagnostic("path_reparse_point", "项目根目录不能是符号链接或 reparse point")
        root = root_value.resolve(strict=False)
        if not root.exists() or not root.is_dir():
            return _diagnostic("project_root_invalid", "项目根目录不存在或不是目录")
        if _is_reparse_point(root):
            return _diagnostic("path_reparse_point", "项目根目录不能是符号链接或 reparse point")
    except (OSError, RuntimeError, ValueError) as exc:
        return _diagnostic("project_root_invalid", f"项目根目录无效：{exc}")

    candidate = root
    try:
        for index, part in enumerate(parts):
            candidate = candidate / part
            if _is_reparse_point(candidate):
                return _diagnostic(
                    "path_symlink",
                    "媒体路径不能经过符号链接或目录联接",
                    path="/".join(parts[: index + 1]),
                )
            try:
                metadata = os.lstat(candidate)
            except FileNotFoundError:
                return _diagnostic(
                    "path_missing",
                    "媒体文件不存在",
                    path="/".join(parts),
                )
            except OSError as exc:
                return _diagnostic(
                    "path_invalid",
                    f"媒体路径不可读取：{exc}",
                    path="/".join(parts),
                )
            if index < len(parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
                return _diagnostic(
                    "path_not_directory",
                    "媒体路径的中间节点不是目录",
                    path="/".join(parts[: index + 1]),
                )

        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(root)
        except ValueError:
            return _diagnostic("path_outside_project", "媒体路径超出项目目录")

        metadata = os.lstat(candidate)
        if require_file and not stat.S_ISREG(metadata.st_mode):
            return _diagnostic("path_not_file", "媒体路径不是普通文件", path="/".join(parts))
        if stat.S_ISREG(metadata.st_mode) and metadata.st_size <= 0:
            return _diagnostic("path_empty", "媒体文件为空", path="/".join(parts))
        return resolved
    except (OSError, RuntimeError, ValueError) as exc:
        return _diagnostic("path_invalid", f"媒体路径无效：{exc}", path="/".join(parts))


def _model_mapping(value: Any) -> dict[str, Any] | None:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python")
    if isinstance(value, Mapping):
        return dict(value)
    return None


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _json_mapping(value: Any) -> dict[str, Any]:
    mapping = _model_mapping(value)
    if mapping is not None:
        return mapping
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _looks_like_record(value: Mapping[str, Any]) -> bool:
    return bool(
        set(value).intersection(
            {
                "id",
                "path",
                "file_path",
                "relative_path",
                "status",
                "project_id",
                "shot_id",
                "text",
                "start_time",
                "end_time",
                "kind",
            }
        )
    )


def _record_values(value: Any, *, default_shot_id: str | None = None) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python")
    if isinstance(value, str):
        return [{"file_path": value, "shot_id": default_shot_id}]
    if isinstance(value, Mapping):
        mapping: Mapping[str, Any] = value
        if _looks_like_record(mapping):
            record_result: dict[str, Any] = dict(mapping)
            if default_shot_id and not record_result.get("shot_id"):
                record_result["shot_id"] = default_shot_id
            return [record_result]
        for wrapper in ("items", "records", "data", "versions", "current"):
            if wrapper in mapping:
                nested = _record_values(mapping[wrapper], default_shot_id=default_shot_id)
                if nested:
                    return nested
        record_list: list[dict[str, Any]] = []
        for key, nested_value in mapping.items():
            nested_records = _record_values(
                nested_value,
                default_shot_id=_text(key) or default_shot_id,
            )
            for record in nested_records:
                record.setdefault("id", _text(key))
                record_list.append(record)
        return record_list
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        result = []
        for item in value:
            result.extend(_record_values(item, default_shot_id=default_shot_id))
        return result
    return [{"status": "failed", "error": "记录不是有效对象", "shot_id": default_shot_id}]


def _dedupe_records(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        copied = dict(record)
        key = json.dumps(
            {
                "id": copied.get("id"),
                "shot_id": copied.get("shot_id"),
                "path": copied.get("file_path", copied.get("path")),
                "status": copied.get("status"),
                "text": copied.get("text"),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        if key not in seen:
            seen.add(key)
            result.append(copied)
    return result


def _section_records(container: Any, kind: str) -> list[dict[str, Any]]:
    if container is None:
        return []
    if isinstance(container, BaseModel):
        container = container.model_dump(mode="python")
    if isinstance(container, Sequence) and not isinstance(container, str | bytes | bytearray):
        records = _record_values(container)
        return [
            record
            for record in records
            if not _text(record.get("kind") or record.get("type"))
            or _text(record.get("kind") or record.get("type")).casefold() == kind
        ]
    if not isinstance(container, Mapping):
        return []

    records: list[dict[str, Any]] = []
    for alias in _KIND_ALIASES[kind]:
        if alias in container:
            records.extend(_record_values(container[alias]))
    # 也接受 media 为一组带 kind/type 字段的通用记录。
    if _looks_like_record(container) and (
        container.get("id")
        or any(
            key in container
            for key in (
                "file_path",
                "path",
                "relative_path",
                "status",
                "text",
                "start_time",
                "end_time",
            )
        )
    ):
        records.extend(_record_values(container))
    return _dedupe_records(records)


def _top_records(snapshot: Mapping[str, Any], media: Any, kind: str) -> list[dict[str, Any]]:
    records = _section_records(snapshot, kind)
    records.extend(_section_records(media, kind))
    return _dedupe_records(records)


def _shot_records(value: Any) -> list[dict[str, Any]]:
    records = _record_values(value)
    result: list[dict[str, Any]] = []
    for index, record in enumerate(records, start=1):
        copied = dict(record)
        shot_id = _text(copied.get("id") or copied.get("shot_id") or copied.get("external_key"))
        if not shot_id:
            shot_id = f"shot:{index}"
        copied["id"] = shot_id
        result.append(copied)
    return result


def _number(value: Any) -> tuple[int, float]:
    if isinstance(value, bool):
        return (1, 0)
    if isinstance(value, int | float):
        return (0, float(value))
    if isinstance(value, str):
        try:
            return (0, float(value.strip()))
        except ValueError:
            pass
    return (1, 0)


def _shot_sort_key(record: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        _number(record.get("shot_order", record.get("order"))),
        _number(record.get("shot_no", record.get("ordinal"))),
        _text(record.get("external_key")),
        _text(record.get("id")),
    )


def _record_shot_id(record: Mapping[str, Any]) -> str:
    value = record.get("shot_id")
    if isinstance(value, Mapping):
        return _text(value.get("id"))
    return _text(value or record.get("shot"))


def _record_external_key(record: Mapping[str, Any]) -> str:
    return _text(record.get("external_key") or record.get("shot_external_key"))


def _nested_records(
    shot_record: Mapping[str, Any], kind: str, shot_id: str
) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    aliases = _KIND_ALIASES[kind]
    for alias in aliases:
        if alias in shot_record:
            values.extend(_record_values(shot_record[alias], default_shot_id=shot_id))
    media = shot_record.get("media")
    if media is not None:
        values.extend(_section_records(media, kind))
        for record in values:
            record.setdefault("shot_id", shot_id)

    direct_path_keys = {
        "keyframe": ("keyframe_path", "key_frame_path"),
        "video": ("video_path",),
        "audio": ("audio_path",),
        "subtitle": ("subtitle_path", "srt_path"),
    }
    for path_key in direct_path_keys.get(kind, ()):
        if path_key in shot_record and shot_record[path_key] is not None:
            values.append(
                {
                    "id": f"{kind}:{shot_id}",
                    "shot_id": shot_id,
                    "file_path": shot_record[path_key],
                }
            )
    return _dedupe_records(values)


def _matches_shot(
    record: Mapping[str, Any], shot_record: Mapping[str, Any], shot_count: int
) -> bool:
    shot_id = _text(shot_record.get("id"))
    record_shot_id = _record_shot_id(record)
    if record_shot_id:
        return record_shot_id == shot_id
    external_key = _record_external_key(record)
    if external_key:
        return external_key == _text(shot_record.get("external_key"))
    return shot_count == 1


def _records_for_shot(
    shot_record: Mapping[str, Any],
    kind: str,
    top_records: Sequence[Mapping[str, Any]],
    shot_count: int,
) -> list[dict[str, Any]]:
    shot_id = _text(shot_record.get("id"))
    nested = _nested_records(shot_record, kind, shot_id)
    nested.extend(
        dict(record) for record in top_records if _matches_shot(record, shot_record, shot_count)
    )
    result = _dedupe_records(nested)
    # shot 快照同时暴露 singular/plural/version aliases；空 alias 记录不能在
    # 已有真实媒体旁边再制造一个 synthetic missing item。
    meaningful_fields = (
        "file_path",
        "path",
        "relative_path",
        "status",
        "text",
        "start_time",
        "end_time",
        "audio_id",
        "duration_sec",
        "input_signature",
        "failed",
        "ok",
    )
    if result:
        result = [
            record
            for record in result
            if record.get("id")
            or any(record.get(field) is not None for field in meaningful_fields)
        ]
    for index, record in enumerate(result, start=1):
        record.setdefault("shot_id", shot_id)
        record.setdefault("id", f"{kind}:{shot_id}:{index}")
    return result


def _record_id(record: Mapping[str, Any], kind: str, shot_id: str, index: int) -> str:
    value = record.get("id")
    if isinstance(value, Mapping):
        value = value.get("id")
    value = _text(value) or _text(
        record.get(
            {
                "keyframe": "keyframe_id",
                "video": "video_id",
                "audio": "audio_id",
                "subtitle": "subtitle_id",
                "bgm": "bgm_id",
                "composition": "composition_id",
            }[kind]
        )
    )
    return value or f"{kind}:{shot_id}:{index}"


def _record_project_id(record: Mapping[str, Any]) -> str:
    value = record.get("project_id") or record.get("owner_project_id")
    if isinstance(value, Mapping):
        value = value.get("id")
    if value:
        return _text(value)
    nested = record.get("project")
    if isinstance(nested, Mapping):
        return _text(nested.get("id"))
    return _text(nested)


def _record_path(record: Mapping[str, Any]) -> Any:
    for key in ("file_path", "path", "relative_path", "local_path", "srt_path"):
        if key in record:
            return record[key]
    return None


def _record_provenance(record: Mapping[str, Any]) -> dict[str, Any]:
    for key in ("provenance", "provenance_json"):
        if key in record:
            return _json_mapping(record[key])
    return {}


def _record_signature(record: Mapping[str, Any]) -> str:
    provenance = _record_provenance(record)
    return _text(
        record.get("upstream_input_signature")
        or provenance.get("upstream_input_signature")
        or record.get("input_signature")
        or record.get("signature")
        or record.get("source_signature")
        or provenance.get("input_signature")
    )


def _signature_chain(
    record: Mapping[str, Any],
    *,
    expected_run_id: str,
    expected_signature: str,
    stage_signatures: Mapping[str, Sequence[str]],
) -> tuple[bool, str]:
    """校验 root -> stage/upstream -> media own 的分层签名链。"""

    provenance = _record_provenance(record)
    raw_signature = _text(
        record.get("input_signature")
        or record.get("signature")
        or record.get("source_signature")
        or provenance.get("input_signature")
    )
    upstream_signature = _text(
        record.get("upstream_input_signature")
        or provenance.get("upstream_input_signature")
        or provenance.get("upstream_signature")
    )
    stage_id = _text(
        record.get("workflow_stage_id")
        or record.get("stage_id")
        or provenance.get("workflow_stage_id")
        or provenance.get("stage_id")
    )
    stage_key = _text(record.get("stage_key") or provenance.get("stage_key"))
    known = {expected_signature} if expected_signature else set()
    has_dynamic_provenance = bool(provenance) or bool(
        record.get("workflow_run_id")
        or record.get("workflow_stage_id")
        or record.get("stage_id")
    )
    if has_dynamic_provenance:
        record_run_id = _text(
            record.get("workflow_run_id")
            or provenance.get("workflow_run_id")
            or provenance.get("run_id")
        )
        if (
            record_run_id != expected_run_id
            or not stage_id
            or not upstream_signature
            or not raw_signature
        ):
            return False, raw_signature or upstream_signature
        stage_values: set[str] = set()
        for key in (stage_id, stage_key):
            stage_values.update(
                value for value in stage_signatures.get(key, ()) if _text(value)
            )
        # media own signature 可以与 run root 不同，但 upstream 必须精确落在
        # 当前 workflow stage 的输入签名上。
        return upstream_signature in stage_values, raw_signature or upstream_signature

    # 没有 provenance 的记录只按 legacy 语义与 run 根签名兼容；不能把它
    # 当作动态派生媒体放宽到任意末端 signature。
    if upstream_signature:
        return upstream_signature in known, upstream_signature
    if raw_signature in known:
        return True, raw_signature
    return not raw_signature, raw_signature


def _record_status(record: Mapping[str, Any], kind: str) -> tuple[str, str]:
    if record.get("failed") is True or record.get("ok") is False:
        return "failed", _text(record.get("error") or record.get("reason")) or "记录标记为失败"
    raw = _text(record.get("status")).casefold()
    if raw in _FAILED_STATUSES:
        return "failed", _text(record.get("error") or record.get("reason")) or f"媒体状态为 {raw}"
    if raw in _PENDING_STATUSES:
        return "missing", _text(record.get("error") or record.get("reason")) or f"媒体状态为 {raw}"
    if raw and raw not in _READY_STATUSES:
        return "failed", f"媒体状态无法确认：{raw}"
    if kind == "subtitle" and not raw:
        return "ready", ""
    return "ready", ""


def _has_valid_inline_subtitle(record: Mapping[str, Any]) -> bool:
    text = _text(record.get("text"))
    start_raw = record.get("start_time")
    end_raw = record.get("end_time")
    if start_raw is None or end_raw is None:
        return False
    try:
        start_time = float(str(start_raw))
        end_time = float(str(end_raw))
    except (TypeError, ValueError):
        return False
    return bool(text) and math.isfinite(start_time) and math.isfinite(end_time) and (
        start_time >= 0 and end_time > start_time
    )


def _normalise_path_for_manifest(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str | Path):
        return str(value)
    parts, error = _relative_parts(value)
    if error is not None:
        return str(value).replace("\\", "/")
    return "/".join(parts)


def _path_check(
    value: Any,
    project_root: str | Path | None,
) -> tuple[str | None, Diagnostic | None]:
    display = _normalise_path_for_manifest(value)
    if display is None:
        return None, _diagnostic("path_missing", "缺少媒体路径")
    if project_root is None:
        _, shape_error = _relative_parts(value)
        return display, shape_error
    checked = validate_relative_media_path(project_root, value)
    if isinstance(checked, Diagnostic):
        return display, checked
    return display, None


def _contextual_diagnostic(
    value: Diagnostic,
    *,
    kind: str,
    item_id: str,
    shot_id: str | None = None,
) -> Diagnostic:
    details = dict(value.details)
    details.update({"kind": kind, "item_id": item_id})
    if shot_id:
        details["shot_id"] = shot_id
    return Diagnostic(
        level=value.level,
        code=value.code,
        message=value.message,
        path=value.path,
        details=details,
    )


def _required_config(run: Mapping[str, Any], snapshot: Mapping[str, Any]) -> dict[str, Any]:
    for source in (
        run.get("config_snapshot"),
        run.get("config"),
        snapshot.get("config_snapshot"),
        snapshot.get("config"),
    ):
        mapping = _json_mapping(source)
        if mapping:
            return mapping
    return {}


def _config_bool(config: Mapping[str, Any], names: Sequence[str], default: bool) -> bool:
    for name in names:
        if name not in config:
            continue
        value = config[name]
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().casefold() not in {"0", "false", "no", "off"}
        return bool(value)
    return default


def _item_required(record: Mapping[str, Any] | None, default: bool) -> bool:
    if record is None or "required" not in record:
        return default
    value = record["required"]
    if isinstance(value, str):
        return value.strip().casefold() not in {"0", "false", "no", "off"}
    return bool(value)


def _evaluate(
    *,
    kind: MediaKind,
    record: Mapping[str, Any],
    shot_id: str | None,
    project_id: str,
    chapter_id: str,
    workflow_run_id: str,
    expected_signature: str,
    stage_signatures: Mapping[str, Sequence[str]],
    required: bool,
    allow_partial: bool,
    project_root: str | Path | None,
) -> _Evaluation:
    copied = dict(record)
    item_id = _record_id(copied, kind, shot_id or chapter_id, 1)
    path_value = _record_path(copied)
    path, path_error = (
        _path_check(path_value, project_root) if path_value is not None else (None, None)
    )
    reasons: list[str] = []
    diagnostics: list[Diagnostic] = []
    hard_block = False
    category: Literal["missing", "failed"] | None = None

    owner_project_id = _record_project_id(copied)
    if owner_project_id and owner_project_id != project_id:
        code = "preflight_bgm_cross_project" if kind == "bgm" else "preflight_project_mismatch"
        message = (
            "BGM 不属于当前项目，拒绝跨项目合成" if kind == "bgm" else "媒体记录不属于当前项目"
        )
        diagnostics.append(
            _diagnostic(
                code,
                message,
                path=path,
                details={"expected_project_id": project_id, "actual_project_id": owner_project_id},
            )
        )
        reasons.append(message)
        hard_block = True

    record_chapter_id = _text(copied.get("chapter_id"))
    if record_chapter_id and record_chapter_id != chapter_id:
        message = "媒体记录不属于当前章节"
        diagnostics.append(
            _diagnostic(
                "preflight_chapter_mismatch",
                message,
                path=path,
                details={"expected_chapter_id": chapter_id, "actual_chapter_id": record_chapter_id},
            )
        )
        reasons.append(message)
        hard_block = True

    provenance = _record_provenance(copied)
    has_dynamic_provenance = bool(provenance) or bool(
        copied.get("workflow_run_id")
        or copied.get("workflow_stage_id")
        or copied.get("stage_id")
    )
    if has_dynamic_provenance:
        actual_run_id = _text(
            copied.get("workflow_run_id")
            or provenance.get("workflow_run_id")
            or provenance.get("run_id")
        )
        actual_stage_id = _text(
            copied.get("workflow_stage_id")
            or copied.get("stage_id")
            or provenance.get("workflow_stage_id")
            or provenance.get("stage_id")
        )
        if not actual_run_id:
            message = "动态媒体缺少 workflow_run_id provenance"
            diagnostics.append(_diagnostic("preflight_provenance_missing", message, path=path))
            reasons.append(message)
            hard_block = True
        elif actual_run_id != workflow_run_id:
            message = "媒体 provenance 不属于当前 workflow run"
            diagnostics.append(
                _diagnostic(
                    "preflight_workflow_run_mismatch",
                    message,
                    path=path,
                    details={
                        "expected_workflow_run_id": workflow_run_id,
                        "actual_workflow_run_id": actual_run_id,
                    },
                )
            )
            reasons.append(message)
            hard_block = True
        if not actual_stage_id or actual_stage_id not in stage_signatures:
            message = "动态媒体缺少当前 workflow stage provenance"
            diagnostics.append(
                _diagnostic(
                    "preflight_stage_provenance_missing",
                    message,
                    path=path,
                    details={"workflow_stage_id": actual_stage_id},
                )
            )
            reasons.append(message)
            hard_block = True

    signature_ok, record_signature = _signature_chain(
        copied,
        expected_run_id=workflow_run_id,
        expected_signature=expected_signature,
        stage_signatures=stage_signatures,
    )
    if not signature_ok:
        message = "媒体输入 signature 与 workflow run 不一致"
        diagnostics.append(
            _diagnostic(
                "preflight_signature_mismatch",
                message,
                path=path,
                details={
                    "expected_signature": expected_signature,
                    "actual_signature": record_signature,
                },
            )
        )
        reasons.append(message)
        hard_block = True

    raw_status = _text(copied.get("status")).casefold()
    availability, status_reason = _record_status(copied, kind)
    if kind == "subtitle" and _has_valid_inline_subtitle(copied):
        if raw_status in {"", *_PENDING_STATUSES}:
            availability, status_reason = "ready", ""
    if kind == "composition" and raw_status in {"", *_PENDING_STATUSES} and path_value is None:
        # composition 是本次预检要创建的目标，不是合成前置媒体。
        availability, status_reason = "ready", ""
    if status_reason:
        reasons.append(status_reason)
        category = availability  # type: ignore[assignment]
        diagnostics.append(
            _diagnostic(
                "preflight_failed" if availability == "failed" else "preflight_not_ready",
                status_reason,
                path=path,
            )
        )

    requires_path = kind in {"keyframe", "video", "audio", "bgm"}
    if kind == "composition":
        requires_path = raw_status in _READY_STATUSES
    # 字幕（shot_subtitle）无 status 列且由合成阶段生成，路径仅作提示：
    # 有路径时不强制校验存在性（与既有测试契约一致）。
    if kind == "subtitle" and path_value is None:
        text = _text(copied.get("text"))
        try:
            start_value = copied.get("start_time")
            end_value = copied.get("end_time")
            if start_value is None or end_value is None:
                raise TypeError("字幕时间缺失")
            start_time = float(start_value)
            end_time = float(end_value)
        except (TypeError, ValueError):
            start_time, end_time = -1.0, -1.0
        if not text or start_time < 0 or end_time <= start_time:
            requires_path = True
            path_error = _diagnostic("path_missing", "字幕缺少有效文本或时间轴")
    if requires_path:
        if path_value is None:
            path_error = _diagnostic("path_missing", "缺少媒体文件路径")
        if path_error is not None:
            contextual = _contextual_diagnostic(
                path_error,
                kind=kind,
                item_id=item_id,
                shot_id=shot_id,
            )
            diagnostics.append(contextual)
            reasons.append(path_error.message)
            if path_error.code in _HARD_PATH_CODES:
                hard_block = True
                category = "failed"
            elif category is None:
                category = "missing"

    if kind == "composition" and path_value is not None and path_error is not None:
        contextual = _contextual_diagnostic(
            path_error,
            kind=kind,
            item_id=item_id,
            shot_id=shot_id,
        )
        diagnostics.append(contextual)
        reasons.append(path_error.message)
        if path_error.code in _HARD_PATH_CODES:
            hard_block = True
            category = "failed"
        elif category is None:
            category = "missing"

    reason = "; ".join(dict.fromkeys(item for item in reasons if item))
    if hard_block:
        category = "failed"
        if not reason:
            reason = "预检发现不可绕过的阻断错误"
        original = PreflightItem(
            kind=cast("MediaKind", kind),
            id=item_id,
            status="failed",
            path=path,
            reason=reason,
            required=required,
        )
        return _Evaluation(
            item=original,
            original=original,
            category="failed",
            diagnostics=tuple(
                _contextual_diagnostic(item, kind=kind, item_id=item_id, shot_id=shot_id)
                for item in diagnostics
            ),
            hard_block=True,
            required_unavailable=required,
        )

    if category is not None:
        original = PreflightItem(
            kind=cast("MediaKind", kind),
            id=item_id,
            status=category,
            path=path,
            reason=reason or "媒体不可用",
            required=required,
        )
        can_skip = allow_partial or not required
        if can_skip:
            skipped = original.model_copy(update={"status": "skipped"})
            return _Evaluation(
                item=skipped,
                original=original,
                category=category,
                diagnostics=tuple(
                    _contextual_diagnostic(item, kind=kind, item_id=item_id, shot_id=shot_id)
                    for item in diagnostics
                ),
                hard_block=False,
                required_unavailable=required,
            )
        diagnostics.append(
            _diagnostic(
                "preflight_required_unavailable",
                "必需媒体缺失或未完成，不能开始合成",
                path=path,
            )
        )
        return _Evaluation(
            item=original,
            original=original,
            category=category,
            diagnostics=tuple(
                _contextual_diagnostic(item, kind=kind, item_id=item_id, shot_id=shot_id)
                for item in diagnostics
            ),
            hard_block=required,
            required_unavailable=required,
        )

    if (
        kind == "composition"
        and raw_status in {"", *_PENDING_STATUSES}
        and path_value is None
    ):
        planned = PreflightItem(
            kind="composition",
            id=item_id,
            status="planned",
            path=None,
            reason="成片尚未创建，但已具备可计划状态",
            required=required,
        )
        return _Evaluation(
            item=planned,
            original=None,
            category=None,
            diagnostics=tuple(
                _contextual_diagnostic(item, kind=kind, item_id=item_id, shot_id=shot_id)
                for item in diagnostics
            ),
            hard_block=False,
            required_unavailable=False,
        )

    item = PreflightItem(
        kind=cast("MediaKind", kind),
        id=item_id,
        status="ready",
        path=path,
        reason="",
        required=required,
    )
    return _Evaluation(
        item=item,
        original=None,
        category=None,
        diagnostics=tuple(
            _contextual_diagnostic(item, kind=kind, item_id=item_id, shot_id=shot_id)
            for item in diagnostics
        ),
        hard_block=False,
        required_unavailable=False,
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    )


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _run_ids(snapshot: Mapping[str, Any]) -> tuple[dict[str, Any], str, str, str]:
    run = _model_mapping(snapshot.get("run") or snapshot.get("workflow_run")) or {}
    workflow_run_id = _text(
        snapshot.get("workflow_run_id")
        or run.get("workflow_run_id")
        or run.get("id")
        or snapshot.get("run_id")
    )
    project_id = _text(snapshot.get("project_id") or run.get("project_id"))
    chapter_id = _text(snapshot.get("chapter_id") or run.get("chapter_id"))
    if not workflow_run_id or not project_id or not chapter_id:
        raise PreflightError("snapshot 必须包含 workflow_run_id、project_id 和 chapter_id")
    return run, workflow_run_id, project_id, chapter_id


def _selected_bgm(
    snapshot: Mapping[str, Any], run: Mapping[str, Any], media: Any
) -> list[dict[str, Any]]:
    values: list[Any] = []
    for source in (snapshot.get("bgm"), snapshot.get("bgms"), media):
        if source is not None:
            values.extend(_section_records(source, "bgm"))
    if not values:
        config = _required_config(run, snapshot)
        selected = snapshot.get("bgm_id") or run.get("bgm_id") or config.get("bgm_id")
        if selected:
            values.extend(_record_values(selected))
    return _dedupe_records(values)


def run_composition_preflight(
    snapshot: Mapping[str, Any] | BaseModel,
    *,
    strict: bool = True,
    allow_partial: bool = False,
    project_root: str | Path | None = None,
) -> CompositionPreflightResult:
    """对动态合成快照做纯本地预检。

    ``allow_partial`` 是显式的业务选择，而不是隐式降级。缺失或失败的必需输入
    只有在该参数为真时才会变成 ``skipped``；项目归属、路径安全和 signature
    冲突始终阻断。
    """

    root = _model_mapping(snapshot)
    if root is None:
        raise PreflightError("snapshot 必须是结构化对象")
    run, workflow_run_id, project_id, chapter_id = _run_ids(root)
    media = root.get("media")
    config = _required_config(run, root)
    run_signature = _text(run.get("input_signature") or run.get("signature"))
    snapshot_signature = _text(root.get("input_signature"))
    expected_signature = run_signature or snapshot_signature

    stage_signatures: dict[str, list[str]] = {}
    for stage in _record_values(root.get("stages")):
        stage_id = _text(stage.get("id"))
        stage_key = _text(stage.get("stage_key") or stage.get("key"))
        values: list[str] = []
        for source in (stage, _json_mapping(stage.get("selected_output"))):
            for name in ("input_signature", "upstream_input_signature"):
                value = _text(source.get(name))
                if value and value not in values:
                    values.append(value)
            artifact_meta = source.get("artifact_meta")
            if isinstance(artifact_meta, Mapping):
                for name in ("input_signature", "upstream_input_signature"):
                    value = _text(artifact_meta.get(name))
                    if value and value not in values:
                        values.append(value)
        for key in (stage_id, stage_key):
            if key:
                stage_signatures.setdefault(key, []).extend(
                    value for value in values if value not in stage_signatures.get(key, [])
                )

    diagnostics: list[Diagnostic] = []
    if run_signature and snapshot_signature and run_signature != snapshot_signature:
        diagnostics.append(
            _diagnostic(
                "preflight_signature_mismatch",
                "workflow run 与快照的 input signature 不一致",
                details={
                    "expected_signature": run_signature,
                    "actual_signature": snapshot_signature,
                },
            )
        )

    raw_shots = root.get("shots")
    shots = _shot_records(raw_shots) if raw_shots is not None else []
    top_records = {
        kind: _top_records(root, media, kind) for kind in ("keyframe", "video", "audio", "subtitle")
    }
    if not shots:
        inferred_ids = sorted(
            {
                shot_id
                for records in top_records.values()
                for record in records
                if (shot_id := _record_shot_id(record))
            }
        )
        shots = [{"id": shot_id} for shot_id in inferred_ids]
    if not shots:
        raise PreflightError("snapshot.shots 不能为空，无法确定合成范围")
    shots = sorted(shots, key=_shot_sort_key)

    default_required = {
        "keyframe": _config_bool(config, ("generate_keyframe", "include_keyframe"), True),
        "video": _config_bool(config, ("generate_video", "include_video"), True),
        "audio": _config_bool(config, ("generate_audio", "include_audio"), True),
        "subtitle": _config_bool(config, ("generate_subtitle", "burn_subtitles"), True),
    }
    items: list[PreflightItem] = []
    missing: list[PreflightItem] = []
    failed: list[PreflightItem] = []
    skipped: list[PreflightItem] = []
    required_unavailable = False
    hard_block = bool(diagnostics)
    warning = False

    for shot_record in shots:
        shot_id = _text(shot_record.get("id"))
        shot_project_id = _record_project_id(shot_record)
        if shot_project_id and shot_project_id != project_id:
            diagnostics.append(
                _diagnostic(
                    "preflight_project_mismatch",
                    "Shot 不属于当前项目",
                    details={
                        "shot_id": shot_id,
                        "expected_project_id": project_id,
                        "actual_project_id": shot_project_id,
                    },
                )
            )
            hard_block = True

        for kind in ("keyframe", "video", "audio", "subtitle"):
            records = _records_for_shot(shot_record, kind, top_records[kind], len(shots))
            if not records:
                records = [{"id": f"{kind}:{shot_id}", "shot_id": shot_id}]
            for index, record in enumerate(records, start=1):
                required = _item_required(record, default_required[kind])
                if not _text(record.get("id")):
                    record["id"] = f"{kind}:{shot_id}:{index}"
                evaluation = _evaluate(
                    kind=kind,
                    record=record,
                    shot_id=shot_id,
                    project_id=project_id,
                    chapter_id=chapter_id,
                    workflow_run_id=workflow_run_id,
                    expected_signature=expected_signature,
                    stage_signatures=stage_signatures,
                    required=required,
                    allow_partial=allow_partial,
                    project_root=project_root,
                )
                items.append(evaluation.item)
                diagnostics.extend(evaluation.diagnostics)
                hard_block = hard_block or evaluation.hard_block
                required_unavailable = required_unavailable or evaluation.required_unavailable
                if evaluation.original is not None:
                    if evaluation.category == "missing":
                        missing.append(evaluation.original)
                    else:
                        failed.append(evaluation.original)
                    if evaluation.item.status == "skipped":
                        skipped.append(evaluation.item)
                        warning = True

    bgm_records = _selected_bgm(root, run, media)
    bgm_required = _config_bool(
        config,
        ("bgm_required", "include_bgm", "generate_bgm"),
        False,
    )
    if bgm_records:
        for index, record in enumerate(bgm_records, start=1):
            required = _item_required(record, True)
            if not _text(record.get("id")):
                record["id"] = f"bgm:{index}"
            evaluation = _evaluate(
                kind="bgm",
                record=record,
                shot_id=None,
                project_id=project_id,
                chapter_id=chapter_id,
                workflow_run_id=workflow_run_id,
                expected_signature=expected_signature,
                stage_signatures=stage_signatures,
                required=required,
                allow_partial=allow_partial,
                project_root=project_root,
            )
            items.append(evaluation.item)
            diagnostics.extend(evaluation.diagnostics)
            hard_block = hard_block or evaluation.hard_block
            required_unavailable = required_unavailable or evaluation.required_unavailable
            if evaluation.original is not None:
                if evaluation.category == "missing":
                    missing.append(evaluation.original)
                else:
                    failed.append(evaluation.original)
                if evaluation.item.status == "skipped":
                    skipped.append(evaluation.item)
                    warning = True
    elif bgm_required:
        evaluation = _evaluate(
            kind="bgm",
            record={"id": "bgm:selected"},
            shot_id=None,
            project_id=project_id,
            chapter_id=chapter_id,
            workflow_run_id=workflow_run_id,
            expected_signature=expected_signature,
            stage_signatures=stage_signatures,
            required=True,
            allow_partial=allow_partial,
            project_root=project_root,
        )
        items.append(evaluation.item)
        diagnostics.extend(evaluation.diagnostics)
        hard_block = hard_block or evaluation.hard_block
        required_unavailable = required_unavailable or evaluation.required_unavailable
        if evaluation.original is not None:
            missing.append(evaluation.original)
            if evaluation.item.status == "skipped":
                skipped.append(evaluation.item)
                warning = True

    composition_values: list[dict[str, Any]] = []
    for source in (root.get("composition"), root.get("compositions"), run.get("composition")):
        if source is not None:
            composition_values.extend(_record_values(source))
    composition_values = _dedupe_records(composition_values)
    if not composition_values:
        composition_values = [{"id": f"composition:{chapter_id}", "status": "pending"}]
    composition_needs_job = True
    for index, record in enumerate(composition_values, start=1):
        required = _item_required(record, True)
        if not _text(record.get("id")):
            record["id"] = f"composition:{chapter_id}:{index}"
        raw_status = _text(record.get("status")).casefold()
        if raw_status in _READY_STATUSES and _record_path(record) is not None:
            composition_needs_job = False
        if raw_status in {"queued", "running", "processing"}:
            composition_needs_job = False
        evaluation = _evaluate(
            kind="composition",
            record=record,
            shot_id=None,
            project_id=project_id,
            chapter_id=chapter_id,
            workflow_run_id=workflow_run_id,
            expected_signature=expected_signature,
            stage_signatures=stage_signatures,
            required=required,
            allow_partial=allow_partial,
            project_root=project_root,
        )
        items.append(evaluation.item)
        diagnostics.extend(evaluation.diagnostics)
        hard_block = hard_block or evaluation.hard_block
        required_unavailable = required_unavailable or evaluation.required_unavailable
        if evaluation.original is not None:
            if evaluation.category == "missing":
                missing.append(evaluation.original)
            else:
                failed.append(evaluation.original)
            if evaluation.item.status == "skipped":
                skipped.append(evaluation.item)
                if required:
                    warning = True

    # 最终 manifest 只依赖排序后的相对路径和结构化状态，不包含机器绝对路径。
    kind_rank = {kind: index for index, kind in enumerate(_KIND_ORDER)}
    items.sort(key=lambda item: (kind_rank[item.kind], item.id, item.path or "", item.status))
    missing.sort(key=lambda item: (kind_rank[item.kind], item.id))
    failed.sort(key=lambda item: (kind_rank[item.kind], item.id))
    skipped.sort(key=lambda item: (kind_rank[item.kind], item.id))

    manifest = [item.model_dump(mode="json") for item in items]
    manifest_hash = _hash(manifest)
    input_signature = expected_signature or manifest_hash
    ordered_diagnostics = sorted(
        {_canonical_json(item.model_dump(mode="json")): item for item in diagnostics}.values(),
        key=lambda item: (item.code, item.path or "", item.message, _canonical_json(item.details)),
    )
    if required_unavailable and not allow_partial:
        hard_block = True
    if skipped:
        warning = True
    status: Literal["ready", "warning", "blocked"]
    if hard_block:
        status = "blocked"
    elif warning:
        status = "warning"
    else:
        status = "ready"

    provenance = {
        "preflight_version": "phase-6c-pure-v1",
        "manifest": manifest,
        "manifest_hash": manifest_hash,
        "input_signature": input_signature,
        "expected_signature": expected_signature,
        "estimated_jobs_formula": "one composition job when a new composition may start",
        "project_root_validated": project_root is not None,
        "network_access": False,
        "ffmpeg_called": False,
        "diagnostics": [item.model_dump(mode="json") for item in ordered_diagnostics],
    }
    estimated_jobs = 1 if composition_needs_job and status in {"ready", "warning"} else 0
    return CompositionPreflightResult(
        workflow_run_id=workflow_run_id,
        project_id=project_id,
        chapter_id=chapter_id,
        strict=strict,
        allow_partial=allow_partial,
        status=status,
        items=items,
        missing=missing,
        failed=failed,
        skipped=skipped,
        estimated_jobs=estimated_jobs,
        input_signature=input_signature,
        provenance=provenance,
    )


def can_start_composition(result: CompositionPreflightResult) -> bool:
    """只有无阻断结果，或明确选择部分合成时，才允许创建合成 Job。"""

    if result.status == "ready":
        return True
    return result.status == "warning" and result.allow_partial is True


__all__ = [
    "CompositionPreflightResult",
    "PreflightError",
    "PreflightItem",
    "can_start_composition",
    "run_composition_preflight",
    "validate_relative_media_path",
]
