"""动态漫 Phase 6D 的 Markdown/ZIP 交付包导出。

本模块只处理调用方已经准备好的内存数据，不读取数据库、文件系统或 Provider，
也不改变现有的 legacy 视频导出。所有时间、版本和输入快照都必须由调用方传入。
"""

from __future__ import annotations

import builtins
import hashlib
import io
import json
import re
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

STAGE_EXPORTS: dict[str, str] = {
    "0-source": "00_拆解.md",
    "1-character-bios": "01_人物小传.md",
    "2-rhythm": "02_节奏爽点.md",
    "3-script": "03_详细剧本.md",
    "4-storyboard": "04_标准分镜.md",
    "5a-image-prompts": "05_图片提示词.md",
    "5b-video-prompts": "06_视频提示词.md",
    "README.md": "README.md",
}

_STAGE_KEYS: tuple[str, ...] = tuple(key for key in STAGE_EXPORTS if key != "README.md")
_STAGE_LABELS: dict[str, str] = {
    "0-source": "源文拆解",
    "1-character-bios": "人物小传",
    "2-rhythm": "节奏爽点",
    "3-script": "详细剧本",
    "4-storyboard": "标准分镜",
    "5a-image-prompts": "图片提示词",
    "5b-video-prompts": "视频提示词",
}
_STAGE_ALIASES: dict[str, str] = {
    "0": "0-source",
    "source": "0-source",
    "1": "1-character-bios",
    "character-bios": "1-character-bios",
    "characters": "1-character-bios",
    "2": "2-rhythm",
    "rhythm": "2-rhythm",
    "3": "3-script",
    "script": "3-script",
    "4": "4-storyboard",
    "storyboard": "4-storyboard",
    "5a": "5a-image-prompts",
    "image-prompts": "5a-image-prompts",
    "5a-image": "5a-image-prompts",
    "5b": "5b-video-prompts",
    "video-prompts": "5b-video-prompts",
    "5b-video": "5b-video-prompts",
}

_EXPORTER_VERSION = "dynamic-delivery-exporter-v1"
_SCHEMA_VERSION = "dynamic-delivery-v1"
_FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)

_RAW_KEYS = frozenset(
    {
        "raw",
        "raw_output",
        "raw_output_json",
        "raw_response",
        "provider_raw",
        "response_raw",
    }
)
_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "token",
    "access_token",
    "auth_token",
    "authorization",
    "bearer",
    "cookie",
    "client_secret",
    "secret_key",
    "password",
    "private_key",
    "signed_url",
    "signature_url",
)
_SECRET_QUERY_RE = re.compile(
    r"([?&](?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token|token|"
    r"signature|sig|x-amz-signature|x-goog-signature|x-amz-credential|"
    r"x-amz-security-token|x-amz-signedheaders|ossaccesskeyid|key-pair-id|"
    r"policy|se)=)[^&#\s]+",
    re.IGNORECASE,
)
_LABELLED_SECRET_RE = re.compile(
    r"((?:api[\s_-]*key|access[\s_-]*token|auth(?:orization)?|client[\s_-]*secret|"
    r"cookie|set[\s_-]*cookie|signature|sig|token)"
    r"\s*[:=]\s*)([^\s,;]+)",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"\bBearer\s+[^\s,;]+", re.IGNORECASE)
_COOKIE_HEADER_RE = re.compile(r"\b(?:Cookie|Set-Cookie)\s*:\s*[^\r\n]+", re.IGNORECASE)
_WINDOWS_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/][^\s\"'<>|]+")
_UNC_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])[\\]{2}[^\s\"'<>|]+")
_POSIX_PATH_RE = re.compile(r"(?<![\w.:\-/])/(?!/)[^\s\"'<>|]+")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class _ExportModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _validate_arcname(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("ZIP entry 名称不能为空")
    if "\x00" in value:
        raise ValueError("ZIP entry 名称不能包含 NUL")
    if value.startswith(("/", "\\")):
        raise ValueError("ZIP entry 名称不能是绝对路径")
    normalized = value.replace("\\", "/")
    if _DRIVE_RE.match(normalized):
        raise ValueError("ZIP entry 名称不能包含盘符")
    parts = normalized.split("/")
    if ".." in parts:
        raise ValueError("ZIP entry 名称不能包含 ..")
    if any(part in {"", "."} for part in parts):
        raise ValueError("ZIP entry 名称包含无效路径段")
    return normalized


class ExportEntry(_ExportModel):
    """一个确定性导出文件，同时保留文本和最终字节内容。"""

    arcname: str
    bytes: builtins.bytes = b""
    text: str | None = None
    sha256: str = ""
    size: int = Field(default=0, ge=0)
    kind: str = "document"
    source_id: str = ""
    source_signature: str = ""
    source_attempt: int | None = Field(default=None, ge=1)
    source_revision: int | None = Field(default=None, ge=1)
    attempt: int | None = Field(default=None, ge=1)
    revision: int | None = Field(default=None, ge=1)

    @field_validator("arcname")
    @classmethod
    def validate_entry_path(cls, value: str) -> str:
        return _validate_arcname(value)

    @model_validator(mode="after")
    def populate_digest(self) -> ExportEntry:
        if self.attempt is None:
            self.attempt = self.source_attempt
        elif self.source_attempt is not None and self.attempt != self.source_attempt:
            raise ValueError("ExportEntry 的 attempt 与 source_attempt 不一致")
        if self.revision is None:
            self.revision = self.source_revision
        elif self.source_revision is not None and self.revision != self.source_revision:
            raise ValueError("ExportEntry 的 revision 与 source_revision 不一致")
        if self.text is not None:
            encoded = self.text.encode("utf-8")
            if self.bytes not in (b"", encoded):
                raise ValueError("ExportEntry 的 text 与 bytes 不一致")
            self.bytes = encoded
        self.size = len(self.bytes)
        digest = hashlib.sha256(self.bytes).hexdigest()
        if self.sha256 not in ("", digest):
            raise ValueError("ExportEntry 的 sha256 与 bytes 不一致")
        self.sha256 = digest
        if self.text is None:
            try:
                self.text = self.bytes.decode("utf-8")
            except UnicodeDecodeError:
                self.text = None
        return self


class DeliveryExportInput(_ExportModel):
    """导出所需的完整内存快照。

    ``exported_at`` 没有默认值，避免 exporter 在纯函数内部产生当前时间。
    """

    run: dict[str, Any]
    stages: list[dict[str, Any]] | dict[str, Any]
    checklist: list[dict[str, Any]] | dict[str, Any]
    files: list[dict[str, Any]] | dict[str, Any]
    versions: list[dict[str, Any]] | dict[str, Any]
    media_entries: list[ExportEntry] = Field(default_factory=list)
    export_mode: Literal["strict", "partial"]
    exported_at: str = Field(min_length=1)
    preflight: dict[str, Any] = Field(default_factory=dict)
    input_signature: str = ""
    provenance: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def copy_model_values(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        result = dict(value)
        for key in (
            "run",
            "stages",
            "checklist",
            "files",
            "versions",
            "preflight",
            "provenance",
        ):
            if key in result:
                result[key] = _plain(result[key])
        if "media_entries" in result:
            raw_entries = result["media_entries"]
            if isinstance(raw_entries, Sequence) and not isinstance(raw_entries, str | bytes):
                result["media_entries"] = [
                    item.model_dump(mode="python")
                    if isinstance(item, ExportEntry)
                    else dict(item)
                    if isinstance(item, Mapping)
                    else item
                    for item in raw_entries
                ]
        return result


class DeliveryManifest(_ExportModel):
    """交付包清单；状态字段明确区分 success、partial 和 blocked。"""

    schema_version: str = _SCHEMA_VERSION
    exporter_version: str = _EXPORTER_VERSION
    run_id: str = ""
    exported_at: str = Field(min_length=1)
    export_mode: Literal["strict", "partial"] = "strict"
    status: Literal["success", "partial", "blocked"] = "blocked"
    manifest_arcname: str = "metadata/manifest.json"
    run: dict[str, Any] = Field(default_factory=dict)
    stages: list[dict[str, Any]] = Field(default_factory=list)
    checklist: list[dict[str, Any]] = Field(default_factory=list)
    files: list[dict[str, Any]] = Field(default_factory=list)
    versions: list[dict[str, Any]] = Field(default_factory=list)
    entries: list[dict[str, Any]] = Field(default_factory=list)
    missing: list[dict[str, Any]] = Field(default_factory=list)
    failed: list[dict[str, Any]] = Field(default_factory=list)
    skipped: list[dict[str, Any]] = Field(default_factory=list)
    diagnostics: list[dict[str, Any]] = Field(default_factory=list)
    preflight: dict[str, Any] = Field(default_factory=dict)
    input_signature: str = ""
    provenance: dict[str, Any] = Field(default_factory=dict)

    @field_validator("manifest_arcname")
    @classmethod
    def validate_manifest_path(cls, value: str) -> str:
        return _validate_arcname(value)


def _plain(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _plain(value.model_dump(mode="python"))
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    if isinstance(value, set | frozenset):
        return sorted((_plain(item) for item in value), key=repr)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return f"<binary:{len(value)} bytes>"
    return value


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    return ""


def _json_value(value: Any, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        stripped = value.strip()
        if stripped and stripped[0] in "[{":
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                return value
    return _plain(value)


def _first_stage_value(source: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key not in source:
            continue
        value = _json_value(source[key], None)
        if _has_content(value):
            return source[key]
    return default


def _has_content(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping | list | tuple | set | frozenset):
        return bool(value)
    return True


def _normal_stage_key(value: Any) -> str:
    key = _text(value)
    return _STAGE_ALIASES.get(key, key)


def _stage_records(stages: Any) -> dict[str, dict[str, Any]]:
    value = _plain(stages)
    records: dict[str, dict[str, Any]] = {}
    if isinstance(value, Mapping):
        if value.get("stage_key") or value.get("key"):
            values: Iterable[Any] = (value,)
        else:
            values = (
                {**item, "stage_key": item.get("stage_key", key)}
                if isinstance(item, Mapping)
                else {"stage_key": key, "status": _text(item)}
                for key, item in value.items()
            )
    elif isinstance(value, list):
        values = value
    else:
        values = ()
    for item in values:
        if not isinstance(item, Mapping):
            continue
        record = dict(item)
        key = _normal_stage_key(record.get("stage_key", record.get("key")))
        if key in _STAGE_KEYS:
            records[key] = record
    return records


def _diagnostic_dict(level: str, code: str, message: str, **extra: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"level": level, "code": code, "message": message}
    result.update(extra)
    return result


def _diagnostic_values(record: Mapping[str, Any], gate: Mapping[str, Any]) -> list[dict[str, Any]]:
    values: list[Any] = []
    for source in (record.get("diagnostics"), gate.get("diagnostics")):
        if isinstance(source, list):
            values.extend(source)
    selected = _json_value(
        _first_stage_value(record, "draft_output", "draft_output_json", default={}), {}
    )
    if not _has_content(selected):
        selected = _json_value(
            _first_stage_value(
                record,
                "normalized_output",
                "normalized_output_json",
                default={},
            ),
            {},
        )
    if isinstance(selected, Mapping) and isinstance(selected.get("diagnostics"), list):
        values.extend(selected["diagnostics"])
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        if isinstance(value, BaseModel):
            value = value.model_dump(mode="python")
        if isinstance(value, Mapping):
            item = dict(value)
        else:
            item = _diagnostic_dict("warning", "stage_diagnostic", _text(value) or repr(value))
        key = json.dumps(_plain(item), ensure_ascii=False, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _stage_snapshot(stage_key: str, record: Mapping[str, Any] | None) -> dict[str, Any]:
    source = dict(record or {})
    missing_record = record is None
    raw = _json_value(_first_stage_value(source, "raw_output", "raw_output_json", default={}), {})
    normalized = _json_value(
        _first_stage_value(
            source,
            "normalized_output",
            "normalized_output_json",
            default={},
        ),
        {},
    )
    draft = _json_value(
        _first_stage_value(source, "draft_output", "draft_output_json", default={}), {}
    )
    inline_content = _json_value(
        _first_stage_value(source, "content", "output", "data", default={}), {}
    )
    if not _has_content(draft) and not _has_content(normalized) and _has_content(inline_content):
        normalized = inline_content
    selected = draft if _has_content(draft) else normalized
    selected_source = "draft_output" if _has_content(draft) else "normalized_output"
    gate = _json_value(
        _first_stage_value(source, "gate_result", "gate_result_json", default={}), {}
    )
    if not isinstance(gate, Mapping):
        gate = {}
    gate = dict(gate)
    diagnostics = _diagnostic_values(source, gate)
    status = _text(source.get("status")) or ("missing" if missing_record else "ready")
    error = _text(source.get("error"))
    has_selected = _has_content(selected)
    if missing_record:
        status = "missing"
        diagnostics.insert(
            0,
            _diagnostic_dict("blocking", "stage_missing", f"阶段 {stage_key} 未提供阶段记录"),
        )
    elif not has_selected:
        diagnostics.insert(
            0,
            _diagnostic_dict(
                "blocking",
                "stage_output_missing",
                f"阶段 {stage_key} 缺少 draft_output 和 normalized_output",
            ),
        )
    gate_status = _text(gate.get("status"))
    if gate_status == "blocked" and not any(
        item.get("code") == "stage_gate_blocked" for item in diagnostics
    ):
        diagnostics.append(
            _diagnostic_dict("blocking", "stage_gate_blocked", f"阶段 {stage_key} gate 为 blocked")
        )
    if error and not any(item.get("code") == "stage_error" for item in diagnostics):
        diagnostics.append(_diagnostic_dict("blocking", "stage_error", error))

    selected_mapping = selected if isinstance(selected, Mapping) else {}
    artifact_meta = selected_mapping.get("artifact_meta", selected_mapping.get("meta", {}))
    artifact_meta = artifact_meta if isinstance(artifact_meta, Mapping) else {}
    signatures: dict[str, Any] = {}
    for key in (
        "signature",
        "input_signature",
        "content_signature",
        "own_signature",
        "source_signature",
        "base_compiler_hash",
    ):
        value = _first_stage_value(
            source,
            key,
            default=artifact_meta.get(key, selected_mapping.get(key)),
        )
        if value not in (None, ""):
            signatures[key] = _plain(value)
    provenance = _first_stage_value(
        source,
        "provenance",
        "provenance_json",
        default=selected_mapping.get("provenance", {}),
    )
    if not isinstance(provenance, Mapping):
        provenance = selected_mapping.get("provenance", {})
    if not isinstance(provenance, Mapping):
        provenance = {}

    snapshot = {
        "stage_key": stage_key,
        "label": _STAGE_LABELS[stage_key],
        "status": status,
        "has_output": has_selected,
        "selected_source": selected_source if has_selected else None,
        "draft_output": draft,
        "normalized_output": normalized,
        "raw_output": raw,
        "selected_output": selected,
        "gate_result": gate,
        "diagnostics": diagnostics,
        "error": error,
        "attempt": source.get("attempt", 1),
        "revision": source.get("revision", source.get("confirmed_revision")),
        "input_signature": _first_stage_value(
            source,
            "input_signature",
            default=artifact_meta.get("input_signature", ""),
        ),
        "signatures": signatures,
        "provenance": dict(provenance),
        "output_refs": _json_value(
            _first_stage_value(source, "output_refs", "output_refs_json", default=[]), []
        ),
    }
    return _redact(snapshot)


def _delivery_record_in_scope(
    record: Mapping[str, Any],
    *,
    run: Mapping[str, Any],
    stages: Mapping[str, Mapping[str, Any]],
) -> bool:
    """拒绝把其它 run、stage attempt 或 upstream signature 的媒体带进 manifest。"""
    provenance = record.get("provenance")
    provenance = provenance if isinstance(provenance, Mapping) else {}
    actual_run = _text(
        record.get("workflow_run_id")
        or provenance.get("workflow_run_id")
        or provenance.get("run_id")
    )
    if not actual_run:
        return True
    expected_run = _text(run.get("id") or run.get("run_id"))
    if actual_run != expected_run:
        return False
    kind = _text(record.get("kind") or record.get("target_type")).casefold()
    if kind in {"audio", "subtitle"}:
        # Audio/subtitle rows are generated as delivery child jobs and their own
        # signatures differ from the delivery stage signature. Preflight already
        # validated their current run/stage provenance and file scope.
        return True
    stage_key = {
        "keyframe": "5a-image-prompts",
        "image": "5a-image-prompts",
        "video": "5b-video-prompts",
        "audio": "6-delivery",
        "subtitle": "6-delivery",
    }.get(kind)
    if stage_key is None:
        return True
    expected_stage = stages.get(stage_key)
    if expected_stage is None:
        return False
    actual_stage = _text(
        record.get("workflow_stage_id")
        or record.get("stage_id")
        or provenance.get("workflow_stage_id")
        or provenance.get("stage_id")
    )
    expected_stage_id = _text(expected_stage.get("id"))
    if (
        actual_stage
        and expected_stage_id
        and actual_stage != expected_stage_id
        and stage_key not in {"6-delivery"}
    ):
        return False
    expected_signature = _text(expected_stage.get("input_signature"))
    upstream_signature = _text(
        record.get("upstream_input_signature")
        or provenance.get("upstream_input_signature")
    )
    if (
        stage_key != "6-delivery"
        and expected_signature
        and upstream_signature
        and expected_signature != upstream_signature
    ):
        return False
    expected_attempt = expected_stage.get("attempt")
    actual_attempt = provenance.get("workflow_stage_attempt")
    if expected_attempt is not None and actual_attempt is not None:
        if str(expected_attempt) != str(actual_attempt):
            return False
    return True


def _content_without_raw(value: Any) -> Any:
    value = _plain(value)
    if isinstance(value, Mapping):
        return {
            str(key): _content_without_raw(item)
            for key, item in value.items()
            if re.sub(r"[^a-z0-9]", "", str(key).casefold())
            not in {re.sub(r"[^a-z0-9]", "", raw_key) for raw_key in _RAW_KEYS}
        }
    if isinstance(value, list):
        return [_content_without_raw(item) for item in value]
    return value


def _redact_key(key: Any) -> bool:
    camel_case = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key))
    normalized = re.sub(r"[^a-z0-9_]", "_", camel_case.casefold())
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _redact_text(value: str, *, key: str = "") -> str:
    if not value:
        return value
    if _redact_key(key):
        return "<redacted>"
    result = _COOKIE_HEADER_RE.sub("Cookie: <redacted>", value)
    result = _BEARER_RE.sub("Bearer <redacted>", result)
    result = _SECRET_QUERY_RE.sub(r"\1<redacted>", result)
    result = _LABELLED_SECRET_RE.sub(r"\1<redacted>", result)

    def replace_path(match: re.Match[str]) -> str:
        return "<redacted-path>"

    result = _WINDOWS_PATH_RE.sub(replace_path, result)
    result = _UNC_PATH_RE.sub(replace_path, result)
    result = _POSIX_PATH_RE.sub(replace_path, result)
    return result


def _redact(value: Any, *, key: str = "") -> Any:
    value = _plain(value)
    if isinstance(value, Mapping):
        return {
            str(item_key): "<redacted>"
            if _redact_key(str(item_key))
            else _redact(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item, key=key) for item in value]
    if isinstance(value, str):
        return _redact_text(value, key=key)
    return value


def _json_text(value: Any) -> str:
    return (
        json.dumps(
            _redact(value),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            separators=(",", ": "),
            allow_nan=False,
            default=str,
        )
        + "\n"
    )


def _json_fence(value: Any) -> str:
    return f"```json\n{_json_text(value).rstrip()}\n```"


def _stage_body_markdown(stage_key: str, selected: Any) -> list[str]:
    content = _content_without_raw(selected)
    if not _has_content(content):
        return ["## 状态占位", "该阶段没有可导出的 draft_output 或 normalized_output。"]

    if stage_key not in {"5a-image-prompts", "5b-video-prompts"}:
        return ["## 阶段产物", _json_fence(content)]

    root = content if isinstance(content, Mapping) else {}
    bundles = root.get("bundles") if isinstance(root, Mapping) else None
    lines = ["## Prompt IR", ""]
    if isinstance(bundles, Mapping) and bundles:
        for bundle_key in sorted((str(key) for key in bundles), key=str):
            bundle = bundles.get(bundle_key)
            lines.extend([f"### {bundle_key}", ""])
            if not isinstance(bundle, Mapping):
                lines.append(_json_fence(bundle))
                lines.append("")
                continue
            for layer in ("final", "generated", "human_override"):
                if layer in bundle:
                    lines.extend([f"#### {layer}", "", _json_fence(bundle[layer]), ""])
            metadata = {
                key: value
                for key, value in bundle.items()
                if key not in {"final", "generated", "human_override"}
            }
            if metadata:
                lines.extend(["#### metadata", "", _json_fence(metadata), ""])
        return lines[:-1]

    layers = [layer for layer in ("final", "generated", "human_override") if layer in root]
    if layers:
        for layer in ("final", "generated", "human_override"):
            if layer in root:
                lines.extend([f"### {layer}", "", _json_fence(root[layer]), ""])
        return lines[:-1]
    return ["## Prompt IR", _json_fence(content)]


def render_stage_markdown(
    stage_key: str | Mapping[str, Any],
    stage: Mapping[str, Any] | None = None,
) -> str:
    """渲染一个固定阶段的 Markdown；缺失阶段也会生成状态占位。"""

    if isinstance(stage_key, Mapping):
        if stage is not None:
            raise TypeError("render_stage_markdown 不能同时传入 stage mapping 和 stage")
        raw_stage: Any = _plain(stage_key)
        stage_key = _normal_stage_key(raw_stage.get("stage_key", raw_stage.get("key")))
        stage = raw_stage
    else:
        stage_key = _normal_stage_key(stage_key)
        stage = _plain(stage) if stage is not None else None
    if stage_key not in _STAGE_KEYS:
        raise ValueError(f"不支持的导出阶段：{stage_key!r}")
    if isinstance(stage, Mapping):
        wrapper_keys = {
            "draft_output",
            "normalized_output",
            "raw_output",
            "gate_result",
            "status",
            "attempt",
            "revision",
            "error",
        }
        if not wrapper_keys.intersection(stage):
            stage = {"stage_key": stage_key, "status": "ready", "draft_output": stage}
    snapshot = _stage_snapshot(stage_key, stage)
    title = STAGE_EXPORTS[stage_key].removesuffix(".md")
    lines = [
        f"# {title}",
        "",
        f"- 阶段键：`{stage_key}`",
        f"- 状态：`{snapshot['status']}`",
        f"- 产物来源：`{snapshot['selected_source'] or 'none'}`",
        f"- 门禁状态：`{_text(snapshot['gate_result'].get('status')) or 'unknown'}`",
        f"- attempt：`{snapshot['attempt']}`",
        f"- revision：`{snapshot['revision']}`",
        f"- input_signature：`{_text(snapshot['input_signature']) or 'none'}`",
        "",
    ]
    lines.extend(_stage_body_markdown(stage_key, snapshot.get("selected_output")))
    diagnostics = _content_without_raw(snapshot.get("diagnostics") or [])
    if diagnostics:
        lines.extend(["", "## 诊断", _json_fence(diagnostics)])
    if snapshot.get("error"):
        lines.extend(["", "## 错误", _text(snapshot["error"])])
    return "\n".join(lines).rstrip() + "\n"


def _collection(value: Any) -> list[dict[str, Any]]:
    value = _plain(value)
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, Mapping)]
    if isinstance(value, Mapping):
        if not value:
            return []
        if all(isinstance(item, Mapping) for item in value.values()):
            return [
                {"key": str(key), **dict(item)}
                for key, item in value.items()
                if isinstance(item, Mapping)
            ]
        return [dict(value)]
    return []


_CHECKLIST_DEFINITIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "references",
        "角色/场景参考图",
        (
            "references",
            "reference_images",
            "asset_images",
            "character_scene_reference_images",
            "角色场景参考图",
            "角色/场景参考图",
        ),
    ),
    ("keyframes", "关键帧", ("keyframes", "keyframe", "storyboard_keyframes", "关键帧")),
    ("videos", "分镜视频", ("videos", "video", "shot_videos", "storyboard_videos", "分镜视频")),
    ("audio", "配音", ("audio", "audios", "voiceover", "voiceovers", "配音")),
    ("subtitle", "字幕", ("subtitle", "subtitles", "caption", "captions", "字幕")),
    ("bgm", "BGM", ("bgm", "music", "background_music", "背景音乐")),
    (
        "composition",
        "成片",
        ("composition", "composed_video", "final_video", "finished_video", "成片", "合成"),
    ),
)


def _key_token(value: Any) -> str:
    return re.sub(r"[\s_\-/\\]", "", _text(value).casefold())


def _checklist_lookup(checklist: Any) -> dict[str, Any]:
    lookup: dict[str, Any] = {}
    if isinstance(checklist, Mapping):
        values = checklist.items()
        nested_values = checklist.values()
    elif isinstance(checklist, list):
        values = (
            (
                item.get("key", item.get("kind", item.get("name", item.get("label", "")))),
                item,
            )
            for item in checklist
            if isinstance(item, Mapping)
        )
        nested_values = checklist
    else:
        values = ()
        nested_values = ()
    for key, value in values:
        token = _key_token(key)
        if token:
            lookup[token] = value
    for value in nested_values:
        if isinstance(value, Mapping):
            for key, item in value.items():
                lookup.setdefault(_key_token(key), item)
    return lookup


def _count_values(item: Mapping[str, Any]) -> tuple[int | None, int | None]:
    completed: Any = None
    total: Any = None
    for key in ("completed", "complete", "done", "current", "count", "ready", "success"):
        if key in item and isinstance(item[key], int | float) and not isinstance(item[key], bool):
            completed = int(item[key])
            break
    for key in ("total", "expected", "target", "total_count"):
        if key in item and isinstance(item[key], int | float) and not isinstance(item[key], bool):
            total = int(item[key])
            break
    ratio = item.get("ratio", item.get("progress"))
    if isinstance(ratio, str) and "/" in ratio:
        left, right = ratio.split("/", 1)
        if completed is None and left.strip().isdigit():
            completed = int(left.strip())
        if total is None and right.strip().isdigit():
            total = int(right.strip())
    return completed, total


def _checklist_from_preflight(
    checklist: list[dict[str, Any]] | dict[str, Any],
    preflight: Mapping[str, Any],
) -> list[dict[str, Any]] | dict[str, Any]:
    """把同一份 preflight 结论投影到 manifest checklist，避免状态漂移。"""

    raw_items = preflight.get("items")
    if not isinstance(raw_items, list):
        return checklist
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    aliases = {"keyframe": "keyframes", "video": "videos"}
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            continue
        kind = aliases.get(_text(raw.get("kind")), _text(raw.get("kind")))
        if kind:
            grouped.setdefault(kind, []).append(raw)
    if not grouped:
        return checklist

    lookup = _checklist_lookup(checklist)
    result: list[dict[str, Any]] = []
    for key, label, aliases_for_key in _CHECKLIST_DEFINITIONS:
        raw: Any = None
        for alias in (key, *aliases_for_key):
            if _key_token(alias) in lookup:
                raw = lookup[_key_token(alias)]
                break
        item: dict[str, Any] = (
            dict(raw) if isinstance(raw, Mapping) else {"key": key, "label": label}
        )
        item.setdefault("key", key)
        item.setdefault("label", label)
        values = grouped.get(key)
        if values:
            statuses = {_text(value.get("status")).casefold() for value in values}
            completed_count = sum(
                _text(value.get("status")).casefold() in {"ready", "completed"}
                for value in values
            )
            if "failed" in statuses:
                item["status"] = "failed"
            elif "missing" in statuses:
                item["status"] = "missing"
            elif "skipped" in statuses:
                item["status"] = "skipped"
            elif "planned" in statuses:
                # 部分完成 + 部分仍计划：标记 partial，避免清单虚高为 completed，
                # 掩盖尚未完成的成片。
                item["status"] = (
                    "partial"
                    if completed_count > 0 and completed_count < len(values)
                    else "planned"
                )
            else:
                item["status"] = "completed"
            item["completed"] = completed_count
            item["total"] = len(values)
            reasons = [
                _text(value.get("reason"))
                for value in values
                if _text(value.get("reason"))
            ]
            if reasons:
                item["reason"] = "; ".join(dict.fromkeys(reasons))
            details_value = item.get("details")
            details = dict(details_value) if isinstance(details_value, Mapping) else {}
            item["details"] = {
                **details,
                "preflight_item_ids": [str(value.get("id")) for value in values],
            }
        result.append(item)
    return result


def _normalize_checklist(
    checklist: list[dict[str, Any]] | dict[str, Any], run: Mapping[str, Any]
) -> list[dict[str, Any]]:
    lookup = _checklist_lookup(checklist)
    config = run.get("config_snapshot", {})
    config = config if isinstance(config, Mapping) else {}
    result: list[dict[str, Any]] = []
    for key, label, aliases in _CHECKLIST_DEFINITIONS:
        raw: Any = None
        found = False
        for alias in aliases:
            token = _key_token(alias)
            if token in lookup:
                raw = lookup[token]
                found = True
                break
        item = dict(raw) if isinstance(raw, Mapping) else {}
        status = _text(item.get("status")).casefold()
        reason = _text(item.get("reason", item.get("message")))
        completed, total = _count_values(item)
        if isinstance(raw, bool):
            status = "completed" if raw else "missing"
            completed, total = (1 if raw else 0), 1
        elif isinstance(raw, int | float) and not isinstance(raw, bool):
            completed, total = int(raw), 1
        if key == "bgm":
            if status in {"selected", "completed", "ready"} or item.get("selected") is True:
                display_status = "selected"
            else:
                display_status = "none"
                reason = reason or "未选择 BGM"
            completed = (
                completed if completed is not None else (1 if display_status == "selected" else 0)
            )
            total = total if total is not None else 1
        elif key == "composition":
            enabled = config.get("generate_composition", True)
            if not enabled and not found:
                display_status = "skipped"
                reason = reason or "运行配置未启用成片合成"
            elif status in {"completed", "ready", "success"} or item.get("completed") is True:
                display_status = "completed"
            elif status in {"failed", "error"}:
                display_status = "failed"
            elif status in {"skipped", "cancelled"}:
                display_status = "skipped"
                reason = reason or "成片合成被跳过"
            elif status in {"planned", "pending", "queued", "running", "processing"}:
                display_status = "planned"
                reason = reason or "成片尚未创建，但已具备可计划状态"
            else:
                display_status = "planned"
                reason = reason or ("未提供成片" if not found else "成片尚未完成")
            completed = (
                completed if completed is not None else (1 if display_status == "completed" else 0)
            )
            total = total if total is not None else 1
        else:
            enabled_key = {
                "audio": "generate_audio",
                "subtitle": "generate_subtitle",
            }.get(key)
            if enabled_key and not config.get(enabled_key, True) and not found:
                display_status = "skipped"
                reason = reason or f"运行配置未启用{label}"
            elif status in {"failed", "error", "blocked", "partial_failed", "interrupted"}:
                display_status = "failed"
            elif status in {"skipped", "cancelled"}:
                display_status = "skipped"
            elif status in {"completed", "ready", "success"}:
                display_status = "completed"
            elif completed is not None and total is not None and completed >= total > 0:
                display_status = "completed"
            elif completed is not None and completed > 0:
                display_status = "partial"
                reason = reason or f"仅完成 {completed}/{total or '?'}"
            else:
                display_status = "missing"
                reason = reason or ("未提供清单项" if not found else f"{label}尚未完成")
            completed = completed if completed is not None else 0
            total = total if total is not None else 0
        result.append(
            _redact(
                {
                    "key": key,
                    "label": label,
                    "status": display_status,
                    "completed": completed,
                    "total": total,
                    "reason": reason,
                    "details": item,
                }
            )
        )
    return result


def _issue_lists(
    snapshots: Sequence[Mapping[str, Any]],
    checklist: Sequence[Mapping[str, Any]],
    run: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    missing: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for snapshot in snapshots:
        key = _text(snapshot.get("stage_key"))
        status = _text(snapshot.get("status")).casefold()
        diagnostics = snapshot.get("diagnostics")
        diagnostics = diagnostics if isinstance(diagnostics, list) else []
        reason = _text(snapshot.get("error")) or next(
            (_text(item.get("message")) for item in diagnostics if isinstance(item, Mapping)),
            "阶段未通过导出门禁",
        )
        if status in {"skipped", "cancelled"}:
            skipped.append({"kind": "stage", "stage_key": key, "reason": reason})
        elif status in {"failed", "blocked", "partial_failed", "interrupted"}:
            failed.append({"kind": "stage", "stage_key": key, "status": status, "reason": reason})
        elif status == "missing" or not snapshot.get("has_output", False):
            missing.append({"kind": "stage", "stage_key": key, "reason": reason})
        elif status not in {"confirmed", "ready", "warning", "completed", "success"}:
            failed.append({"kind": "stage", "stage_key": key, "status": status, "reason": reason})
        gate = snapshot.get("gate_result")
        if isinstance(gate, Mapping) and (
            gate.get("status") in {"blocked", "partial_failed"} or gate.get("passed") is False
        ):
            if not any(item.get("stage_key") == key for item in failed):
                failed.append({"kind": "stage_gate", "stage_key": key, "reason": reason})

    for item in checklist:
        key = _text(item.get("key"))
        status = _text(item.get("status")).casefold()
        reason = _text(item.get("reason")) or f"{item.get('label', key)} 状态为 {status}"
        if status in {"missing", "partial", "pending"}:
            missing.append(
                {
                    "kind": "checklist",
                    "item": key,
                    "label": item.get("label", key),
                    "reason": reason,
                    "completed": item.get("completed", 0),
                    "total": item.get("total", 0),
                }
            )
        elif status == "failed":
            failed.append({"kind": "checklist", "item": key, "reason": reason})
        elif status == "skipped":
            skipped.append({"kind": "checklist", "item": key, "reason": reason})
        elif status == "planned":
            # composition planned 是合法的交付计划态，不属于 missing。
            continue

    run_status = _text(run.get("status")).casefold()
    if run_status in {"blocked", "failed", "partial_failed", "interrupted", "cancelled"}:
        failed.append(
            {
                "kind": "run",
                "run_id": _text(run.get("id", run.get("run_id"))),
                "status": run_status,
                "reason": _text(run.get("error")) or f"运行状态为 {run_status}",
            }
        )
    return missing, failed, skipped


def _stage_entries(input_data: DeliveryExportInput) -> list[ExportEntry]:
    records = _stage_records(input_data.stages)
    entries: list[ExportEntry] = []
    for stage_key in _STAGE_KEYS:
        entries.append(
            ExportEntry(
                arcname=STAGE_EXPORTS[stage_key],
                text=render_stage_markdown(stage_key, records.get(stage_key)),
            )
        )
    return entries


def _stage_metadata_entries(input_data: DeliveryExportInput) -> list[ExportEntry]:
    records = _stage_records(input_data.stages)
    return [
        ExportEntry(
            arcname=f"metadata/stages/{stage_key}.json",
            text=_json_text(_stage_snapshot(stage_key, records.get(stage_key))),
        )
        for stage_key in _STAGE_KEYS
    ]


def _entry_summary(entry: ExportEntry) -> dict[str, Any]:
    result = {
        "arcname": entry.arcname,
        "sha256": entry.sha256,
        "size": entry.size,
    }
    if entry.kind != "document" or entry.source_id or entry.source_signature:
        result.update(
            {
                "kind": entry.kind,
                "source_id": entry.source_id,
                "source_signature": entry.source_signature,
                "source_attempt": entry.source_attempt,
                "source_revision": entry.source_revision,
                "attempt": entry.attempt,
                "revision": entry.revision,
            }
        )
    return result


def _manifest_base(
    input_data: DeliveryExportInput,
    entry_summaries: Sequence[Mapping[str, Any]],
) -> DeliveryManifest:
    run = _redact(_plain(input_data.run))
    if not isinstance(run, Mapping):
        run = {}
    records = _stage_records(input_data.stages)
    snapshots = [_stage_snapshot(key, records.get(key)) for key in _STAGE_KEYS]
    preflight = _plain(input_data.preflight)
    preflight = preflight if isinstance(preflight, Mapping) else {}
    checklist_input = _checklist_from_preflight(input_data.checklist, preflight)
    checklist = _normalize_checklist(checklist_input, run)
    missing, failed, skipped = _issue_lists(snapshots, checklist, run)
    for field, target in (("missing", missing), ("failed", failed), ("skipped", skipped)):
        values = preflight.get(field)
        if not isinstance(values, list):
            continue
        for value in values:
            if not isinstance(value, Mapping):
                continue
            item = {"kind": "preflight", "source": field, **dict(value)}
            if item not in target:
                target.append(item)
    versions = _collection(input_data.versions)
    files = _collection(input_data.files)
    scoped_versions = [
        item
        for item in versions
        if _delivery_record_in_scope(item, run=run, stages=records)
    ]
    scoped_files = [
        item
        for item in files
        if _delivery_record_in_scope(item, run=run, stages=records)
    ]
    omitted_media = [
        item
        for item in (*versions, *files)
        if not _delivery_record_in_scope(item, run=run, stages=records)
    ]
    for item in omitted_media:
        failed.append(
            {
                "kind": "delivery_scope",
                "item_id": _text(item.get("id")) or "unknown",
                "reason": "媒体不属于当前 delivery attempt/revision，已从交付包排除",
            }
        )
    preflight_status = _text(preflight.get("status")).casefold()
    if preflight_status == "blocked":
        status: Literal["success", "partial", "blocked"] = (
            "blocked" if input_data.export_mode == "strict" else "partial"
        )
    elif preflight_status == "warning":
        status = "partial" if input_data.export_mode == "partial" else "blocked"
    elif missing or failed or skipped:
        status: Literal["success", "partial", "blocked"] = (
            "blocked" if input_data.export_mode == "strict" else "partial"
        )
    else:
        status = "success"
    diagnostics = [
        {
            "source": "stage",
            "stage_key": snapshot["stage_key"],
            "diagnostic": item,
        }
        for snapshot in snapshots
        for item in snapshot.get("diagnostics", [])
        if isinstance(item, Mapping)
    ]
    diagnostics.extend(
        {
            "source": "delivery_scope",
            "diagnostic": {
                "level": "blocking",
                "code": "delivery_media_scope_mismatch",
                "message": "媒体不属于当前 delivery attempt/revision，已从交付包排除",
            },
        }
        for _item in omitted_media
    )
    provenance = run.get("provenance", {}) if isinstance(run, Mapping) else {}
    if not isinstance(provenance, Mapping):
        provenance = {}
    export_provenance = _plain(input_data.provenance)
    if not isinstance(export_provenance, Mapping):
        export_provenance = {}
    input_signature = (
        _text(input_data.input_signature)
        or _text(preflight.get("input_signature"))
        or _text(run.get("input_signature"))
    )
    manifest_provenance = {
        **dict(provenance),
        **dict(export_provenance),
        "input_signature": input_signature,
        "preflight": _plain(preflight.get("provenance", {})),
    }
    return DeliveryManifest(
        run_id=_text(run.get("id", run.get("run_id"))) if isinstance(run, Mapping) else "",
        exported_at=input_data.exported_at,
        export_mode=input_data.export_mode,
        status=status,
        run=dict(run),
        stages=[_redact(snapshot) for snapshot in snapshots],
        checklist=checklist,
        files=[_redact(item) for item in scoped_files],
        versions=[_redact(item) for item in scoped_versions],
        entries=[_redact(dict(item)) for item in entry_summaries],
        missing=[_redact(item) for item in missing],
        failed=[_redact(item) for item in failed],
        skipped=[_redact(item) for item in skipped],
        diagnostics=_redact(diagnostics),
        preflight=_redact(dict(preflight)),
        input_signature=input_signature,
        provenance=_redact(manifest_provenance),
    )


def _readme_markdown(input_data: DeliveryExportInput, manifest: DeliveryManifest) -> str:
    lines = [
        "# 动态漫交付包",
        "",
        f"- run_id：`{manifest.run_id or 'unknown'}`",
        f"- export_mode：`{manifest.export_mode}`",
        f"- exported_at：`{manifest.exported_at}`",
        f"- export_status：`{manifest.status}`",
        f"- schema_version：`{manifest.schema_version}`",
        f"- exporter_version：`{manifest.exporter_version}`",
        "",
        "## 交付检查清单",
        "",
        "| 项目 | 状态 | 已完成 | 总数 | 原因 |",
        "| --- | --- | ---: | ---: | --- |",
    ]
    for item in manifest.checklist:
        lines.append(
            "| {label} | `{status}` | {completed} | {total} | {reason} |".format(
                label=item.get("label", ""),
                status=item.get("status", ""),
                completed=item.get("completed", 0),
                total=item.get("total", 0),
                reason=item.get("reason", "") or "",
            )
        )
    lines.extend(
        [
            "",
            "## 阶段状态",
            "",
            "| 阶段 | 状态 | 产物来源 | 门禁 |",
            "| --- | --- | --- | --- |",
        ]
    )
    for stage in manifest.stages:
        gate = stage.get("gate_result", {})
        gate_status = gate.get("status", "unknown") if isinstance(gate, Mapping) else "unknown"
        lines.append(
            f"| {stage.get('label', stage.get('stage_key', ''))} | "
            f"`{stage.get('status', '')}` | `{stage.get('selected_source') or 'none'}` | "
            f"`{gate_status}` |"
        )
    for heading, values in (
        ("缺失项", manifest.missing),
        ("失败项", manifest.failed),
        ("跳过项", manifest.skipped),
    ):
        if values:
            lines.extend(["", f"## {heading}", "", _json_fence(values)])
    lines.extend(
        [
            "",
            "## 说明",
            "",
            "本包为纯 Markdown/ZIP 交付快照；raw provider 输出只保存在 metadata/ 结构化 JSON，"
            "普通 Markdown 正文使用 draft_output（非空时优先）或 normalized_output。",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def _non_manifest_entries(
    input_data: DeliveryExportInput,
    manifest: DeliveryManifest | None = None,
) -> list[ExportEntry]:
    stage_entries = _stage_entries(input_data)
    metadata_entries = _stage_metadata_entries(input_data)
    media_entries = list(input_data.media_entries)
    if manifest is None:
        preliminary = _manifest_base(
            input_data,
            [
                _entry_summary(entry)
                for entry in (*stage_entries, *metadata_entries, *media_entries)
            ],
        )
        manifest = preliminary
    readme = ExportEntry(arcname="README.md", text=_readme_markdown(input_data, manifest))
    return [*stage_entries, readme, *metadata_entries, *media_entries]


def _coerce_input(value: DeliveryExportInput | Mapping[str, Any]) -> DeliveryExportInput:
    if isinstance(value, DeliveryExportInput):
        return value
    return DeliveryExportInput.model_validate(value)


def build_delivery_manifest(
    value: DeliveryExportInput | Mapping[str, Any],
) -> DeliveryManifest:
    """构建脱敏、确定性的交付 manifest。"""

    input_data = _coerce_input(value)
    stage_entries = _stage_entries(input_data)
    metadata_entries = _stage_metadata_entries(input_data)
    media_entries = list(input_data.media_entries)
    preliminary = _manifest_base(
        input_data,
        [
            _entry_summary(entry)
            for entry in (*stage_entries, *metadata_entries, *media_entries)
        ],
    )
    readme = ExportEntry(arcname="README.md", text=_readme_markdown(input_data, preliminary))
    return _manifest_base(
        input_data,
        [
            _entry_summary(entry)
            for entry in (*stage_entries, readme, *metadata_entries, *media_entries)
        ],
    )


def build_delivery_files(
    value: DeliveryExportInput | Mapping[str, Any],
    manifest: DeliveryManifest | Mapping[str, Any] | None = None,
) -> list[ExportEntry]:
    """构建固定顺序的 Markdown 和结构化 JSON 文件，不写入文件系统。"""

    input_data = _coerce_input(value)
    current_manifest: DeliveryManifest
    if manifest is None:
        current_manifest = build_delivery_manifest(input_data)
    elif isinstance(manifest, DeliveryManifest):
        current_manifest = manifest
    else:
        current_manifest = DeliveryManifest.model_validate(manifest)
    base_entries = _non_manifest_entries(input_data, current_manifest)
    manifest_entry = ExportEntry(
        arcname="metadata/manifest.json",
        text=_json_text(current_manifest.model_dump(mode="json")),
    )
    return _ordered_entries([*base_entries, manifest_entry])


def _ordered_entries(entries: Iterable[ExportEntry]) -> list[ExportEntry]:
    validated: list[ExportEntry] = []
    seen: set[str] = set()
    for entry in entries:
        current = entry if isinstance(entry, ExportEntry) else ExportEntry.model_validate(entry)
        current.arcname = _validate_arcname(current.arcname)
        if current.arcname in seen:
            raise ValueError(f"ZIP entry 名称重复：{current.arcname}")
        seen.add(current.arcname)
        validated.append(current)

    def order(entry: ExportEntry) -> tuple[int, str]:
        if entry.arcname in STAGE_EXPORTS.values():
            return (list(STAGE_EXPORTS.values()).index(entry.arcname), entry.arcname)
        if entry.arcname == "metadata/manifest.json":
            return (len(STAGE_EXPORTS.values()), entry.arcname)
        if entry.arcname.startswith("metadata/stages/"):
            stage_name = entry.arcname.removeprefix("metadata/stages/").removesuffix(".json")
            try:
                stage_index = _STAGE_KEYS.index(stage_name)
            except ValueError:
                stage_index = len(_STAGE_KEYS)
            return (len(STAGE_EXPORTS.values()) + 1 + stage_index, entry.arcname)
        return (len(STAGE_EXPORTS.values()) + 2 + len(_STAGE_KEYS), entry.arcname)

    return sorted(validated, key=order)


def build_zip_bytes(
    entries: Iterable[ExportEntry | Mapping[str, Any]] | DeliveryExportInput | Mapping[str, Any],
) -> bytes:
    """使用固定时间戳、属性和顺序生成确定性 ZIP 字节。"""

    if isinstance(entries, DeliveryExportInput):
        entries = build_delivery_files(entries)
    elif isinstance(entries, Mapping) and {
        "run",
        "stages",
        "checklist",
        "files",
        "versions",
        "export_mode",
        "exported_at",
    }.issubset(entries):
        entries = build_delivery_files(cast(Mapping[str, Any], entries))
    normalized = _ordered_entries(
        entry if isinstance(entry, ExportEntry) else ExportEntry.model_validate(entry)
        for entry in entries
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(
        buffer,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for entry in normalized:
            arcname = _validate_arcname(entry.arcname)
            info = zipfile.ZipInfo(filename=arcname, date_time=_FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 0
            info.create_version = 20
            info.extract_version = 20
            info.flag_bits = 0x800
            info.external_attr = 0
            info.internal_attr = 0
            info.extra = b""
            info.comment = b""
            archive.writestr(info, entry.bytes, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return buffer.getvalue()


__all__ = [
    "DeliveryExportInput",
    "DeliveryManifest",
    "ExportEntry",
    "STAGE_EXPORTS",
    "build_delivery_files",
    "build_delivery_manifest",
    "build_zip_bytes",
    "render_stage_markdown",
]
