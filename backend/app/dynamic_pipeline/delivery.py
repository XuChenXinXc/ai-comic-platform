"""动态工作流的交付快照、6-delivery 编排、预检和 Markdown/ZIP 导出服务。

``DynamicDeliveryService`` 只从数据库读取已经存在的运行、阶段和媒体记录。媒体路径
保持为数据库中的项目相对字符串，预检阶段才负责校验路径；6-delivery 的执行由
``DynamicDeliveryJobService`` 负责，并复用已有音频、字幕和合成服务。
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.config import settings
from app.db import Database
from app.dynamic_pipeline.audio_delivery import (
    AudioDeliveryError,
    build_audio_delivery_plan,
)
from app.dynamic_pipeline.composition_preflight import (
    CompositionPreflightResult,
    PreflightItem,
    can_start_composition,
    run_composition_preflight,
    validate_relative_media_path,
)
from app.dynamic_pipeline.exporter import (
    DeliveryExportInput,
    ExportEntry,
    build_delivery_files,
    build_delivery_manifest,
    build_zip_bytes,
)
from app.dynamic_pipeline.subtitle_delivery import (
    SubtitleTimelineError,
    build_subtitle_lines,
)
from app.jobs.repository import NON_TERMINAL_STATUSES, JobRepository
from app.models.tables import (
    api_provider,
    asset,
    audio_config,
    chapter,
    chapter_composition,
    job,
    keyframe_version,
    project,
    project_bgm,
    shot,
    shot_audio,
    shot_subtitle,
    video_version,
    workflow_run,
    workflow_stage,
)
from app.services.file_service import FileService
from app.services.signature_service import calculate_signature
from app.services.utils import json_dumps, json_loads, row_dict, sanitize_error_message, utc_now


class DeliveryServiceError(RuntimeError):
    """动态交付快照、预检或导出失败。"""

    def __init__(
        self,
        message: str = "",
        *,
        code: str = "delivery_error",
        http_status: int = 400,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status


class DeliveryExecutionError(DeliveryServiceError):
    """6-delivery 子任务编排或执行失败。"""


class DeliveryDependencyError(DeliveryExecutionError):
    """6-delivery 媒体屏障未满足或存在失败前置任务。"""


class DeliveryStaleError(DeliveryExecutionError):
    """写入终态时发现 run/stage/Job 已被新 attempt 取代，属于过期而非失败。"""


_RUN_JSON_FIELDS: tuple[tuple[str, str, Any], ...] = (
    ("source_snapshot_json", "source_snapshot", {}),
    ("config_snapshot_json", "config_snapshot", {}),
    ("asset_mapping_json", "asset_mapping", []),
)
_STAGE_JSON_FIELDS: tuple[tuple[str, str, Any], ...] = (
    ("raw_output_json", "raw_output", {}),
    ("normalized_output_json", "normalized_output", {}),
    ("draft_output_json", "draft_output", {}),
    ("output_refs_json", "output_refs", []),
    ("gate_result_json", "gate_result", {}),
)
_SHOT_JSON_FIELDS: tuple[tuple[str, str, Any], ...] = (
    ("character_ids_json", "character_ids", []),
    ("prop_ids_json", "prop_ids", []),
    ("positions_json", "positions", []),
    ("action_events_json", "action_events", []),
    ("expressions_json", "expressions", []),
    ("dialogue_json", "dialogue", []),
    ("dialogue_cues_json", "dialogue_cues", []),
    ("sound_cues_json", "sound_cues", []),
    ("continuity_json", "continuity", {}),
    ("pipeline_metadata_json", "pipeline_metadata", {}),
    ("reference_bindings_json", "reference_bindings", []),
)
_KEYFRAME_JSON_FIELDS: tuple[tuple[str, str, Any], ...] = (
    ("reference_bindings_json", "reference_bindings", []),
    ("prompt_ir_json", "prompt_ir", {}),
    ("provenance_json", "provenance", {}),
)
_VIDEO_JSON_FIELDS: tuple[tuple[str, str, Any], ...] = (
    ("prompt_ir_json", "prompt_ir", {}),
    ("provenance_json", "provenance", {}),
)
_AUDIO_JSON_FIELDS: tuple[tuple[str, str, Any], ...] = (("provenance_json", "provenance", {}),)
_SUBTITLE_JSON_FIELDS: tuple[tuple[str, str, Any], ...] = (("provenance_json", "provenance", {}),)
_BGM_JSON_FIELDS: tuple[tuple[str, str, Any], ...] = (("mood_tags", "mood_tags", []),)
_COMPOSITION_JSON_FIELDS: tuple[tuple[str, str, Any], ...] = (
    ("preflight_json", "preflight", {}),
    ("provenance_json", "provenance", {}),
)

_MEDIA_SECRET_KEY_PARTS = (
    "api_key",
    "apikey",
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
    r"x-amz-security-token|ossaccesskeyid|key-pair-id|policy|se)=)[^&#\s]+",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"\bBearer\s+[^\s,;]+", re.IGNORECASE)
_COOKIE_HEADER_RE = re.compile(r"\b(?:Cookie|Set-Cookie)\s*:\s*[^\r\n]+", re.IGNORECASE)
_LABELLED_SECRET_RE = re.compile(
    r"((?:api[\s_-]*key|access[\s_-]*token|auth(?:orization)?|client[\s_-]*secret|"
    r"cookie|set[\s_-]*cookie|signature|sig|token)\s*[:=]\s*)([^\s,;]+)",
    re.IGNORECASE,
)

_READY_STATUSES = frozenset(
    {"completed", "complete", "confirmed", "ready", "available", "success", "succeeded", "ok"}
)
_DELIVERY_DEPENDENCY_JOB_TYPES = frozenset(
    {
        "generate_keyframe",
        "generate_video",
        "generate_dialogue_audio",
        "generate_dynamic_subtitle",
    }
)


def _plain(value: Any) -> Any:
    """把数据库值转换成只包含 JSON 基础类型的副本。"""

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


def _json_value(value: Any, default: Any) -> Any:
    if value is None:
        return copy.deepcopy(default)
    if isinstance(value, str):
        parsed = json_loads(value, None)
        if parsed is None:
            return copy.deepcopy(default)
        return _plain(parsed)
    if isinstance(value, Mapping | list | tuple):
        return _plain(value)
    return copy.deepcopy(default)


def _redact_key(key: str) -> bool:
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key).casefold()
    normalized = re.sub(r"[^a-z0-9_]", "_", normalized)
    return any(part in normalized for part in _MEDIA_SECRET_KEY_PARTS)


def _redact_text(value: str) -> str:
    value = _COOKIE_HEADER_RE.sub("Cookie: <redacted>", value)
    value = _BEARER_RE.sub("Bearer <redacted>", value)
    value = _SECRET_QUERY_RE.sub(r"\1<redacted>", value)
    return _LABELLED_SECRET_RE.sub(r"\1<redacted>", value)


def _redact(value: Any) -> Any:
    value = _plain(value)
    if isinstance(value, Mapping):
        return {
            str(key): "<redacted>" if _redact_key(str(key)) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _snapshot_path(value: Any) -> str | None:
    """保留数据库路径的形状，不解析、不打开，也不根据名称重建路径。"""

    if value is None:
        return None
    if isinstance(value, str):
        return value.replace("\\", "/")
    return str(value).replace("\\", "/")


def _truthy_current(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)


def _status(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _media_provenance_ids(record: Mapping[str, Any]) -> tuple[str, str, bool]:
    provenance = record.get("provenance")
    provenance = provenance if isinstance(provenance, Mapping) else {}
    workflow_run_id = _status(
        record.get("workflow_run_id")
        or provenance.get("workflow_run_id")
        or provenance.get("run_id")
    )
    workflow_stage_id = _status(
        record.get("workflow_stage_id")
        or record.get("stage_id")
        or provenance.get("workflow_stage_id")
        or provenance.get("stage_id")
    )
    has_provenance = bool(provenance) or bool(
        record.get("workflow_run_id")
        or record.get("workflow_stage_id")
        or record.get("stage_id")
    )
    return workflow_run_id, workflow_stage_id, has_provenance


def _queue_version_ids(
    stages: Sequence[Mapping[str, Any]],
    run_id: str,
    kind: str,
) -> set[str]:
    expected_kind = "image" if kind == "keyframe" else "video" if kind == "video" else ""
    if not expected_kind:
        return set()
    result: set[str] = set()
    for stage in stages:
        stage_id = _status(stage.get("id"))
        stage_signature = _status(stage.get("input_signature"))
        refs = stage.get("output_refs")
        if not isinstance(refs, list):
            continue
        for ref in refs:
            if not isinstance(ref, Mapping) or ref.get("capture_mode") != "dynamic_media_queue":
                continue
            ref_run_id = _status(ref.get("workflow_run_id") or ref.get("run_id"))
            if ref_run_id and ref_run_id != run_id:
                continue
            ref_stage_id = _status(ref.get("workflow_stage_id") or ref.get("stage_id"))
            if ref_stage_id and stage_id and ref_stage_id != stage_id:
                continue
            ref_signature = _status(ref.get("input_signature"))
            if ref_signature and stage_signature and ref_signature != stage_signature:
                continue
            for version_id in ref.get("version_ids", []):
                if version_id:
                    result.add(str(version_id))
            targets = ref.get("targets")
            if not isinstance(targets, list):
                continue
            for target in targets:
                if not isinstance(target, Mapping) or target.get("kind") != expected_kind:
                    continue
                version_id = target.get("version_id")
                if version_id:
                    result.add(str(version_id))
    return result


def _media_scope(
    records: Sequence[Mapping[str, Any]],
    *,
    run_id: str,
    stage_ids: set[str],
    queue_version_ids: set[str],
    kind: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """选择当前动态运行的媒体，并在完全没有动态媒体时回退 legacy。"""

    dynamic_records: list[dict[str, Any]] = []
    legacy_records: list[dict[str, Any]] = []
    target_has_dynamic = False
    for raw in records:
        record = dict(raw)
        record_id = _status(record.get("id"))
        workflow_run_id, workflow_stage_id, has_provenance = _media_provenance_ids(record)
        referenced = bool(record_id and record_id in queue_version_ids)
        if referenced:
            target_has_dynamic = True
            dynamic_records.append(record)
            continue
        if workflow_run_id:
            if workflow_run_id == run_id:
                target_has_dynamic = True
                if workflow_stage_id and workflow_stage_id in stage_ids:
                    dynamic_records.append(record)
            continue
        if has_provenance:
            # 有动态 provenance 但缺少 run 归属时不能伪装成 legacy 版本。
            target_has_dynamic = True
            continue
        legacy_records.append(record)

    referenced_dynamic = [
        record
        for record in dynamic_records
        if _status(record.get("id")) in queue_version_ids
    ]
    if referenced_dynamic:
        dynamic_records = referenced_dynamic
    scoped = dynamic_records if target_has_dynamic else legacy_records
    if not scoped:
        return [], []

    if kind in {"keyframe", "video"}:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for record in scoped:
            grouped.setdefault(_status(record.get("shot_id")), []).append(record)
        selected: list[dict[str, Any]] = []
        for values in grouped.values():
            selected.append(
                max(
                    values,
                    key=lambda item: (
                        1 if _truthy_current(item.get("current")) else 0,
                        int(item.get("version_no") or 0),
                        _status(item.get("created_at")),
                        _status(item.get("id")),
                    ),
                )
            )
        return scoped, selected

    if kind == "audio":
        grouped_audio: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for record in scoped:
            key = (_status(record.get("shot_id")), _status(record.get("dialogue_line_id")))
            grouped_audio.setdefault(key, []).append(record)
        selected_audio = [
            max(
                values,
                key=lambda item: (
                    1 if _truthy_current(item.get("current")) else 0,
                    int(item.get("version_no") or 0),
                    _status(item.get("created_at")),
                    _status(item.get("id")),
                ),
            )
            for values in grouped_audio.values()
        ]
        return scoped, selected_audio

    return scoped, list(scoped)


def _decode_row(
    row: Any,
    json_fields: Sequence[tuple[str, str, Any]] = (),
    *,
    media: bool = False,
    current: bool | None = None,
) -> dict[str, Any]:
    record = row_dict(row)
    if record is None:
        return {}
    result = _plain(record)
    for database_name, public_name, default in json_fields:
        result.pop(database_name, None)
        result[public_name] = _json_value(record.get(database_name), default)

    if "file_path" in result:
        result["file_path"] = _snapshot_path(result.get("file_path"))
        result["path"] = result["file_path"]
    if "end_frame_path" in result:
        result["end_frame_path"] = _snapshot_path(result.get("end_frame_path"))
    # Provider URL 不是本地交付路径，且可能携带签名参数；交付快照不需要它。
    result.pop("provider_url", None)

    if media:
        if current is None:
            current = _truthy_current(result.get("is_current"))
        result["current"] = current
        result["status"] = _status(result.get("status"))
        result["input_signature"] = _status(result.get("input_signature"))
        provenance = result.get("provenance")
        result["provenance"] = provenance if isinstance(provenance, Mapping) else {}
    return _redact(result)


def _record_ready(record: Mapping[str, Any], *, kind: str = "") -> bool:
    status = _status(record.get("status")).casefold()
    if status:
        return status in _READY_STATUSES
    # shot_subtitle 没有 status 列：用「存在有效文本 + 时间轴」作为完成判据，
    # 避免交付清单里的字幕项无论是否生成都显示 missing。
    if kind == "subtitle" or record.get("kind") == "subtitle":
        text = str(record.get("text") or record.get("content") or "").strip()
        start_time = record.get("start_time")
        end_time = record.get("end_time")
        return bool(
            text
            and isinstance(start_time, int | float)
            and isinstance(end_time, int | float)
            and float(end_time) > float(start_time)
        )
    return False


def _checklist_media_item(
    key: str,
    label: str,
    records: Sequence[Mapping[str, Any]],
    *,
    kind: str = "",
) -> dict[str, Any]:
    total = len(records)
    # 字幕行没有 status 列：按内容有效性判定完成，见 _record_ready。
    completed = sum(_record_ready(record, kind=kind) for record in records)
    if total and completed == total:
        status = "completed"
        reason = ""
    elif completed:
        status = "partial"
        reason = f"仅完成 {completed}/{total}"
    else:
        status = "missing"
        reason = f"{label}尚未完成"
    return {
        "key": key,
        "label": label,
        "status": status,
        "completed": completed,
        "total": total,
        "reason": reason,
        "details": {"record_ids": [str(record.get("id", "")) for record in records]},
    }


def _reference_checklist_item(
    run: Mapping[str, Any],
    asset_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """按运行素材映射和实际素材记录计算参考图绑定完成度。"""

    mappings = run.get("asset_mapping", [])
    if not isinstance(mappings, list):
        mappings = []
    if not mappings:
        return {
            "key": "references",
            "label": "角色/场景参考图",
            "status": "completed",
            "completed": 0,
            "total": 0,
            "reason": "",
            "details": {},
        }

    assets_by_id = {str(item.get("id")): item for item in asset_records if item.get("id")}
    completed = 0
    failed = 0
    record_details: list[dict[str, Any]] = []
    for index, raw in enumerate(mappings):
        item = raw if isinstance(raw, Mapping) else {}
        asset_id = str(item.get("asset_id") or "").strip()
        status = _status(item.get("status")).casefold()
        actual = assets_by_id.get(asset_id)
        actual_status = _status(actual.get("status") if actual else "").casefold()
        locked_revision = item.get("locked_revision")
        revision_matches = locked_revision is None or (
            actual is not None and str(actual.get("revision")) == str(locked_revision)
        )
        binding_failed = status in {"failed", "error", "blocked"}
        is_complete = bool(
            asset_id
            and actual is not None
            and actual_status in _READY_STATUSES
            and revision_matches
            and not binding_failed
        )
        if is_complete:
            completed += 1
        elif binding_failed or (asset_id and actual is not None and not revision_matches):
            failed += 1
        record_details.append(
            {
                "index": index,
                "workflow_asset_id": item.get("workflow_asset_id"),
                "asset_id": asset_id or None,
                "status": "completed" if is_complete else "failed" if binding_failed else "missing",
                "actual_asset_status": actual_status or None,
                "locked_revision": locked_revision,
                "actual_revision": actual.get("revision") if actual else None,
            }
        )

    if failed:
        display_status = "failed"
        reason = f"{failed}/{len(mappings)} 个素材绑定失败"
    elif completed == len(mappings):
        display_status = "completed"
        reason = ""
    elif completed:
        display_status = "partial"
        reason = f"仅完成 {completed}/{len(mappings)}"
    else:
        display_status = "missing"
        reason = "素材映射尚未绑定到实际素材"
    return {
        "key": "references",
        "label": "角色/场景参考图",
        "status": display_status,
        "completed": completed,
        "total": len(mappings),
        "reason": reason,
        "details": {"records": record_details},
    }


class DynamicDeliveryService:
    """动态运行的只读交付服务。"""

    def __init__(
        self,
        database: Database | Engine,
        file_service: FileService | None = None,
    ) -> None:
        self.database = database
        self.engine = database.engine if isinstance(database, Database) else database
        self.file_service = file_service or (
            FileService(database=database) if isinstance(database, Database) else FileService()
        )

    @staticmethod
    def _require_row(
        row: Any,
        message: str,
        *,
        code: str = "delivery_error",
        http_status: int = 400,
    ) -> dict[str, Any]:
        record = row_dict(row)
        if record is None:
            raise DeliveryServiceError(message, code=code, http_status=http_status)
        return record

    @staticmethod
    def _media_file_record(
        kind: str,
        record: Mapping[str, Any],
        *,
        project_id: str,
        chapter_id: str,
    ) -> dict[str, Any]:
        provenance = record.get("provenance")
        provenance = provenance if isinstance(provenance, Mapping) else {}
        result: dict[str, Any] = {
            "kind": kind,
            "id": record.get("id", ""),
            "project_id": record.get("project_id") or project_id,
            "chapter_id": record.get("chapter_id"),
            "shot_id": record.get("shot_id"),
            "path": record.get("file_path"),
            "file_path": record.get("file_path"),
            "current": record.get("current", False),
            "status": record.get("status", ""),
            "input_signature": record.get("input_signature", ""),
            "provenance": record.get("provenance", {}),
            "workflow_run_id": record.get("workflow_run_id") or provenance.get("workflow_run_id"),
            "workflow_stage_id": record.get("workflow_stage_id") or provenance.get(
                "workflow_stage_id"
            ),
            "source_signature": record.get("input_signature")
            or provenance.get("input_signature")
            or provenance.get("upstream_input_signature", ""),
            "upstream_input_signature": provenance.get("upstream_input_signature", ""),
            "source_attempt": provenance.get("workflow_stage_attempt")
            or provenance.get("source_attempt"),
            "source_revision": provenance.get("workflow_run_revision")
            or provenance.get("run_revision"),
        }
        return _redact(result)

    @staticmethod
    def _build_checklist(
        run: Mapping[str, Any],
        current_media: Mapping[str, Sequence[Mapping[str, Any]]],
        selected_bgms: Sequence[Mapping[str, Any]],
        compositions: Sequence[Mapping[str, Any]],
        asset_records: Sequence[Mapping[str, Any]] = (),
    ) -> list[dict[str, Any]]:
        references = _reference_checklist_item(run, asset_records)
        result = [references]
        for key, label, kind in (
            ("keyframes", "关键帧", "keyframe"),
            ("videos", "分镜视频", "video"),
            ("audio", "配音", "audio"),
            ("subtitle", "字幕", "subtitle"),
        ):
            result.append(
                _checklist_media_item(key, label, current_media.get(kind, ()), kind=kind)
            )

        if selected_bgms:
            result.append(
                {
                    "key": "bgm",
                    "label": "BGM",
                    "status": "selected",
                    "completed": 1,
                    "total": 1,
                    "reason": "",
                    "details": {
                        "record_ids": [str(record.get("id", "")) for record in selected_bgms]
                    },
                }
            )
        else:
            result.append(
                {
                    "key": "bgm",
                    "label": "BGM",
                    "status": "none",
                    "completed": 0,
                    "total": 1,
                    "reason": "未选择 BGM",
                    "details": {},
                }
            )

        composition = compositions[-1] if compositions else None
        composition_status = _status(composition.get("status")) if composition else "pending"
        composition_status = composition_status.casefold()
        composition_display = (
            "completed"
            if composition_status in _READY_STATUSES
            else "failed"
            if composition_status in {"failed", "error"}
            else "planned"
        )
        result.append(
            {
                "key": "composition",
                "label": "成片",
                "status": composition_display,
                "completed": 1 if composition_display == "completed" else 0,
                "total": 1,
                "reason": ""
                if composition_display == "completed"
                else "成片尚未完成，但已具备可计划状态"
                if composition_display == "planned"
                else "成片任务失败",
                "details": {"record_id": composition.get("id")} if composition else {},
            }
        )
        return _redact(result)

    def build_snapshot(self, run_id: str, project_id: str | None = None) -> dict[str, Any]:
        """读取一个运行的完整交付快照，不写数据库或文件系统。"""

        if not isinstance(run_id, str) or not run_id:
            raise DeliveryServiceError("run_id 不能为空")

        with self.engine.connect() as connection:
            run_row = connection.execute(
                sa.select(workflow_run).where(workflow_run.c.id == run_id)
            ).first()
            run_raw = self._require_row(
                run_row,
                f"workflow run 不存在：{run_id}",
                code="workflow_run_not_found",
                http_status=404,
            )
            actual_project_id = str(run_raw.get("project_id") or "")
            actual_chapter_id = str(run_raw.get("chapter_id") or "")
            if project_id is not None and project_id != actual_project_id:
                raise DeliveryServiceError(
                    f"run/project 关联不一致：run={actual_project_id!r}, project={project_id!r}"
                )
            resolved_project_id = project_id or actual_project_id

            project_row = connection.execute(
                sa.select(project).where(project.c.id == resolved_project_id)
            ).first()
            project_raw = self._require_row(
                project_row, f"project 不存在或与 run 不匹配：{resolved_project_id}"
            )
            chapter_row = connection.execute(
                sa.select(chapter).where(chapter.c.id == actual_chapter_id)
            ).first()
            chapter_raw = self._require_row(
                chapter_row, f"chapter 不存在或与 run 不匹配：{actual_chapter_id}"
            )
            if actual_project_id != resolved_project_id:
                raise DeliveryServiceError("workflow_run.project_id 与 project_id 不一致")
            if str(chapter_raw.get("project_id") or "") != resolved_project_id:
                raise DeliveryServiceError("workflow_run、project、chapter 关联不一致")

            stage_rows = connection.execute(
                sa.select(workflow_stage)
                .where(workflow_stage.c.workflow_run_id == run_id)
                .order_by(
                    workflow_stage.c.ordinal,
                    workflow_stage.c.stage_key,
                    workflow_stage.c.attempt,
                    workflow_stage.c.updated_at,
                    workflow_stage.c.id,
                )
            ).all()

            shot_rows = connection.execute(
                sa.select(shot)
                .where(shot.c.chapter_id == actual_chapter_id)
                .order_by(shot.c.shot_order, shot.c.shot_no, shot.c.id)
            ).all()
            for row in shot_rows:
                shot_raw = row_dict(row) or {}
                if str(shot_raw.get("project_id") or "") != resolved_project_id:
                    raise DeliveryServiceError("shot 不属于当前 project")
            raw_asset_mapping = _json_value(run_raw.get("asset_mapping_json"), [])
            mapped_asset_ids = {
                str(item.get("asset_id"))
                for item in raw_asset_mapping
                if isinstance(item, Mapping) and item.get("asset_id")
            }
            asset_rows = connection.execute(
                sa.select(asset).where(
                    asset.c.project_id == resolved_project_id,
                    asset.c.id.in_(mapped_asset_ids) if mapped_asset_ids else sa.false(),
                )
            ).all()
            keyframe_rows = connection.execute(
                sa.select(keyframe_version)
                .select_from(keyframe_version.join(shot, keyframe_version.c.shot_id == shot.c.id))
                .where(shot.c.chapter_id == actual_chapter_id)
                .order_by(keyframe_version.c.shot_id, keyframe_version.c.version_no)
            ).all()
            video_rows = connection.execute(
                sa.select(video_version)
                .select_from(video_version.join(shot, video_version.c.shot_id == shot.c.id))
                .where(shot.c.chapter_id == actual_chapter_id)
                .order_by(video_version.c.shot_id, video_version.c.version_no)
            ).all()
            audio_rows = connection.execute(
                sa.select(shot_audio)
                .select_from(shot_audio.join(shot, shot_audio.c.shot_id == shot.c.id))
                .where(shot.c.chapter_id == actual_chapter_id)
                .order_by(shot_audio.c.shot_id, shot_audio.c.line_order, shot_audio.c.version_no)
            ).all()
            subtitle_rows = connection.execute(
                sa.select(shot_subtitle)
                .select_from(shot_subtitle.join(shot, shot_subtitle.c.shot_id == shot.c.id))
                .where(shot.c.chapter_id == actual_chapter_id)
                .order_by(shot_subtitle.c.shot_id, shot_subtitle.c.start_time, shot_subtitle.c.id)
            ).all()
            composition_rows = connection.execute(
                sa.select(chapter_composition)
                .where(chapter_composition.c.chapter_id == actual_chapter_id)
                .order_by(chapter_composition.c.updated_at, chapter_composition.c.id)
            ).all()
            composition_raw = [row_dict(row) or {} for row in composition_rows]
            for record in composition_raw:
                linked_run_id = str(record.get("workflow_run_id") or "")
                if linked_run_id and linked_run_id != run_id:
                    raise DeliveryServiceError("chapter_composition 不属于当前 workflow run")

            selected_bgm_ids = {
                str(record.get("bgm_id")) for record in composition_raw if record.get("bgm_id")
            }
            config_snapshot = _json_value(run_raw.get("config_snapshot_json"), {})
            configured_bgm_id = (
                config_snapshot.get("bgm_id") if isinstance(config_snapshot, Mapping) else None
            )
            if configured_bgm_id:
                selected_bgm_ids.add(str(configured_bgm_id))
            bgm_query = sa.select(project_bgm).where(
                sa.or_(
                    project_bgm.c.project_id == resolved_project_id,
                    project_bgm.c.id.in_(selected_bgm_ids) if selected_bgm_ids else sa.false(),
                )
            )
            bgm_rows = connection.execute(
                bgm_query.order_by(project_bgm.c.created_at, project_bgm.c.id)
            ).all()

        run = _decode_row(run_raw, _RUN_JSON_FIELDS)
        run["title"] = project_raw.get("name", "")
        run["chapter_title"] = chapter_raw.get("title", "")
        run["project_id"] = actual_project_id
        run["chapter_id"] = actual_chapter_id
        run = _redact(run)
        project_record = _redact(_decode_row(project_raw))
        chapter_record = _redact(_decode_row(chapter_raw))

        decoded_stages = [_decode_row(row, _STAGE_JSON_FIELDS) for row in stage_rows]
        latest_by_key: dict[str, dict[str, Any]] = {}
        for stage in decoded_stages:
            stage_key = _status(stage.get("stage_key"))
            if not stage_key:
                continue
            current = latest_by_key.get(stage_key)
            current_key = (
                int(current.get("attempt") or 0),
                _status(current.get("updated_at")),
                _status(current.get("id")),
            ) if current is not None else (-1, "", "")
            stage_key_value = (
                int(stage.get("attempt") or 0),
                _status(stage.get("updated_at")),
                _status(stage.get("id")),
            )
            if stage_key_value > current_key:
                latest_by_key[stage_key] = stage
        latest_stages = sorted(
            latest_by_key.values(),
            key=lambda item: (
                int(item.get("ordinal") or 0),
                _status(item.get("stage_key")),
            ),
        )
        stage_ids = {str(stage.get("id")) for stage in latest_stages if stage.get("id")}
        queue_version_ids = {
            kind: _queue_version_ids(latest_stages, run_id, kind)
            for kind in ("keyframe", "video", "audio", "subtitle")
        }
        stages = [_redact(stage) for stage in latest_stages]

        raw_keyframes = [
            _decode_row(row, _KEYFRAME_JSON_FIELDS, media=True) for row in keyframe_rows
        ]
        raw_videos = [_decode_row(row, _VIDEO_JSON_FIELDS, media=True) for row in video_rows]
        raw_audios = [_decode_row(row, _AUDIO_JSON_FIELDS, media=True) for row in audio_rows]
        raw_subtitles = [
            _decode_row(row, _SUBTITLE_JSON_FIELDS, media=True, current=True)
            for row in subtitle_rows
        ]
        _keyframe_scope, current_keyframes = _media_scope(
            raw_keyframes,
            run_id=run_id,
            stage_ids=stage_ids,
            queue_version_ids=queue_version_ids["keyframe"],
            kind="keyframe",
        )
        _video_scope, current_videos = _media_scope(
            raw_videos,
            run_id=run_id,
            stage_ids=stage_ids,
            queue_version_ids=queue_version_ids["video"],
            kind="video",
        )
        _audio_scope, current_audios = _media_scope(
            raw_audios,
            run_id=run_id,
            stage_ids=stage_ids,
            queue_version_ids=queue_version_ids["audio"],
            kind="audio",
        )
        _subtitle_scope, current_subtitles = _media_scope(
            raw_subtitles,
            run_id=run_id,
            stage_ids=stage_ids,
            queue_version_ids=queue_version_ids["subtitle"],
            kind="subtitle",
        )
        all_keyframes = [
            _redact(record)
            for record in _keyframe_scope
        ]
        all_videos = [_redact(record) for record in _video_scope]
        all_audios = [_redact(record) for record in _audio_scope]
        all_subtitles = [_redact(record) for record in _subtitle_scope]
        current_keyframes = [_redact(record) for record in current_keyframes]
        current_videos = [_redact(record) for record in current_videos]
        current_audios = [_redact(record) for record in current_audios]
        current_subtitles = [_redact(record) for record in current_subtitles]
        all_bgms = [
            _decode_row(
                row,
                _BGM_JSON_FIELDS,
                media=True,
                current=str((row_dict(row) or {}).get("id")) in selected_bgm_ids,
            )
            for row in bgm_rows
        ]
        compositions = [
            _decode_row(
                row,
                _COMPOSITION_JSON_FIELDS,
                media=True,
                current=index == len(composition_rows) - 1,
            )
            for index, row in enumerate(composition_rows)
        ]

        for records in (all_keyframes, all_videos, all_audios, all_subtitles, compositions):
            for record in records:
                record["project_id"] = resolved_project_id
                record["chapter_id"] = actual_chapter_id

        current_media: dict[str, list[dict[str, Any]]] = {
            "keyframe": current_keyframes,
            "video": current_videos,
            "audio": current_audios,
            "subtitle": current_subtitles,
        }
        selected_bgms = [record for record in all_bgms if record["current"]]

        shots: list[dict[str, Any]] = []
        for row in shot_rows:
            shot_record = _decode_row(row, _SHOT_JSON_FIELDS)
            shot_id = str(shot_record.get("id") or "")
            shot_record["current"] = True
            shot_record["status"] = _status(shot_record.get("status"), "draft")
            shot_record["input_signature"] = _status(
                shot_record.get("own_signature") or shot_record.get("source_signature")
            )
            shot_provenance = shot_record.get("pipeline_metadata", {})
            shot_record["provenance"] = (
                shot_provenance if isinstance(shot_provenance, Mapping) else {}
            )
            shot_record["keyframe"] = [
                record for record in current_media["keyframe"] if record.get("shot_id") == shot_id
            ]
            shot_record["video"] = [
                record for record in current_media["video"] if record.get("shot_id") == shot_id
            ]
            shot_record["audio"] = [
                record for record in current_media["audio"] if record.get("shot_id") == shot_id
            ]
            shot_record["subtitle"] = [
                record for record in current_media["subtitle"] if record.get("shot_id") == shot_id
            ]
            shot_record["keyframes"] = shot_record["keyframe"]
            shot_record["videos"] = shot_record["video"]
            shot_record["audios"] = shot_record["audio"]
            shot_record["subtitles"] = shot_record["subtitle"]
            shot_record["keyframe_versions"] = [
                record for record in all_keyframes if record.get("shot_id") == shot_id
            ]
            shot_record["video_versions"] = [
                record for record in all_videos if record.get("shot_id") == shot_id
            ]
            shot_record["audio_versions"] = [
                record for record in all_audios if record.get("shot_id") == shot_id
            ]
            shots.append(_redact(shot_record))

        media = {
            "keyframes": current_media["keyframe"],
            "keyframe": current_media["keyframe"],
            "videos": current_media["video"],
            "video": current_media["video"],
            "audios": current_media["audio"],
            "audio": current_media["audio"],
            "subtitles": current_media["subtitle"],
            "subtitle": current_media["subtitle"],
            "bgms": selected_bgms,
            "bgm": selected_bgms,
            "compositions": compositions,
            "composition": compositions,
        }
        all_version_records: list[dict[str, Any]] = []
        for kind, records in (
            ("keyframe", all_keyframes),
            ("video", all_videos),
            ("audio", all_audios),
            ("subtitle", all_subtitles),
            ("bgm", all_bgms),
            ("composition", compositions),
        ):
            all_version_records.extend(
                self._media_file_record(
                    kind,
                    record,
                    project_id=resolved_project_id,
                    chapter_id=actual_chapter_id,
                )
                for record in records
            )

        file_records = [
            self._media_file_record(
                kind,
                record,
                project_id=resolved_project_id,
                chapter_id=actual_chapter_id,
            )
            for kind, records in (
                ("keyframe", current_media["keyframe"]),
                ("video", current_media["video"]),
                ("audio", current_media["audio"]),
                ("subtitle", current_media["subtitle"]),
                ("bgm", selected_bgms),
                ("composition", compositions),
            )
            for record in records
        ]

        checklist = self._build_checklist(
            run,
            current_media,
            selected_bgms,
            compositions,
            [_decode_row(row) for row in asset_rows],
        )
        snapshot = {
            "workflow_run_id": run_id,
            "run_id": run_id,
            "project_id": resolved_project_id,
            "chapter_id": actual_chapter_id,
            "run": run,
            "project": project_record,
            "chapter": chapter_record,
            "stages": stages,
            "shots": shots,
            "media": _redact(media),
            "keyframes": current_media["keyframe"],
            "videos": current_media["video"],
            "audios": current_media["audio"],
            "subtitles": current_media["subtitle"],
            "bgm": selected_bgms,
            "project_bgms": all_bgms,
            "composition": compositions,
            "chapter_compositions": compositions,
            "files": file_records,
            "versions": all_version_records,
            "checklist": checklist,
            "input_signature": run.get("input_signature", ""),
            "provenance": {
                "source": "database_snapshot",
                "workflow_run_id": run_id,
                "project_id": resolved_project_id,
                "chapter_id": actual_chapter_id,
                "media_paths_opened": False,
                "ffmpeg_called": False,
            },
        }
        return _redact(snapshot)

    def _preflight_snapshot(
        self,
        snapshot: Mapping[str, Any],
        *,
        strict: bool,
        allow_partial: bool,
    ) -> CompositionPreflightResult:
        project_id = str(snapshot.get("project_id") or "")
        try:
            project_root = self.file_service.project_path(project_id)
            result = run_composition_preflight(
                copy.deepcopy(snapshot),
                strict=strict,
                allow_partial=allow_partial,
                project_root=project_root,
            )
            references = next(
                (
                    item
                    for item in snapshot.get("checklist", [])
                    if isinstance(item, Mapping) and item.get("key") == "references"
                ),
                None,
            )
            reference_status = (
                str(references.get("status") or "").casefold()
                if isinstance(references, Mapping)
                else ""
            )
            if reference_status in {"missing", "partial", "failed"}:
                reference_record = references if isinstance(references, Mapping) else {}
                reference_item = PreflightItem(
                    kind="references",
                    id=f"references:{result.workflow_run_id}",
                    status=(
                        "failed"
                        if reference_status == "failed"
                        else "skipped"
                        if allow_partial
                        else "missing"
                    ),
                    path=None,
                    reason=str(reference_record.get("reason") or "参考图素材映射尚未完成"),
                    required=True,
                )
                items = [reference_item, *result.items]
                missing = list(result.missing)
                failed = list(result.failed)
                skipped = list(result.skipped)
                if reference_item.status == "failed":
                    failed.append(reference_item)
                elif reference_item.status == "skipped":
                    skipped.append(reference_item)
                else:
                    missing.append(reference_item)
                provenance = copy.deepcopy(result.provenance)
                diagnostics = list(provenance.get("diagnostics", []))
                diagnostics.append(
                    {
                        "level": "blocking" if reference_item.status != "skipped" else "warning",
                        "code": "preflight_reference_mapping_incomplete",
                        "message": reference_item.reason,
                        "path": "checklist.references",
                    }
                )
                provenance["diagnostics"] = diagnostics
                target_status = result.status
                if reference_item.status == "failed" or reference_item.status == "missing":
                    target_status = "blocked"
                elif target_status == "ready":
                    target_status = "warning"
                return result.model_copy(
                    deep=True,
                    update={
                        "status": target_status,
                        "items": items,
                        "missing": missing,
                        "failed": failed,
                        "skipped": skipped,
                        "provenance": provenance,
                    },
                )
            return result
        except DeliveryServiceError:
            raise
        except Exception as exc:
            raise DeliveryServiceError(f"preflight 执行失败：{exc}") from exc

    def _validate_delivery_input_snapshot(self, run_id: str) -> None:
        """预检入口也复用交付 Job 的惰性 Prompt stale 校验。"""

        with self.engine.connect() as connection:
            run = row_dict(
                connection.execute(
                    sa.select(workflow_run).where(workflow_run.c.id == run_id)
                ).first()
            )
            stage = row_dict(
                connection.execute(
                    sa.select(workflow_stage).where(
                        workflow_stage.c.workflow_run_id == run_id,
                        workflow_stage.c.stage_key == "6-delivery",
                    )
                    .order_by(workflow_stage.c.attempt.desc(), workflow_stage.c.updated_at.desc())
                    .limit(1)
                ).first()
            )
        if run is None or stage is None:
            return
        if not isinstance(self.database, Database):
            return
        DynamicDeliveryJobService(
            self.database,
            file_service=self.file_service,
        )._validate_delivery_input_snapshot(run, stage)

    @staticmethod
    def _checklist_with_preflight(
        checklist: Sequence[Mapping[str, Any]],
        result: CompositionPreflightResult,
    ) -> list[dict[str, Any]]:
        """把路径、签名和计划态结果投影回导出清单。"""

        grouped: dict[str, list[Any]] = {}
        for item in result.items:
            checklist_key = {
                "keyframe": "keyframes",
                "video": "videos",
            }.get(item.kind, item.kind)
            grouped.setdefault(checklist_key, []).append(item)
        output: list[dict[str, Any]] = []
        for raw in checklist:
            item = dict(raw)
            key = _status(item.get("key")).casefold()
            if key not in grouped:
                output.append(item)
                continue
            values = grouped[key]
            failed = [value for value in values if value.status == "failed"]
            skipped = [value for value in values if value.status == "skipped"]
            missing = [value for value in values if value.status == "missing"]
            planned = [value for value in values if value.status == "planned"]
            completed = sum(value.status in {"ready", "completed"} for value in values)
            if failed:
                item["status"] = "failed"
                item["reason"] = "; ".join(
                    value.reason for value in failed if value.reason
                ) or "媒体预检失败"
            elif missing:
                item["status"] = "missing"
                item["reason"] = "; ".join(
                    value.reason for value in missing if value.reason
                ) or "媒体尚未完成"
            elif skipped:
                item["status"] = "skipped"
                item["reason"] = "; ".join(
                    value.reason for value in skipped if value.reason
                ) or "媒体按部分交付策略跳过"
            elif planned:
                item["status"] = "planned"
                item["reason"] = "成片尚未创建，但已具备可计划状态"
            else:
                item["status"] = "completed"
                item["reason"] = ""
            item["completed"] = completed
            item["total"] = len(values)
            details = item.get("details")
            details = dict(details) if isinstance(details, Mapping) else {}
            item["details"] = {
                **details,
                "preflight_item_ids": [value.id for value in values],
            }
            output.append(item)
        return output

    def preflight(
        self,
        run_id: str,
        strict: bool = True,
        allow_partial: bool = False,
    ) -> CompositionPreflightResult:
        """读取快照并执行纯本地合成前预检。"""

        self._validate_delivery_input_snapshot(run_id)
        snapshot = self.build_snapshot(run_id)
        return self._preflight_snapshot(snapshot, strict=strict, allow_partial=allow_partial)

    @staticmethod
    def _safe_run_id(run_id: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "", str(run_id))
        return safe or "run"

    @staticmethod
    def _blocked_preflight_error(result: CompositionPreflightResult) -> DeliveryServiceError:
        return DeliveryServiceError(
            "preflight blocked: "
            f"missing={len(result.missing)}, failed={len(result.failed)}, "
            f"skipped={len(result.skipped)}",
            code="preflight_blocked",
            http_status=409,
        )

    @staticmethod
    def _export_stage_key(kind: str) -> str:
        return {
            "keyframe": "5a-image-prompts",
            "video": "5b-video-prompts",
            "audio": "6-delivery",
            "subtitle": "6-delivery",
            "composition": "6-delivery",
            "bgm": "6-delivery",
        }.get(kind, "6-delivery")

    @staticmethod
    def _safe_media_arcname(kind: str, source_id: str, path: str, *, run_id: str) -> str:
        suffix = Path(path).suffix.lower() or {
            "keyframe": ".png",
            "video": ".mp4",
            "audio": ".mp3",
            "composition": ".mp4",
        }.get(kind, ".bin")
        safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", source_id).strip("._")
        if not safe_id:
            safe_id = DynamicDeliveryService._safe_run_id(run_id)
        return f"media/{kind}/{safe_id}{suffix}"

    @staticmethod
    def _config_enabled(run: Mapping[str, Any], name: str, default: bool = True) -> bool:
        config = run.get("config_snapshot")
        config = config if isinstance(config, Mapping) else {}
        if name not in config:
            return default
        value = config[name]
        if isinstance(value, str):
            return value.strip().casefold() not in {"", "0", "false", "no", "off"}
        return bool(value)

    @staticmethod
    def _positive_int(value: Any, *, field: str, source_id: str) -> int:
        try:
            result = int(value)
        except (TypeError, ValueError) as exc:
            raise DeliveryServiceError(f"交付媒体 {source_id} 缺少有效 {field}") from exc
        if result < 1:
            raise DeliveryServiceError(f"交付媒体 {source_id} 的 {field} 无效")
        return result

    def _binary_media_entries(
        self,
        snapshot: Mapping[str, Any],
        preflight: CompositionPreflightResult,
        *,
        allow_partial: bool | None = None,
    ) -> list[ExportEntry]:
        """读取并冻结当前 run/stage attempt 的真实媒体字节。"""
        project_id = str(snapshot.get("project_id") or "")
        run_id = str(snapshot.get("workflow_run_id") or snapshot.get("run_id") or "")
        run = snapshot.get("run")
        run = run if isinstance(run, Mapping) else {}
        stages = {
            str(stage.get("stage_key")): stage
            for stage in snapshot.get("stages", [])
            if isinstance(stage, Mapping) and stage.get("stage_key")
        }
        files = [
            dict(item)
            for item in snapshot.get("files", [])
            if isinstance(item, Mapping)
            and item.get("kind") in {"keyframe", "video", "audio", "subtitle", "composition", "bgm"}
            and item.get("current", True) is not False
        ]
        subtitle_records = (
            snapshot.get("subtitles", [])
            if isinstance(snapshot.get("subtitles"), list)
            else []
        )
        for item in subtitle_records:
            if isinstance(item, Mapping) and not any(
                str(existing.get("id") or "") == str(item.get("id") or "")
                and existing.get("path")
                for existing in files
            ):
                files.append({"kind": "subtitle", **dict(item)})
        root = self.file_service.project_path(project_id)
        entries: list[ExportEntry] = []
        subtitle_inline: list[Mapping[str, Any]] = []
        inline_subtitle_ids: set[str] = set()
        partial = preflight.allow_partial if allow_partial is None else allow_partial
        for record in files:
            kind = str(record.get("kind") or "")
            stage = stages.get(self._export_stage_key(kind), {})
            provenance = record.get("provenance")
            provenance = provenance if isinstance(provenance, Mapping) else {}
            actual_run = str(
                record.get("workflow_run_id")
                or provenance.get("workflow_run_id")
                or ""
            )
            if not actual_run:
                dynamic_record = False
            else:
                dynamic_record = True
            if actual_run and actual_run != run_id:
                raise DeliveryServiceError("媒体不属于当前 workflow run，拒绝导出")
            expected_stage_id = str(stage.get("id") or "")
            actual_stage_id = str(
                record.get("workflow_stage_id")
                or provenance.get("workflow_stage_id")
                or ""
            )
            if dynamic_record and (
                not expected_stage_id or not actual_stage_id or expected_stage_id != actual_stage_id
            ):
                raise DeliveryServiceError("媒体不属于当前 workflow stage attempt，拒绝导出")
            expected_signature = str(stage.get("input_signature") or "")
            upstream_signature = str(
                record.get("upstream_input_signature")
                or provenance.get("upstream_input_signature")
                or ""
            )
            if dynamic_record and expected_signature and (
                upstream_signature or str(record.get("input_signature") or "")
            ) != expected_signature:
                raise DeliveryServiceError("媒体 upstream signature 已过期，拒绝导出")
            actual_attempt = record.get("source_attempt")
            if actual_attempt is None:
                actual_attempt = provenance.get("workflow_stage_attempt")
            expected_attempt = stage.get("attempt")
            if dynamic_record and expected_attempt is not None and actual_attempt is not None:
                if str(actual_attempt) != str(expected_attempt):
                    raise DeliveryServiceError("媒体 stage attempt 已过期，拒绝导出")
            if dynamic_record and expected_attempt is None:
                raise DeliveryServiceError("当前 workflow stage 缺少 attempt，拒绝导出")
            source_id = str(record.get("id") or "").strip()
            if not source_id:
                raise DeliveryServiceError("交付媒体缺少 source_id，拒绝导出")
            attempt_value = (
                actual_attempt
                if actual_attempt is not None
                else expected_attempt
                if expected_attempt is not None
                else 1
            )
            entry_attempt = self._positive_int(
                attempt_value,
                field="attempt",
                source_id=source_id,
            )
            actual_revision = record.get("source_revision")
            if actual_revision is None:
                actual_revision = provenance.get("workflow_run_revision")
            if actual_revision is None:
                actual_revision = provenance.get("run_revision")
            expected_revision = run.get("revision")
            if dynamic_record and actual_revision is not None and expected_revision is not None:
                try:
                    if int(actual_revision) > int(expected_revision):
                        raise DeliveryServiceError("媒体 workflow run revision 超前，拒绝导出")
                except (TypeError, ValueError) as exc:
                    raise DeliveryServiceError(
                        f"交付媒体 {source_id} 的 revision 无效，拒绝导出"
                    ) from exc
            revision_value = (
                actual_revision
                if actual_revision is not None
                else expected_revision
                if expected_revision is not None
                else 1
            )
            entry_revision = self._positive_int(
                revision_value,
                field="revision",
                source_id=source_id,
            )
            path_value = str(record.get("path") or record.get("file_path") or "").strip()
            if kind == "subtitle" and not path_value:
                if (
                    str(record.get("status") or "").casefold() in _READY_STATUSES
                    or record.get("text")
                    and record.get("start_time") is not None
                    and record.get("end_time") is not None
                ):
                    if source_id not in inline_subtitle_ids:
                        inline_subtitle_ids.add(source_id)
                        subtitle_inline.append(record)
                continue
            if not path_value:
                if partial and str(record.get("status") or "").casefold() not in _READY_STATUSES:
                    continue
                raise DeliveryServiceError(f"交付媒体缺少文件路径：{record.get('id')}")
            checked = validate_relative_media_path(root, path_value)
            if not isinstance(checked, Path):
                if partial:
                    continue
                raise DeliveryServiceError(f"交付媒体路径校验失败：{record.get('id')}")
            try:
                data = checked.read_bytes()
            except OSError as exc:
                if partial:
                    continue
                raise DeliveryServiceError(f"交付媒体读取失败：{record.get('id')}") from exc
            if not data:
                if partial:
                    continue
                raise DeliveryServiceError(f"交付媒体为空：{record.get('id')}")
            source_signature = str(
                record.get("source_signature")
                or record.get("input_signature")
                or provenance.get("input_signature")
                or upstream_signature
                or expected_signature
            )
            if dynamic_record and not source_signature:
                raise DeliveryServiceError(f"交付媒体 {source_id} 缺少 source_signature")
            entries.append(
                ExportEntry(
                    arcname=self._safe_media_arcname(
                        kind,
                        source_id,
                        path_value,
                        run_id=run_id,
                    ),
                    bytes=data,
                    kind=kind,
                    source_id=source_id,
                    source_signature=source_signature,
                    source_attempt=entry_attempt,
                    source_revision=entry_revision,
                    attempt=entry_attempt,
                    revision=entry_revision,
                )
            )
        if subtitle_inline:
            subtitle_inline.sort(
                key=lambda item: (
                    str(item.get("shot_id") or ""),
                    float(item.get("start_time") or 0),
                    str(item.get("id") or ""),
                )
            )

            def timestamp(value: Any) -> str:
                seconds = max(0.0, float(value or 0))
                hours = int(seconds // 3600)
                minutes = int((seconds % 3600) // 60)
                whole = int(seconds % 60)
                millis = int(round((seconds - int(seconds)) * 1000))
                if millis >= 1000:
                    whole += 1
                    millis = 0
                return f"{hours:02d}:{minutes:02d}:{whole:02d},{millis:03d}"

            srt_lines: list[str] = []
            for index, item in enumerate(subtitle_inline, start=1):
                srt_lines.extend(
                    [
                        str(index),
                        (
                            f"{timestamp(item.get('start_time'))} --> "
                            f"{timestamp(item.get('end_time'))}"
                        ),
                        str(item.get("text") or ""),
                        "",
                    ]
                )
            stage = stages.get("6-delivery", {})
            entries.append(
                ExportEntry(
                    arcname=f"media/subtitle/{self._safe_run_id(run_id)}.srt",
                    text="\n".join(srt_lines),
                    kind="subtitle",
                    source_id=f"subtitle:{run_id}",
                    source_signature=str(stage.get("input_signature") or ""),
                    source_attempt=self._positive_int(
                        stage.get("attempt") or 1,
                        field="attempt",
                        source_id=f"subtitle:{run_id}",
                    ),
                    source_revision=self._positive_int(
                        run.get("revision") or 1,
                        field="revision",
                        source_id=f"subtitle:{run_id}",
                    ),
                    attempt=self._positive_int(
                        stage.get("attempt") or 1,
                        field="attempt",
                        source_id=f"subtitle:{run_id}",
                    ),
                    revision=self._positive_int(
                        run.get("revision") or 1,
                        field="revision",
                        source_id=f"subtitle:{run_id}",
                    ),
                )
            )
        entries.sort(key=lambda item: (item.kind, item.arcname, item.source_id))

        if not partial:
            composition_enabled = self._config_enabled(run, "generate_composition", True)
            for item in preflight.items:
                if not item.required:
                    continue
                if item.kind == "composition" and not composition_enabled:
                    continue
                if item.status == "planned":
                    raise DeliveryServiceError("严格交付要求 Composition 文件已完成")
                if item.status not in {"ready", "completed"}:
                    raise DeliveryServiceError(
                        f"严格交付媒体未完成：{item.kind}/{item.id}（{item.status}）"
                    )
                if item.kind == "subtitle" and item.path is None:
                    if any(entry.kind == "subtitle" for entry in entries):
                        continue
                if not any(
                    entry.kind == item.kind
                    and (
                        entry.source_id == item.id
                        or (item.path is not None and entry.arcname.endswith(Path(item.path).name))
                    )
                    for entry in entries
                ):
                    raise DeliveryServiceError(
                        f"严格交付缺少媒体字节：{item.kind}/{item.id}"
                    )
        return entries

    def export(
        self,
        run_id: str,
        strict: bool = True,
        allow_partial: bool = False,
        exported_at: str = "",
    ) -> tuple[bytes, str, dict[str, Any]]:
        """导出确定性 Markdown/JSON ZIP；不会创建合成或交付资源。"""

        self._validate_delivery_input_snapshot(run_id)
        snapshot = self.build_snapshot(run_id)
        result = self._preflight_snapshot(snapshot, strict=strict, allow_partial=allow_partial)
        if result.status == "blocked" or (result.status == "warning" and not allow_partial):
            raise self._blocked_preflight_error(result)

        export_mode = "partial" if result.status == "warning" or allow_partial else "strict"
        effective_exported_at = (
            exported_at.strip() if isinstance(exported_at, str) else str(exported_at)
        ) or utc_now()
        checklist = self._checklist_with_preflight(snapshot["checklist"], result)
        input_data = DeliveryExportInput(
            run=snapshot["run"],
            stages=snapshot["stages"],
            checklist=checklist,
            files=snapshot["files"],
            versions=snapshot["versions"],
            media_entries=self._binary_media_entries(
                snapshot,
                result,
                allow_partial=allow_partial or result.allow_partial,
            ),
            export_mode=export_mode,
            exported_at=effective_exported_at,
            preflight=result.model_dump(mode="json"),
            input_signature=result.input_signature,
            provenance={
                "snapshot": copy.deepcopy(snapshot.get("provenance", {})),
                "workflow_run_id": run_id,
                "preflight_status": result.status,
            },
        )

        try:
            manifest = build_delivery_manifest(input_data)
            entries = build_delivery_files(input_data, manifest)
            zip_bytes = build_zip_bytes(entries)
        except Exception as exc:
            raise DeliveryServiceError(f"delivery export failed：{exc}") from exc

        filename = f"dynamic-delivery-{self._safe_run_id(run_id)}.zip"
        return zip_bytes, filename, manifest.model_dump(mode="json")

    def get_checklist(self, run_id: str) -> dict[str, Any]:
        """返回交付清单输入和只读预检结果。"""

        self._validate_delivery_input_snapshot(run_id)
        snapshot = self.build_snapshot(run_id)
        result = self._preflight_snapshot(snapshot, strict=True, allow_partial=False)
        return {
            "checklist": self._checklist_with_preflight(snapshot["checklist"], result),
            "preflight": result.model_dump(mode="json"),
        }


class DynamicDeliveryJobService:
    """执行 6-delivery 的可恢复子任务编排。"""

    _JOB_TYPE = "dynamic_stage_generate"
    _SUBTITLE_JOB_TYPE = "generate_dynamic_subtitle"
    _BGM_JOB_TYPE = "prepare_dynamic_bgm"
    _DELIVERY_VERSION = "dynamic-delivery-runtime-v1"

    def __init__(
        self,
        database: Database,
        *,
        repository: JobRepository | None = None,
        job_service: Any | None = None,
        audio_service: Any | None = None,
        composition_service: Any | None = None,
        file_service: FileService | None = None,
        enqueue: Any | None = None,
    ) -> None:
        self.database = database
        self.repository = repository or JobRepository(database)
        self.job_service = job_service
        self.audio_service = audio_service
        self.composition_service = composition_service
        self.file_service = file_service or FileService(database=database)
        self.enqueue = enqueue

    @staticmethod
    def _text(value: Any) -> str:
        return value.strip() if isinstance(value, str) else str(value).strip() if value else ""

    @staticmethod
    def _config(run: Mapping[str, Any]) -> dict[str, Any]:
        value = run.get("config_snapshot")
        if not isinstance(value, Mapping):
            value = json_loads(run.get("config_snapshot_json"), {})
        return dict(value) if isinstance(value, Mapping) else {}

    @staticmethod
    def _bool(config: Mapping[str, Any], names: Sequence[str], default: bool) -> bool:
        for name in names:
            if name not in config:
                continue
            value = config[name]
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().casefold() not in {"", "0", "false", "no", "off"}
            return bool(value)
        return default

    def _load_context(
        self,
        job_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        with self.database.engine.connect() as connection:
            current_job = row_dict(
                connection.execute(sa.select(job).where(job.c.id == job_id)).first()
            )
            if current_job is None:
                raise DeliveryExecutionError(f"6-delivery Job 不存在：{job_id}")
            run_id = self._text(current_job.get("workflow_run_id"))
            stage_id = self._text(current_job.get("workflow_stage_id"))
            if not run_id or not stage_id:
                raise DeliveryExecutionError(
                    "6-delivery Job 缺少 workflow_run_id 或 workflow_stage_id"
                )
            run = row_dict(
                connection.execute(
                    sa.select(workflow_run).where(workflow_run.c.id == run_id)
                ).first()
            )
            stage = row_dict(
                connection.execute(
                    sa.select(workflow_stage).where(
                        workflow_stage.c.id == stage_id,
                        workflow_stage.c.workflow_run_id == run_id,
                    )
                ).first()
            )
        if run is None:
            raise DeliveryExecutionError(f"6-delivery 关联的 workflow run 不存在：{run_id}")
        if stage is None or stage.get("stage_key") != "6-delivery":
            raise DeliveryExecutionError("6-delivery Job 关联的阶段无效")
        current_job["input_data"] = json_loads(current_job.get("input_json"), {})
        current_job["depends_on"] = json_loads(current_job.get("depends_on_json"), [])
        if not isinstance(current_job["depends_on"], list):
            current_job["depends_on"] = []
        return current_job, run, stage

    def _latest_stage(self, run_id: str, stage_key: str) -> dict[str, Any] | None:
        with self.database.engine.connect() as connection:
            return row_dict(
                connection.execute(
                    sa.select(workflow_stage)
                    .where(
                        workflow_stage.c.workflow_run_id == run_id,
                        workflow_stage.c.stage_key == stage_key,
                    )
                    .order_by(
                        workflow_stage.c.attempt.desc(),
                        workflow_stage.c.updated_at.desc(),
                        workflow_stage.c.id.desc(),
                    )
                    .limit(1)
                ).first()
            )

    def _assert_prompt_stages_confirmed(self, run_id: str) -> None:
        missing: list[str] = []
        for stage_key in ("5a-image-prompts", "5b-video-prompts"):
            stage = self._latest_stage(run_id, stage_key)
            if stage is None or stage.get("status") != "confirmed":
                missing.append(stage_key)
        if missing:
            raise DeliveryExecutionError(
                f"6-delivery 前置 Prompt 阶段尚未确认：{', '.join(missing)}"
            )

    def _delivery_dependency_ids(
        self,
        run_id: str,
        stage: Mapping[str, Any],
        current_job: Mapping[str, Any],
    ) -> list[str]:
        result = [
            str(value).strip()
            for value in current_job.get("depends_on", [])
            if str(value).strip()
        ]
        with self.database.engine.connect() as connection:
            prompt_stage_ids = [
                str(row.id)
                for stage_key in ("5a-image-prompts", "5b-video-prompts")
                for row in [
                    connection.execute(
                        sa.select(workflow_stage.c.id)
                        .where(
                            workflow_stage.c.workflow_run_id == run_id,
                            workflow_stage.c.stage_key == stage_key,
                        )
                        .order_by(
                            workflow_stage.c.attempt.desc(),
                            workflow_stage.c.updated_at.desc(),
                            workflow_stage.c.id.desc(),
                        )
                        .limit(1)
                    ).first()
                ]
                if row is not None
            ]
            stage_conditions = [
                job.c.workflow_stage_id.in_(prompt_stage_ids)
                if prompt_stage_ids
                else sa.false(),
            ]
            rows = connection.execute(
                sa.select(job.c.id)
                .where(
                    job.c.workflow_run_id == run_id,
                    job.c.job_type.in_(tuple(_DELIVERY_DEPENDENCY_JOB_TYPES)),
                    sa.or_(*stage_conditions),
                    job.c.id != current_job.get("id"),
                )
                .order_by(job.c.created_at, job.c.id)
            ).all()
            for stage_key, job_type in (
                ("5a-image-prompts", "generate_keyframe"),
                ("5b-video-prompts", "generate_video"),
            ):
                prompt_stage = connection.execute(
                    sa.select(workflow_stage).where(
                        workflow_stage.c.workflow_run_id == run_id,
                        workflow_stage.c.stage_key == stage_key,
                    )
                    .order_by(
                        workflow_stage.c.attempt.desc(),
                        workflow_stage.c.updated_at.desc(),
                        workflow_stage.c.id.desc(),
                    )
                    .limit(1)
                ).first()
                if prompt_stage is None:
                    continue
                stage_data = dict(prompt_stage._mapping)
                draft = json_loads(stage_data.get("draft_output_json"), {})
                normalized = json_loads(stage_data.get("normalized_output_json"), {})
                selected = draft if isinstance(draft, Mapping) and draft else normalized
                bundles = selected.get("bundles") if isinstance(selected, Mapping) else None
                expected_count = len(bundles) if isinstance(bundles, Mapping) else 0
                if not expected_count:
                    continue
                actual_count = connection.scalar(
                    sa.select(sa.func.count())
                    .select_from(job)
                    .where(
                        job.c.workflow_run_id == run_id,
                        job.c.workflow_stage_id == prompt_stage.id,
                        job.c.job_type == job_type,
                    )
                )
                if int(actual_count or 0) < expected_count:
                    raise DeliveryDependencyError(
                        f"6-delivery 前置媒体尚未完整入队：{stage_key} "
                        f"{int(actual_count or 0)}/{expected_count}"
                    )
        for row in rows:
            value = str(row.id)
            if value not in result:
                result.append(value)
        return result

    def _job_dependency_state(self, record: Mapping[str, Any]) -> tuple[str, str | None]:
        dependencies = record.get("depends_on")
        if not isinstance(dependencies, list):
            dependencies = []
        pending = False
        for dependency_id in dependencies:
            dependency = self.repository.get(str(dependency_id))
            if dependency is None:
                return "failed", f"任务依赖不存在：{dependency_id}"
            status = str(dependency.get("status") or "")
            if status in {"failed", "interrupted", "cancelled", "stale"}:
                return "failed", f"任务依赖未成功完成：{dependency_id}（{status}）"
            if status != "completed":
                pending = True
        return ("pending", None) if pending else ("ready", None)

    async def _wait_for_delivery_dependencies(
        self,
        run_id: str,
        stage: Mapping[str, Any],
        current_job: Mapping[str, Any],
    ) -> None:
        dependencies = self._delivery_dependency_ids(run_id, stage, current_job)
        if not dependencies:
            return
        try:
            # 120 秒依赖等待只是 runner 层 depends_on 门控失效时的兜底防护，
            # 主机制仍是 Job 调度器按 depends_on 依赖顺序入队执行；这里等待
            # 到超时即抛错，避免交付任务在依赖缺失时无限挂起。
            async with asyncio.timeout(120):
                while True:
                    pending = False
                    for dependency_id in dependencies:
                        dependency = self.repository.get(dependency_id)
                        if dependency is None:
                            raise DeliveryDependencyError(
                                f"6-delivery 媒体前置 Job 不存在：{dependency_id}"
                            )
                        status = str(dependency.get("status") or "")
                        if status == "completed":
                            continue
                        if status in {"failed", "cancelled", "interrupted"}:
                            raise DeliveryDependencyError(
                                "6-delivery 媒体前置 Job 失败，交付进入 partial_failed："
                                f"{dependency_id}（{status}）"
                            )
                        pending = True
                    if not pending:
                        return
                    await asyncio.sleep(settings.provider_poll_interval_sec)
        except TimeoutError as exc:
            raise DeliveryDependencyError(
                "6-delivery 媒体前置 Job 在屏障等待时间内未完成，交付进入 partial_failed"
            ) from exc

    @staticmethod
    def _stage_has_prompt_snapshot(stage: Mapping[str, Any]) -> bool:
        for column in (
            "normalized_output_json",
            "draft_output_json",
            "normalized_output",
            "draft_output",
        ):
            raw = stage.get(column)
            value = raw if isinstance(raw, Mapping) else json_loads(raw, {})
            if isinstance(value, Mapping) and isinstance(value.get("bundles"), Mapping):
                return True
        raw_refs = stage.get("output_refs", stage.get("output_refs_json"))
        output_refs = raw_refs if isinstance(raw_refs, list) else json_loads(raw_refs, [])
        return any(
            isinstance(item, Mapping)
            and item.get("capture_mode") in {"compiled_prompt_stage", "prompt_input_snapshot"}
            for item in output_refs
        )

    def _mark_stale_delivery_input(
        self,
        connection: Any,
        *,
        run_id: str,
        delivery_stage: Mapping[str, Any],
        prompt_stages: Sequence[Mapping[str, Any]],
        message: str,
        now: str,
    ) -> None:
        prompt_ids = [str(item.get("id")) for item in prompt_stages if item.get("id")]
        if prompt_ids:
            connection.execute(
                sa.update(job)
                .where(
                    job.c.workflow_stage_id.in_(prompt_ids),
                    job.c.status.in_(NON_TERMINAL_STATUSES),
                )
                .values(
                    status="interrupted",
                    error=message,
                    message="任务因 Prompt 输入快照变化而中断",
                    finished_at=now,
                    updated_at=now,
                )
            )
            connection.execute(
                sa.update(workflow_stage)
                .where(
                    workflow_stage.c.id.in_(prompt_ids),
                    workflow_stage.c.workflow_run_id == run_id,
                    workflow_stage.c.status.not_in(("cancelled", "stale")),
                )
                .values(status="stale", error=message, finished_at=now, updated_at=now)
            )

        delivery_id = str(delivery_stage.get("id") or "")
        if delivery_id:
            connection.execute(
                sa.update(job)
                .where(
                    job.c.workflow_stage_id == delivery_id,
                    job.c.status.in_(NON_TERMINAL_STATUSES),
                )
                .values(
                    status="interrupted",
                    error=message,
                    message="任务因交付输入快照变化而中断",
                    finished_at=now,
                    updated_at=now,
                )
            )
            connection.execute(
                sa.update(workflow_stage)
                .where(
                    workflow_stage.c.id == delivery_id,
                    workflow_stage.c.workflow_run_id == run_id,
                    workflow_stage.c.status != "cancelled",
                )
                .values(status="stale", error=message, finished_at=now, updated_at=now)
            )

    def _validate_delivery_input_snapshot(
        self,
        run: Mapping[str, Any],
        stage: Mapping[str, Any],
    ) -> None:
        """在交付开始前重新核对 Prompt/参考图快照，不触碰媒体生成。"""

        from app.services.dynamic_workflow_service import DynamicWorkflowService

        service = DynamicWorkflowService(self.database, storage=self.file_service)
        run_id = str(run.get("id") or "")
        changed_prompt_stages: list[Mapping[str, Any]] = []
        stale_detected = False
        with self.database.engine.begin() as connection:
            current_run = row_dict(
                connection.execute(
                    sa.select(workflow_run).where(workflow_run.c.id == run_id)
                ).first()
            )
            current_delivery = row_dict(
                connection.execute(
                    sa.select(workflow_stage).where(
                        workflow_stage.c.id == stage.get("id"),
                        workflow_stage.c.workflow_run_id == run_id,
                    )
                ).first()
            )
            if current_run is None or current_delivery is None:
                raise DeliveryExecutionError("交付输入快照关联记录不存在")

            prompt_stages: list[dict[str, Any]] = []
            expected_prompt_signatures: dict[str, str] = {}
            stage4 = service._stage_from_connection(connection, run_id, "4-storyboard")
            for stage_key in ("5a-image-prompts", "5b-video-prompts"):
                prompt_stage = service._stage_from_connection(connection, run_id, stage_key)
                if prompt_stage is None or not self._stage_has_prompt_snapshot(prompt_stage):
                    continue
                prompt_stages.append(prompt_stage)
                if stage4 is None or stage4.get("status") != "confirmed":
                    continue
                expected_signature, _ = service._prompt_stage_input_signature(
                    connection,
                    stage_key,
                    current_run,
                    stage4,
                )
                expected_prompt_signatures[stage_key] = expected_signature
                if expected_signature != str(prompt_stage.get("input_signature") or ""):
                    changed_prompt_stages.append(prompt_stage)

            delivery_signature = service._delivery_stage_input_signature(
                current_run,
                {
                    item["stage_key"]: item
                    for item in prompt_stages
                    if item.get("stage_key")
                },
            )
            stored_delivery_signature = str(current_delivery.get("input_signature") or "")
            if changed_prompt_stages or (
                expected_prompt_signatures
                and delivery_signature != stored_delivery_signature
            ):
                self._mark_stale_delivery_input(
                    connection,
                    run_id=run_id,
                    delivery_stage=current_delivery,
                    prompt_stages=changed_prompt_stages or prompt_stages,
                    message="交付输入快照已过期：Prompt/参考图或资产 revision 发生变化",
                    now=utc_now(),
                )
                stale_detected = True
        if stale_detected:
            raise DeliveryExecutionError(
                "preflight blocked: 交付输入快照已过期，旧 delivery 已阻断",
                code="input_stale",
                http_status=409,
            )

    def _start_delivery(
        self,
        run_id: str,
        stage_id: str,
        input_signature: str,
    ) -> None:
        now = utc_now()
        with self.database.engine.begin() as connection:
            stage_result = connection.execute(
                sa.update(workflow_stage)
                .where(
                    workflow_stage.c.id == stage_id,
                    workflow_stage.c.workflow_run_id == run_id,
                    workflow_stage.c.status == "queued",
                    workflow_stage.c.input_signature == input_signature,
                )
                .values(
                    status="running",
                    error="",
                    started_at=sa.func.coalesce(workflow_stage.c.started_at, now),
                    finished_at=None,
                    updated_at=now,
                )
            )
            if stage_result.rowcount != 1:
                raise DeliveryExecutionError(
                    "6-delivery stage 在媒体屏障等待期间已过期或状态发生变化"
                )
            run_result = connection.execute(
                sa.update(workflow_run)
                .where(
                    workflow_run.c.id == run_id,
                    workflow_run.c.status.in_(("queued", "running")),
                )
                .values(status="running", error="", updated_at=now)
            )
            if run_result.rowcount != 1:
                raise DeliveryExecutionError(
                    "6-delivery 关联 workflow run 在媒体屏障等待期间已过期"
                )

    @staticmethod
    def _delivery_signature(run: Mapping[str, Any], stage: Mapping[str, Any]) -> str:
        return (
            str(stage.get("input_signature") or "").strip()
            or str(run.get("input_signature") or "").strip()
            or "dynamic-delivery-input-unknown"
        )

    @staticmethod
    def _shot_dialogues(snapshot: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        shots = snapshot.get("shots")
        if not isinstance(shots, list):
            return result
        for shot_record in shots:
            if not isinstance(shot_record, Mapping):
                continue
            shot_id = str(shot_record.get("id") or "").strip()
            external_key = str(
                shot_record.get("external_key") or shot_record.get("id") or ""
            ).strip()
            if not shot_id or not external_key:
                continue
            dialogues = shot_record.get("dialogue_cues") or shot_record.get("dialogue") or []
            if isinstance(dialogues, Mapping):
                dialogues = [dialogues]
            if not isinstance(dialogues, list):
                dialogues = []
            result[external_key] = {
                "shot_id": shot_id,
                "external_key": external_key,
                "dialogues": copy.deepcopy(dialogues),
                "duration_sec": shot_record.get("duration_sec") or 6,
            }
        return result

    def _audio_provider(self, project_id: str, config: Mapping[str, Any]) -> tuple[str, str]:
        sources: list[Mapping[str, Any]] = [config]
        for key in ("audio", "tts", "audio_provider", "tts_provider"):
            value = config.get(key)
            if isinstance(value, Mapping):
                sources.append(value)
        provider_id = ""
        provider_model = ""
        for source in sources:
            provider_id = provider_id or self._text(
                source.get("tts_provider_id")
                or source.get("audio_provider_id")
                or source.get("provider_id")
                or source.get("default_tts_provider_id")
            )
            provider_model = provider_model or self._text(
                source.get("tts_provider_model")
                or source.get("audio_provider_model")
                or source.get("provider_model")
                or source.get("model")
            )
        with self.database.engine.connect() as connection:
            audio_record = row_dict(
                connection.execute(
                    sa.select(audio_config).where(audio_config.c.project_id == project_id)
                ).first()
            )
            if not provider_id and audio_record:
                provider_id = self._text(audio_record.get("tts_provider_id"))
            provider_record = row_dict(
                connection.execute(
                    sa.select(api_provider)
                    .where(
                        api_provider.c.kind.in_(("tts", "text")),
                        api_provider.c.enabled == 1,
                        (
                            api_provider.c.id == provider_id
                            if provider_id
                            else api_provider.c.is_default == 1
                        ),
                    )
                    .order_by(api_provider.c.is_default.desc(), api_provider.c.id)
                    .limit(1)
                ).first()
            )
        if provider_record is not None:
            provider_id = provider_id or self._text(provider_record.get("id"))
            provider_model = provider_model or self._text(provider_record.get("model"))
        return provider_id, provider_model

    def _ensure_job(
        self,
        payload: Mapping[str, Any],
        *,
        run: Mapping[str, Any],
        stage: Mapping[str, Any],
        job_type: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        provider_kind: str | None = None,
    ) -> dict[str, Any]:
        idempotency_key = self._text(payload.get("idempotency_key"))
        existing = self.repository.find_by_idempotency_key(idempotency_key)
        if existing is None:
            data = {
                "project_id": run["project_id"],
                "chapter_id": run["chapter_id"],
                "workflow_run_id": run["id"],
                "workflow_stage_id": stage["id"],
                "job_type": job_type or payload.get("job_type"),
                "target_type": target_type or payload.get("target_type") or "",
                "target_id": target_id or payload.get("target_id"),
                "provider_kind": provider_kind or payload.get("provider_kind") or "text",
                "provider_id": payload.get("provider_id"),
                "input_data": copy.deepcopy(payload.get("input_data") or {}),
                "input_signature": payload.get("input_signature"),
                "idempotency_key": idempotency_key,
                "depends_on": list(payload.get("depends_on") or []),
                "progress_total": int(payload.get("progress_total") or 1),
            }
            existing = self.repository.create_job(data)
        elif existing.get("status") in {"failed", "interrupted"}:
            existing = self.repository.retry(str(existing["id"]))
        current = self.repository.get(str(existing["id"]))
        if current is None:
            raise DeliveryExecutionError("交付子任务创建后无法读取")
        return current

    def _enqueue_job(self, job_id: str) -> bool:
        if self.enqueue is None:
            return False
        accepted = self.enqueue(job_id)
        return accepted is not False

    def _registered_runner_handler(self, job_type: str) -> Any | None:
        """读取共享 Runner 已注册的 handler；测试替身没有该注册表时返回 None。"""
        runner = getattr(self.enqueue, "__self__", None)
        handlers = getattr(runner, "_handlers", None)
        if not isinstance(handlers, Mapping):
            return None
        handler = handlers.get(job_type)
        return handler if callable(handler) else None

    async def _execute_registered_child(
        self,
        record: Mapping[str, Any],
        *,
        job_type: str,
    ) -> dict[str, Any] | None:
        """在共享 Runner 的 handler 合同内执行一个已落库子 Job。

        Delivery 顶层任务本身可能占用 Runner 的唯一 text worker，所以这里不能再
        enqueue 后等待同一个 executor；先用 repository claim，再调用同一注册 handler，
        最终状态仍由 handler/Repository 按 Job ID 写回。
        """
        handler = self._registered_runner_handler(job_type)
        if handler is None:
            return None
        job_id = self._text(record.get("id"))
        if not job_id:
            raise DeliveryExecutionError(f"{job_type} Job 缺少 ID")
        current = self.repository.get(job_id)
        if current is None:
            raise DeliveryExecutionError(f"{job_type} Job 不存在：{job_id}")
        if current.get("status") == "completed":
            result = current.get("result_data")
            return dict(result) if isinstance(result, Mapping) else {"status": "completed"}
        if current.get("status") not in NON_TERMINAL_STATUSES:
            return {
                "status": current.get("status"),
                "job_id": job_id,
                "error": sanitize_error_message(current.get("error")),
            }
        claimed = self.repository.claim(job_id, expected_status=str(current.get("status")))
        if claimed is None:
            latest = self.repository.get(job_id)
            if latest is not None and latest.get("status") == "completed":
                result = latest.get("result_data")
                return dict(result) if isinstance(result, Mapping) else {"status": "completed"}
            return {
                "status": "queued",
                "job_id": job_id,
                "error": "子任务正在由共享 Runner 执行",
            }
        try:
            result = handler(claimed)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:  # noqa: BLE001 — handler 异常必须落为子 Job 失败而非卡 running
            error = sanitize_error_message(exc) or str(exc)
            self.repository.fail(job_id, error)
            raise DeliveryExecutionError(
                f"{job_type} 子任务执行失败：{error}"
            ) from exc
        result_mapping = dict(result) if isinstance(result, Mapping) else {}
        latest = self.repository.get(job_id)
        if latest is not None and latest.get("status") in NON_TERMINAL_STATUSES:
            result_status = self._text(result_mapping.get("status"))
            if result_status in {"failed", "partial_failed", "blocked"}:
                self.repository.fail(
                    job_id,
                    self._text(result_mapping.get("error")) or result_status,
                )
            elif result_status == "completed":
                self.repository.complete(job_id, result_mapping)
            latest = self.repository.get(job_id)
        if latest is not None and latest.get("status") == "completed":
            stored = latest.get("result_data")
            return dict(stored) if isinstance(stored, Mapping) else result_mapping
        if latest is not None and latest.get("status") in {"failed", "interrupted", "cancelled"}:
            return {
                **result_mapping,
                "status": latest.get("status"),
                "job_id": job_id,
                "error": self._text(result_mapping.get("error"))
                or sanitize_error_message(latest.get("error")),
            }
        return {**result_mapping, "status": "queued", "job_id": job_id}

    async def _execute_audio(self, record: Mapping[str, Any]) -> dict[str, Any]:
        registered = await self._execute_registered_child(
            record,
            job_type="generate_dialogue_audio",
        )
        if registered is not None:
            return registered
        if self.audio_service is None:
            if self._enqueue_job(str(record["id"])):
                return {"status": "queued", "job_id": record["id"]}
            raise DeliveryExecutionError("动态音频 Job 已创建但没有可用执行器")
        result = self.audio_service.handle_job(record)
        if inspect.isawaitable(result):
            result = await result
        return dict(result) if isinstance(result, Mapping) else {"status": "failed"}

    @staticmethod
    def _delivery_generation(current_job: Mapping[str, Any]) -> str:
        input_data = current_job.get("input_data")
        if not isinstance(input_data, Mapping):
            input_data = {}
        return str(input_data.get("delivery_generation") or "").strip()

    @staticmethod
    def _with_delivery_generation(
        payload: Mapping[str, Any],
        generation: str,
    ) -> dict[str, Any]:
        result = copy.deepcopy(dict(payload))
        if not generation:
            return result
        base_key = str(result.get("idempotency_key") or "")
        result["idempotency_key"] = f"{base_key}:resume:{generation}"
        base_signature = str(result.get("input_signature") or "")
        resumed_signature = calculate_signature(
            "dynamic-audio-resume-v1",
            base_signature,
            generation,
        )
        result["input_signature"] = resumed_signature
        input_data = result.get("input_data")
        input_data = dict(input_data) if isinstance(input_data, Mapping) else {}
        input_data["delivery_generation"] = generation
        input_data["input_signature"] = resumed_signature
        provenance = input_data.get("provenance")
        if isinstance(provenance, Mapping):
            provenance = dict(provenance)
            provenance["input_signature"] = resumed_signature
            provenance["delivery_generation"] = generation
            input_data["provenance"] = provenance
        result["input_data"] = input_data
        return result

    async def _audio_subtask(
        self,
        snapshot: Mapping[str, Any],
        run: Mapping[str, Any],
        stage: Mapping[str, Any],
        config: Mapping[str, Any],
        signature: str,
        delivery_generation: str = "",
    ) -> dict[str, Any]:
        if not self._bool(config, ("generate_audio", "include_audio"), True):
            return {"kind": "audio", "status": "skipped", "reason": "运行配置未启用配音"}
        shot_dialogues = self._shot_dialogues(snapshot)
        has_dialogue = any(item.get("dialogues") for item in shot_dialogues.values())
        if not has_dialogue:
            return {"kind": "audio", "status": "completed", "jobs": [], "reason": "没有对白"}
        provider_id, provider_model = self._audio_provider(str(run["project_id"]), config)
        if not provider_id:
            raise DeliveryExecutionError("运行配置未提供可用的 TTS Provider")
        try:
            plan = build_audio_delivery_plan(
                shot_dialogues,
                workflow_run_id=str(run["id"]),
                workflow_stage_id=str(stage["id"]),
                input_signature=signature,
                provider_id=provider_id,
                provider_model=provider_model,
                voice_by_speaker=config.get("voice_by_speaker")
                if isinstance(config.get("voice_by_speaker"), Mapping)
                else None,
                params=config.get("audio_params")
                if isinstance(config.get("audio_params"), Mapping)
                else None,
            )
        except (AudioDeliveryError, TypeError, ValueError) as exc:
            raise DeliveryExecutionError(f"动态音频计划失败：{exc}") from exc
        if plan.status == "blocked":
            return {
                "kind": "audio",
                "status": "failed",
                "jobs": [],
                "diagnostics": [item.model_dump(mode="json") for item in plan.diagnostics],
            }
        summaries: list[dict[str, Any]] = []
        failed = False
        for audio_plan in plan.jobs:
            audio_payload = self._with_delivery_generation(
                audio_plan.to_job_payload(),
                delivery_generation,
            )
            child = self._ensure_job(
                audio_payload,
                run=run,
                stage=stage,
                job_type="generate_dialogue_audio",
                target_type="shot_audio",
                target_id=audio_plan.shot_id,
                provider_kind="tts",
            )
            result = await self._execute_audio(child)
            status = self._text(result.get("status")) or self._text(child.get("status"))
            failed = failed or status in {"failed", "interrupted", "cancelled"}
            summaries.append(
                {
                    "job_id": child["id"],
                    "dialogue_line_id": audio_plan.dialogue_line_id,
                    "shot_id": audio_plan.shot_id,
                    "status": status,
                    "result": result,
                }
            )
        return {
            "kind": "audio",
            "status": "failed" if failed else "completed",
            "jobs": summaries,
            "input_signature": plan.input_signature,
            "provenance": plan.provenance,
        }

    def _subtitle_shots(
        self,
        snapshot: Mapping[str, Any],
        audio_result: Mapping[str, Any],
    ) -> dict[str, dict[str, Any]]:
        shots = self._shot_dialogues(snapshot)
        audio_lines: dict[str, list[dict[str, Any]]] = {}
        for item in audio_result.get("jobs", []):
            if not isinstance(item, Mapping):
                continue
            result = item.get("result")
            if not isinstance(result, Mapping):
                continue
            line_id = self._text(item.get("dialogue_line_id"))
            if not line_id:
                continue
            audio_lines.setdefault(self._text(item.get("shot_id")), []).append(
                {
                    "dialogue_line_id": line_id,
                    "audio_id": result.get("audio_id"),
                }
            )
        for value in shots.values():
            lines = value.get("dialogues") if isinstance(value, Mapping) else []
            if not isinstance(lines, list):
                lines = []
            by_id = {
                self._text(
                    item.get("dialogue_line_id") or item.get("line_id") or item.get("id")
                ): item
                for item in lines
                if isinstance(item, Mapping)
            }
            for audio_line in audio_lines.get(self._text(value.get("shot_id")), []):
                line_id = self._text(audio_line.get("dialogue_line_id"))
                if line_id in by_id:
                    by_id[line_id] = {
                        **dict(by_id[line_id]),
                        "audio_id": audio_line.get("audio_id"),
                    }
            value["dialogues"] = list(by_id.values()) if by_id else lines
        return shots

    def _persist_subtitle_timelines(
        self,
        *,
        job_id: str,
        run_id: str,
        stage_id: str,
        input_signature: str,
        upstream_input_signature: str,
        timelines: Sequence[tuple[str, Any]],
    ) -> list[str]:
        created_ids: list[str] = []
        with self.database.engine.begin() as connection:
            current_job = row_dict(
                connection.execute(sa.select(job).where(job.c.id == job_id)).first()
            )
            delivery_stage = row_dict(
                connection.execute(
                    sa.select(workflow_stage).where(
                        workflow_stage.c.id == stage_id,
                        workflow_stage.c.workflow_run_id == run_id,
                    )
                ).first()
            )
            latest_stage = row_dict(
                connection.execute(
                    sa.select(workflow_stage)
                    .where(
                        workflow_stage.c.workflow_run_id == run_id,
                        workflow_stage.c.stage_key == "6-delivery",
                    )
                    .order_by(
                        workflow_stage.c.attempt.desc(),
                        workflow_stage.c.updated_at.desc(),
                        workflow_stage.c.id.desc(),
                    )
                    .limit(1)
                ).first()
            )
            if (
                current_job is None
                or delivery_stage is None
                or latest_stage is None
                or latest_stage.get("id") != stage_id
                or current_job.get("status") not in NON_TERMINAL_STATUSES
                or current_job.get("workflow_stage_id") != stage_id
                or str(current_job.get("input_signature") or "") != input_signature
                or delivery_stage.get("status") in {"stale", "cancelled"}
            ):
                raise DeliveryExecutionError("字幕写入前 delivery attempt/signature 已变化")
            dependencies = json_loads(current_job.get("depends_on_json"), [])
            for dependency_id in dependencies if isinstance(dependencies, list) else []:
                dependency = self.repository.get(str(dependency_id))
                if dependency is None or dependency.get("status") != "completed":
                    raise DeliveryDependencyError(f"字幕依赖音频尚未完成：{dependency_id}")
            for shot_id, timeline in timelines:
                rows = connection.execute(
                    sa.select(shot_subtitle.c.id, shot_subtitle.c.provenance_json).where(
                        shot_subtitle.c.shot_id == shot_id
                    )
                ).all()
                remove_ids: list[str] = []
                for row in rows:
                    provenance = json_loads(row.provenance_json, {})
                    if (
                        isinstance(provenance, Mapping)
                        and provenance.get("workflow_run_id") == run_id
                        and provenance.get("workflow_stage_id") == stage_id
                        and provenance.get("input_signature") == input_signature
                    ):
                        remove_ids.append(str(row.id))
                if remove_ids:
                    connection.execute(
                        sa.delete(shot_subtitle).where(shot_subtitle.c.id.in_(remove_ids))
                    )
                for line in timeline.lines:
                    subtitle_id = "dynamic-subtitle:" + calculate_signature(
                        run_id,
                        stage_id,
                        input_signature,
                        shot_id,
                        line.dialogue_line_id,
                    )
                    provenance = {
                        "phase": "6b-subtitle",
                        "workflow_run_id": run_id,
                        "workflow_stage_id": stage_id,
                        "input_signature": input_signature,
                        "upstream_input_signature": upstream_input_signature,
                        "source_signature": timeline.source_signature,
                        "timeline_signature": timeline.timeline_signature,
                        "audio_id": line.audio_id,
                    }
                    connection.execute(
                        sa.insert(shot_subtitle).values(
                            id=subtitle_id,
                            shot_id=shot_id,
                            dialogue_line_id=line.dialogue_line_id,
                            audio_id=line.audio_id,
                            text=line.text,
                            start_time=line.start_time,
                            end_time=line.end_time,
                            style_preset=line.style_preset,
                            provenance_json=json_dumps(provenance),
                            created_at=utc_now(),
                        )
                    )
                    created_ids.append(subtitle_id)
        return created_ids

    async def handle_subtitle_job(self, job_record: Mapping[str, Any]) -> dict[str, Any]:
        """执行一个动态字幕子任务，使用现有纯字幕时间轴合同。"""

        job_id = self._text(job_record.get("id"))
        if not job_id:
            return {"status": "failed", "error": "字幕 Job 缺少 ID"}
        current = self.repository.get(job_id)
        if current is None:
            return {"status": "failed", "error": "字幕 Job 不存在"}
        if current.get("status") == "completed":
            return current.get("result_data") or {"status": "completed"}
        if current.get("status") not in NON_TERMINAL_STATUSES:
            return {
                "status": current.get("status"),
                "error": sanitize_error_message(current.get("error")),
            }
        dependency_status, dependency_error = self._job_dependency_state(current)
        if dependency_status == "failed":
            error = dependency_error or "字幕依赖失败"
            self.repository.fail(job_id, error)
            return {"status": "failed", "job_id": job_id, "error": error}
        if dependency_status == "pending":
            return {
                "status": "queued",
                "job_id": job_id,
                "error": "字幕依赖尚未完成，保持 queued",
            }
        try:
            current_context, run, stage = self._load_context(job_id)
            if not self._is_current_delivery_related_job(current_context, stage):
                error = "旧 delivery 子任务已不是当前 attempt，结果被丢弃"
                self.repository.interrupt(job_id, error)
                return {"status": "interrupted", "job_id": job_id, "error": error}
            input_data = current.get("input_data")
            if not isinstance(input_data, Mapping):
                input_data = json_loads(current.get("input_json"), {})
            shots = input_data.get("shots") if isinstance(input_data, Mapping) else {}
            if not isinstance(shots, Mapping):
                raise DeliveryExecutionError("字幕 Job 的 shots 输入必须是对象")
            input_signature = self._text(input_data.get("input_signature")) or (
                self._delivery_signature(run, stage)
            )
            upstream_input_signature = self._text(
                input_data.get("upstream_input_signature")
            ) or self._delivery_signature(run, stage)
            timelines: list[tuple[str, Any]] = []
            for shot_key, raw in shots.items():
                if not isinstance(raw, Mapping):
                    continue
                shot_id = self._text(raw.get("shot_id"))
                if not shot_id:
                    raise DeliveryExecutionError(f"字幕 Job 缺少 shot_id：{shot_key}")
                lines = raw.get("dialogues") or []
                if not isinstance(lines, list):
                    lines = [lines]
                with self.database.engine.connect() as connection:
                    audio_rows = connection.execute(
                        sa.select(shot_audio).where(
                            shot_audio.c.shot_id == shot_id,
                            shot_audio.c.is_current == 1,
                            shot_audio.c.status == "completed",
                        )
                    ).all()
                    video_duration = connection.scalar(
                        sa.select(video_version.c.actual_duration_sec).where(
                            video_version.c.shot_id == shot_id,
                            video_version.c.is_current == 1,
                            video_version.c.status == "completed",
                        )
                    )
                audio_durations: dict[str, Any] = {}
                for audio_row in audio_rows:
                    audio = row_dict(audio_row) or {}
                    audio_id = self._text(audio.get("id"))
                    line_id = self._text(audio.get("dialogue_line_id"))
                    duration = audio.get("duration_sec")
                    if audio_id:
                        audio_durations[audio_id] = duration
                    if line_id:
                        audio_durations[line_id] = duration
                timeline = build_subtitle_lines(
                    lines,
                    audio_durations=audio_durations,
                    shot_duration_sec=float(raw.get("duration_sec") or 6),
                    video_duration_sec=video_duration,
                    source_signature=input_signature,
                )
                timelines.append((shot_id, timeline))
            subtitle_ids = self._persist_subtitle_timelines(
                job_id=job_id,
                run_id=str(run["id"]),
                stage_id=str(stage["id"]),
                input_signature=input_signature,
                upstream_input_signature=upstream_input_signature,
                timelines=timelines,
            )
            latest = self.repository.get(job_id)
            if latest is None or not self._is_current_delivery_related_job(latest, stage):
                raise DeliveryExecutionError("字幕写入后 delivery attempt/signature 已变化")
            result = {
                "status": "completed",
                "job_id": job_id,
                "subtitle_ids": subtitle_ids,
                "timeline_signatures": [timeline.timeline_signature for _, timeline in timelines],
            }
            self.repository.complete(job_id, result)
            return result
        except (SubtitleTimelineError, DeliveryExecutionError, TypeError, ValueError) as exc:
            error = sanitize_error_message(exc)
            self.repository.fail(job_id, error)
            return {"status": "failed", "job_id": job_id, "error": error}

    async def _subtitle_subtask(
        self,
        snapshot: Mapping[str, Any],
        run: Mapping[str, Any],
        stage: Mapping[str, Any],
        config: Mapping[str, Any],
        signature: str,
        audio_result: Mapping[str, Any],
        delivery_generation: str = "",
    ) -> dict[str, Any]:
        if not self._bool(config, ("generate_subtitle", "burn_subtitles"), True):
            return {"kind": "subtitle", "status": "skipped", "reason": "运行配置未启用字幕"}
        shots = self._subtitle_shots(snapshot, audio_result)
        child_signature = calculate_signature(
            self._DELIVERY_VERSION,
            "subtitle",
            signature,
            shots,
            delivery_generation,
        )
        subtitle_idempotency_key = "dynamic-subtitle-v1:" + child_signature
        if delivery_generation:
            subtitle_idempotency_key += f":resume:{delivery_generation}"
        payload = {
            "job_type": self._SUBTITLE_JOB_TYPE,
            "target_type": "shot_subtitle",
            "target_id": run["chapter_id"],
            "provider_kind": "text",
            "input_signature": child_signature,
            "idempotency_key": subtitle_idempotency_key,
            "input_data": {
                "workflow_run_id": run["id"],
                "workflow_stage_id": stage["id"],
                "input_signature": child_signature,
                "upstream_input_signature": signature,
                "shots": shots,
                "delivery_generation": delivery_generation,
            },
            "depends_on": [
                str(item.get("job_id"))
                for item in audio_result.get("jobs", [])
                if isinstance(item, Mapping) and item.get("job_id")
            ],
        }
        child = self._ensure_job(payload, run=run, stage=stage)
        registered = await self._execute_registered_child(
            child,
            job_type=self._SUBTITLE_JOB_TYPE,
        )
        result = registered if registered is not None else await self.handle_subtitle_job(child)
        result = dict(result)
        result["kind"] = "subtitle"
        return result

    async def handle_bgm_job(self, job_record: Mapping[str, Any]) -> dict[str, Any]:
        """校验一个已选择的 BGM 子任务，不重新实现 Provider 或媒体生成。"""

        job_id = self._text(job_record.get("id"))
        if not job_id:
            return {"status": "failed", "error": "BGM Job 缺少 ID"}
        current = self.repository.get(job_id)
        if current is None:
            return {"status": "failed", "error": "BGM Job 不存在"}
        if current.get("status") == "completed":
            return current.get("result_data") or {"status": "completed", "job_id": job_id}
        if current.get("status") not in NON_TERMINAL_STATUSES:
            return {
                "status": current.get("status"),
                "error": sanitize_error_message(current.get("error")),
            }
        try:
            current_context, run, stage = self._load_context(job_id)
            if not self._is_current_delivery_related_job(current_context, stage):
                error = "旧 delivery 子任务已不是当前 attempt，结果被丢弃"
                self.repository.interrupt(job_id, error)
                return {"status": "interrupted", "job_id": job_id, "error": error}
            input_data = current.get("input_data")
            if not isinstance(input_data, Mapping):
                input_data = json_loads(current.get("input_json"), {})
            bgm_id = self._text(input_data.get("bgm_id"))
            if not bgm_id:
                raise DeliveryExecutionError("BGM Job 缺少 bgm_id")
            with self.database.engine.connect() as connection:
                bgm = row_dict(
                    connection.execute(
                        sa.select(project_bgm).where(
                            project_bgm.c.id == bgm_id,
                            project_bgm.c.project_id == run["project_id"],
                        )
                    ).first()
                )
            if bgm is None:
                raise DeliveryExecutionError("BGM 不存在或不属于当前项目")
            result = {
                "status": "completed",
                "job_id": job_id,
                "bgm_id": bgm_id,
                "file_path": bgm.get("file_path"),
                "workflow_run_id": run["id"],
                "workflow_stage_id": stage["id"],
            }
            self.repository.complete(job_id, result)
            return result
        except (DeliveryExecutionError, TypeError, ValueError) as exc:
            error = sanitize_error_message(exc)
            self.repository.fail(job_id, error)
            return {"status": "failed", "job_id": job_id, "error": error}

    def _composition_config(self, config: Mapping[str, Any]) -> dict[str, Any]:
        include_audio = self._bool(config, ("include_audio", "generate_audio"), True)
        burn_subtitles = self._bool(config, ("burn_subtitles", "generate_subtitle"), True)
        generate_bgm = self._bool(config, ("generate_bgm",), False)
        composition_config: dict[str, Any] = {
            "resolution": config.get("resolution", "1080p"),
            "include_audio": include_audio,
            "burn_subtitles": burn_subtitles,
            "crossfade": bool(config.get("crossfade", False)),
            "crossfade_seconds": config.get("crossfade_seconds", 0.5),
        }
        bgm_id = self._text(config.get("bgm_id")) if generate_bgm else ""
        if bgm_id:
            composition_config["bgm_id"] = bgm_id
            composition_config["bgm_volume"] = config.get("bgm_volume", 0.3)
        return composition_config

    @staticmethod
    def _composition_media_record(record: Mapping[str, Any]) -> dict[str, Any]:
        provenance = record.get("provenance")
        provenance = provenance if isinstance(provenance, Mapping) else {}
        return {
            "version_id": str(record.get("id") or ""),
            "file_path": str(record.get("path") or record.get("file_path") or ""),
            "input_signature": str(
                record.get("input_signature")
                or provenance.get("input_signature")
                or ""
            ),
            "attempt": provenance.get("workflow_stage_attempt")
            or provenance.get("source_attempt"),
            "revision": provenance.get("workflow_run_revision")
            or provenance.get("run_revision"),
            "workflow_run_id": str(
                record.get("workflow_run_id")
                or provenance.get("workflow_run_id")
                or ""
            ),
            "workflow_stage_id": str(
                record.get("workflow_stage_id")
                or provenance.get("workflow_stage_id")
                or ""
            ),
        }

    def _composition_media_snapshot(
        self,
        snapshot: Mapping[str, Any],
        *,
        run: Mapping[str, Any],
        stage: Mapping[str, Any],
    ) -> dict[str, Any]:
        shots = snapshot.get("shots")
        shots = shots if isinstance(shots, list) else []
        output: list[dict[str, Any]] = []
        for shot_payload in shots:
            if not isinstance(shot_payload, Mapping):
                continue
            shot_id = str(shot_payload.get("id") or "").strip()
            if not shot_id:
                continue
            video_values = shot_payload.get("video") or shot_payload.get("videos") or []
            audio_values = shot_payload.get("audio") or shot_payload.get("audios") or []
            video_values = video_values if isinstance(video_values, list) else [video_values]
            audio_values = audio_values if isinstance(audio_values, list) else [audio_values]
            video_records = [
                self._composition_media_record(item)
                for item in video_values
                if isinstance(item, Mapping) and item.get("id")
            ]
            audio_records = [
                self._composition_media_record(item)
                for item in audio_values
                if isinstance(item, Mapping) and item.get("id")
            ]
            output.append(
                {
                    "shot_id": shot_id,
                    "video_version_id": video_records[0]["version_id"] if video_records else None,
                    "audio_version_id": audio_records[0]["version_id"] if audio_records else None,
                    "video": video_records[0] if video_records else None,
                    "audio": audio_records[0] if audio_records else None,
                    "video_versions": video_records,
                    "audio_versions": audio_records,
                }
            )
        return {
            "version": "dynamic-composition-media-snapshot-v1",
            "workflow_run_id": str(run.get("id") or ""),
            "workflow_stage_id": str(stage.get("id") or ""),
            "workflow_stage_attempt": stage.get("attempt"),
            "workflow_run_revision": run.get("revision"),
            "input_signature": self._delivery_signature(run, stage),
            "shots": output,
        }

    def _queue_composition(
        self,
        run: Mapping[str, Any],
        stage: Mapping[str, Any],
        config: Mapping[str, Any],
        preflight: CompositionPreflightResult,
        dependency_job_ids: Sequence[str] = (),
        snapshot: Mapping[str, Any] | None = None,
        delivery_generation: str = "",
    ) -> dict[str, Any]:
        if self.composition_service is None:
            raise DeliveryExecutionError("未注册 CompositionService，无法创建成片 Job")
        composition_config = self._composition_config(config)
        signature = self._delivery_signature(run, stage)
        media_snapshot = self._composition_media_snapshot(
            snapshot or {},
            run=run,
            stage=stage,
        )
        with self.database.engine.connect() as connection:
            existing = row_dict(
                connection.execute(
                    sa.select(chapter_composition)
                    .select_from(
                        chapter_composition.join(
                            job,
                            chapter_composition.c.job_id == job.c.id,
                        )
                    )
                    .where(
                        chapter_composition.c.chapter_id == run["chapter_id"],
                        chapter_composition.c.workflow_run_id == run["id"],
                        chapter_composition.c.input_signature == signature,
                        job.c.workflow_stage_id == stage["id"],
                    )
                    .order_by(chapter_composition.c.created_at.desc())
                    .limit(1)
                ).first()
            )
        if existing is not None:
            existing_provenance = json_loads(existing.get("provenance_json"), {})
            existing_provenance = (
                existing_provenance if isinstance(existing_provenance, Mapping) else {}
            )
            existing_stage_id = self._text(
                existing.get("workflow_stage_id") or existing_provenance.get("workflow_stage_id")
            )
            if existing_stage_id and existing_stage_id != self._text(stage.get("id")):
                raise DeliveryExecutionError("Composition 记录属于旧 delivery attempt")
            existing_generation = self._text(existing_provenance.get("delivery_generation"))
            same_generation = existing_generation == self._text(delivery_generation)
            existing_job = (
                self.repository.get(str(existing.get("job_id")))
                if existing.get("job_id")
                else None
            )
            if same_generation and (
                existing.get("status") in {"failed", "cancelled"}
                or existing_job is not None
                and existing_job.get("status") in {"failed", "interrupted", "cancelled"}
            ):
                same_generation = False
            if existing.get("status") == "completed" and same_generation:
                return {
                    "kind": "composition",
                    "status": "completed",
                    "composition_id": existing.get("id"),
                    "job_id": existing.get("job_id"),
                    "preflight": preflight.model_dump(mode="json"),
                }
            if existing_job is None and existing.get("status") != "completed":
                raise DeliveryExecutionError("Composition 记录缺少可执行 Job")
            if (
                existing_job is not None
                and existing_job.get("status") in NON_TERMINAL_STATUSES
                and same_generation
            ):
                if not self._enqueue_job(str(existing_job["id"])):
                    raise DeliveryExecutionError("Composition Job 已绑定上下文但入队失败")
            if existing_job is not None and same_generation:
                return {
                    "kind": "composition",
                    "status": "queued",
                    "composition_id": existing.get("id"),
                    "job_id": existing.get("job_id"),
                    "preflight": preflight.model_dump(mode="json"),
                }
            if not delivery_generation:
                delivery_generation = "retry-" + calculate_signature(
                    "dynamic-composition-retry-v1",
                    existing.get("id"),
                    existing.get("updated_at"),
                )[:24]

        delivery_provenance = {
            "phase": "6c-composition",
            "delivery_version": self._DELIVERY_VERSION,
            "workflow_run_id": run["id"],
            "workflow_stage_id": stage["id"],
            "input_signature": signature,
            "upstream_input_signature": signature,
            "workflow_stage_attempt": stage.get("attempt"),
            "workflow_run_revision": run.get("revision"),
            "delivery_generation": delivery_generation,
            "media_snapshot": media_snapshot,
            "config": composition_config,
        }
        job_key = "dynamic-composition-v1:" + calculate_signature(
            run["id"], stage["id"], signature, composition_config, delivery_generation
        )
        result = self.composition_service.compose_chapter(
            str(run["chapter_id"]),
            composition_config,
            enqueue=False,
            workflow_context={
                "workflow_run_id": run["id"],
                "workflow_stage_id": stage["id"],
                "workflow_stage_attempt": stage.get("attempt"),
                "workflow_run_revision": run.get("revision"),
                "input_signature": signature,
                "delivery_generation": delivery_generation,
                "media_snapshot": media_snapshot,
                "idempotency_key": job_key,
                "preflight": preflight.model_dump(mode="json"),
                "provenance": delivery_provenance,
            },
            depends_on=dependency_job_ids,
        )
        composition = row_dict(result) or dict(result) if isinstance(result, Mapping) else {}
        composition_id = self._text(composition.get("id"))
        composition_job_id = self._text(composition.get("job_id"))
        if not composition_id or not composition_job_id:
            raise DeliveryExecutionError("CompositionService 未返回成片记录和 Job ID")
        if not self._enqueue_job(composition_job_id):
            raise DeliveryExecutionError("Composition Job 已完整写入但入队失败，可由 recovery 重试")
        return {
            "kind": "composition",
            "status": "queued",
            "composition_id": composition_id,
            "job_id": composition_job_id,
            "preflight": preflight.model_dump(mode="json"),
        }

    async def _bgm_subtask(
        self,
        run: Mapping[str, Any],
        stage: Mapping[str, Any],
        config: Mapping[str, Any],
        snapshot: Mapping[str, Any],
        signature: str,
    ) -> dict[str, Any]:
        if not self._bool(config, ("generate_bgm",), False):
            return {"kind": "bgm", "status": "skipped", "reason": "运行配置未启用 BGM"}
        bgm_id = self._text(config.get("bgm_id"))
        if not bgm_id:
            selected = snapshot.get("bgm")
            if isinstance(selected, list) and selected:
                bgm_id = (
                    self._text(selected[0].get("id"))
                    if isinstance(selected[0], Mapping)
                    else ""
                )
        if not bgm_id:
            return {"kind": "bgm", "status": "skipped", "reason": "未选择 BGM"}
        child_signature = calculate_signature(
            self._DELIVERY_VERSION,
            "bgm",
            signature,
            bgm_id,
        )
        child = self._ensure_job(
            {
                "job_type": self._BGM_JOB_TYPE,
                "target_type": "project_bgm",
                "target_id": bgm_id,
                "provider_kind": "text",
                "input_signature": child_signature,
                "idempotency_key": "dynamic-bgm-v1:" + child_signature,
                "input_data": {
                    "workflow_run_id": run["id"],
                    "workflow_stage_id": stage["id"],
                    "bgm_id": bgm_id,
                    "input_signature": child_signature,
                    "upstream_input_signature": signature,
                },
            },
            run=run,
            stage=stage,
        )
        result = await self.handle_bgm_job(child)
        return {"kind": "bgm", **result}

    def _is_current_delivery_job(self, job_id: str, stage_id: str) -> bool:
        with self.database.engine.connect() as connection:
            current_job = row_dict(
                connection.execute(sa.select(job).where(job.c.id == job_id)).first()
            )
            if current_job is None:
                return False
            stage = row_dict(
                connection.execute(
                    sa.select(workflow_stage).where(workflow_stage.c.id == stage_id)
                ).first()
            )
            if stage is None or stage.get("stage_key") != "6-delivery":
                return False
            latest = row_dict(
                connection.execute(
                    sa.select(workflow_stage)
                    .where(
                        workflow_stage.c.workflow_run_id == stage.get("workflow_run_id"),
                        workflow_stage.c.stage_key == "6-delivery",
                    )
                    .order_by(
                        workflow_stage.c.attempt.desc(),
                        workflow_stage.c.updated_at.desc(),
                        workflow_stage.c.id.desc(),
                    )
                    .limit(1)
                ).first()
            )
        return bool(
            latest is not None
            and latest.get("id") == stage_id
            and current_job.get("workflow_run_id") == stage.get("workflow_run_id")
            and current_job.get("workflow_stage_id") == stage_id
            and (
                current_job.get("input_signature") == latest.get("input_signature")
                or not current_job.get("input_signature")
                and not latest.get("input_signature")
            )
            and latest.get("status") not in {"stale", "cancelled"}
        )

    def _is_current_delivery_related_job(
        self,
        current_job: Mapping[str, Any],
        stage: Mapping[str, Any],
    ) -> bool:
        with self.database.engine.connect() as connection:
            latest = row_dict(
                connection.execute(
                    sa.select(workflow_stage)
                    .where(
                        workflow_stage.c.workflow_run_id == stage.get("workflow_run_id"),
                        workflow_stage.c.stage_key == "6-delivery",
                    )
                    .order_by(
                        workflow_stage.c.attempt.desc(),
                        workflow_stage.c.updated_at.desc(),
                        workflow_stage.c.id.desc(),
                    )
                    .limit(1)
                ).first()
            )
        if (
            latest is None
            or latest.get("id") != stage.get("id")
            or current_job.get("workflow_run_id") != stage.get("workflow_run_id")
            or latest.get("status") in {"stale", "cancelled"}
            or current_job.get("status") not in NON_TERMINAL_STATUSES
        ):
            return False
        input_data = current_job.get("input_data")
        input_data = input_data if isinstance(input_data, Mapping) else {}
        upstream_signature = self._text(input_data.get("upstream_input_signature"))
        if not upstream_signature:
            upstream_signature = self._text(current_job.get("input_signature"))
        return not latest.get("input_signature") or upstream_signature == self._text(
            latest.get("input_signature")
        )

    def _save_delivery_state(
        self,
        *,
        job_id: str,
        run_id: str,
        stage_id: str,
        job_status: str,
        status: str,
        result: Mapping[str, Any],
        error: str = "",
        expected_run_revision: int | None = None,
        expected_input_signature: str = "",
    ) -> bool:
        now = utc_now()
        # queued 是真实屏障状态，不能为了兼容旧 UI 映射成 ready。
        stage_status = "confirmed" if status == "completed" else status
        output_ref = {
            "capture_mode": "dynamic_delivery",
            "delivery_version": self._DELIVERY_VERSION,
            "workflow_run_id": run_id,
            "workflow_stage_id": stage_id,
            "status": status,
            "error": error,
            "subtasks": copy.deepcopy(result.get("subtasks", [])),
            "preflight": copy.deepcopy(result.get("preflight", {})),
        }
        with self.database.engine.begin() as connection:
            current_job = row_dict(
                connection.execute(sa.select(job).where(job.c.id == job_id)).first()
            )
            stage = row_dict(
                connection.execute(
                    sa.select(workflow_stage).where(
                        workflow_stage.c.id == stage_id,
                        workflow_stage.c.workflow_run_id == run_id,
                    )
                ).first()
            )
            latest_stage = row_dict(
                connection.execute(
                    sa.select(workflow_stage)
                    .where(
                        workflow_stage.c.workflow_run_id == run_id,
                        workflow_stage.c.stage_key == "6-delivery",
                    )
                    .order_by(
                        workflow_stage.c.attempt.desc(),
                        workflow_stage.c.updated_at.desc(),
                        workflow_stage.c.id.desc(),
                    )
                    .limit(1)
                ).first()
            )
            if (
                stage is None
                or latest_stage is None
                or latest_stage.get("id") != stage_id
                or latest_stage.get("attempt") != stage.get("attempt")
                or stage.get("status") in {"cancelled", "stale"}
                or current_job is None
                or current_job.get("workflow_run_id") != run_id
                or current_job.get("workflow_stage_id") != stage_id
                or current_job.get("status") not in NON_TERMINAL_STATUSES
            ):
                raise DeliveryStaleError("delivery Job 已不是当前 attempt，拒绝写入终态")
            current_run = row_dict(
                connection.execute(
                    sa.select(workflow_run).where(workflow_run.c.id == run_id)
                ).first()
            )
            if current_run is None or current_run.get("status") in {
                "cancelled",
                "stale",
                "blocked",
            }:
                raise DeliveryStaleError("delivery run 已取消或 stale，拒绝写入终态")
            run_revision = current_run.get("revision")
            if expected_run_revision is not None and run_revision != expected_run_revision:
                raise DeliveryStaleError("delivery run revision 已变化，拒绝写入终态")
            current_signature = str(stage.get("input_signature") or "")
            if expected_input_signature and current_signature != expected_input_signature:
                raise DeliveryStaleError("delivery input_signature 已变化，拒绝写入终态")
            job_signature = str(current_job.get("input_signature") or "")
            if job_signature != current_signature:
                raise DeliveryStaleError("delivery Job input_signature 已变化，拒绝写入终态")
            persisted_stage_status = stage_status
            output_refs = json_loads(stage.get("output_refs_json"), [])
            output_refs = output_refs if isinstance(output_refs, list) else []
            output_refs = [
                item
                for item in output_refs
                if not (
                    isinstance(item, Mapping)
                    and item.get("capture_mode") == "dynamic_delivery"
                )
            ]
            output_refs.append(output_ref)
            finished_at = now if stage_status not in {"queued", "running"} else None
            stage_result = connection.execute(
                sa.update(workflow_stage)
                .where(
                    workflow_stage.c.id == stage_id,
                    workflow_stage.c.workflow_run_id == run_id,
                    workflow_stage.c.input_signature == stage.get("input_signature"),
                    workflow_stage.c.status.not_in(("cancelled", "stale")),
                )
                .values(
                    status=persisted_stage_status,
                    error=error,
                    output_refs_json=json_dumps(output_refs),
                    finished_at=finished_at,
                    updated_at=now,
                )
            )
            if stage_result.rowcount != 1:
                raise DeliveryStaleError("delivery stage 条件更新未命中")
            current_run_status = str(current_run.get("status") or "")
            run_values: dict[str, Any] = {
                "status": "stale" if current_run_status == "stale" else stage_status,
                "error": error,
                "updated_at": now,
                "finished_at": finished_at,
            }
            run_result = connection.execute(
                sa.update(workflow_run)
                .where(
                    workflow_run.c.id == run_id,
                    workflow_run.c.revision == run_revision,
                    workflow_run.c.status.not_in(("cancelled", "stale")),
                )
                .values(**run_values, revision=workflow_run.c.revision + 1)
            )
            if run_result.rowcount != 1:
                raise DeliveryStaleError("delivery run 条件更新未命中")
            job_signature_condition = (
                job.c.input_signature == job_signature
                if job_signature
                else sa.or_(job.c.input_signature == "", job.c.input_signature.is_(None))
            )
            job_result = connection.execute(
                sa.update(job)
                .where(
                    job.c.id == job_id,
                    job.c.workflow_run_id == run_id,
                    job.c.workflow_stage_id == stage_id,
                    job.c.status.in_(NON_TERMINAL_STATUSES),
                    job_signature_condition,
                )
                .values(
                    status=job_status,
                    progress_current=job.c.progress_total
                    if job_status == "completed"
                    else job.c.progress_current,
                    result_json=json_dumps(dict(result)),
                    error=error,
                    message="任务完成" if job_status == "completed" else "任务失败",
                    finished_at=now,
                    updated_at=now,
                )
            )
            if job_result.rowcount != 1:
                raise DeliveryStaleError("delivery Job 条件更新未命中")
        return True

    def _finish_job(
        self,
        job_id: str,
        result: dict[str, Any],
        *,
        failed: bool,
        run_id: str,
        stage_id: str,
        error: str = "",
        interrupted: bool = False,
        expected_run_revision: int | None = None,
    ) -> dict[str, Any]:
        final_status = self._text(result.get("status")) or ("failed" if failed else "completed")
        result["status"] = final_status
        if not self._is_current_delivery_job(job_id, stage_id):
            stale_error = "旧 delivery Job 已不是当前 attempt，结果被丢弃"
            result.update(status="interrupted", error=stale_error)
            try:
                current = self.repository.get(job_id)
                if current is not None and current.get("status") in NON_TERMINAL_STATUSES:
                    self.repository.interrupt(job_id, stale_error)
            except Exception:
                pass
            return result
        workflow_status = (
            "interrupted"
            if interrupted or final_status == "interrupted"
            else "partial_failed"
            if final_status == "partial_failed"
            else "failed"
            if failed or final_status in {"failed", "blocked"}
            else "confirmed"
            if final_status == "completed"
            else "queued"
        )
        job_status = "interrupted" if interrupted else "failed" if failed else "completed"
        try:
            self._save_delivery_state(
                job_id=job_id,
                run_id=run_id,
                stage_id=stage_id,
                job_status=job_status,
                status=workflow_status,
                result=result,
                error=error,
                expected_run_revision=expected_run_revision,
                expected_input_signature=str(result.get("input_signature") or ""),
            )
        except Exception as exc:
            error = error or f"6-delivery 状态快照写入失败：{sanitize_error_message(exc)}"
            # 被新 attempt 取代属于过期而非失败，按异常类型判断，不要匹配错误文案。
            result["status"] = "interrupted" if isinstance(exc, DeliveryStaleError) else "failed"
            result["error"] = error
            try:
                current = self.repository.get(job_id)
                if current is not None and current.get("status") in NON_TERMINAL_STATUSES:
                    if result["status"] == "interrupted":
                        self.repository.interrupt(job_id, error)
                    else:
                        self.repository.fail(job_id, error)
            except Exception:
                pass
        return result

    async def _execute_delivery(
        self,
        current_job: Mapping[str, Any],
        run: Mapping[str, Any],
        stage: Mapping[str, Any],
    ) -> dict[str, Any]:
        run_id = str(run["id"])
        stage_id = str(stage["id"])
        self._validate_delivery_input_snapshot(run, stage)
        self._assert_prompt_stages_confirmed(run_id)
        await self._wait_for_delivery_dependencies(run_id, stage, current_job)
        self._start_delivery(
            run_id,
            stage_id,
            str(stage.get("input_signature") or ""),
        )
        config = self._config(run)
        delivery_generation = self._delivery_generation(current_job)
        allow_partial = self._bool(
            config,
            ("allow_partial", "partial_delivery", "delivery_allow_partial"),
            False,
        ) or self._text(config.get("delivery_mode")).casefold() == "partial"
        signature = self._delivery_signature(run, stage)
        snapshot = DynamicDeliveryService(
            self.database,
            file_service=self.file_service,
        ).build_snapshot(run_id)
        subtasks: list[dict[str, Any]] = []
        audio_result = await self._audio_subtask(
            snapshot,
            run,
            stage,
            config,
            signature,
            delivery_generation,
        )
        subtasks.append(audio_result)
        if audio_result.get("status") == "failed" and not allow_partial:
            return {
                "status": "partial_failed",
                "error": "动态音频子任务失败",
                "subtasks": subtasks,
            }

        snapshot = DynamicDeliveryService(
            self.database,
            file_service=self.file_service,
        ).build_snapshot(run_id)
        subtitle_result = await self._subtitle_subtask(
            snapshot,
            run,
            stage,
            config,
            signature,
            audio_result,
            delivery_generation,
        )
        subtasks.append(subtitle_result)
        if subtitle_result.get("status") == "failed" and not allow_partial:
            return {
                "status": "partial_failed",
                "error": "动态字幕子任务失败",
                "subtasks": subtasks,
            }

        snapshot = DynamicDeliveryService(
            self.database,
            file_service=self.file_service,
        ).build_snapshot(run_id)
        bgm_result = await self._bgm_subtask(run, stage, config, snapshot, signature)
        subtasks.append(bgm_result)
        delivery = DynamicDeliveryService(self.database, file_service=self.file_service)
        preflight = delivery._preflight_snapshot(
            snapshot,
            strict=not allow_partial,
            allow_partial=allow_partial,
        )
        preflight_payload = preflight.model_dump(mode="json")
        if preflight.status == "blocked":
            error = (
                "交付预检失败："
                f"missing={len(preflight.missing)}, failed={len(preflight.failed)}"
            )
            return {
                "status": "failed" if preflight.failed else "partial_failed",
                "error": error,
                "subtasks": subtasks,
                "preflight": preflight_payload,
            }

        if not self._bool(config, ("generate_composition",), True):
            subtasks.append(
                {"kind": "composition", "status": "skipped", "reason": "运行配置未启用成片合成"}
            )
        elif not can_start_composition(preflight):
            return {
                "status": "partial_failed",
                "error": "交付预检尚未允许创建成片 Job",
                "subtasks": subtasks,
                "preflight": preflight_payload,
            }
        else:
            composition_dependencies = [
                str(value)
                for value in current_job.get("depends_on", [])
                if str(value).strip()
            ]
            for subtask in (audio_result, subtitle_result, bgm_result):
                for item in subtask.get("jobs", []) if isinstance(subtask, Mapping) else []:
                    if isinstance(item, Mapping) and item.get("job_id"):
                        composition_dependencies.append(str(item["job_id"]))
                if isinstance(subtask, Mapping) and subtask.get("job_id"):
                    composition_dependencies.append(str(subtask["job_id"]))
            composition_dependencies = list(dict.fromkeys(composition_dependencies))
            subtasks.append(
                self._queue_composition(
                    run,
                    stage,
                    config,
                    preflight,
                    composition_dependencies,
                    snapshot=snapshot,
                    delivery_generation=delivery_generation,
                )
            )

        disabled_kinds = {
            kind
            for kind, names in (
                ("audio", ("generate_audio", "include_audio")),
                ("subtitle", ("generate_subtitle", "burn_subtitles")),
                ("bgm", ("generate_bgm",)),
                ("composition", ("generate_composition",)),
            )
            if not self._bool(config, names, True)
        }
        partial = any(
            item.kind not in disabled_kinds for item in preflight.skipped
        ) or any(
            item.get("status") in {"failed", "interrupted"} for item in subtasks
        )
        composition_queued = any(
            item.get("kind") == "composition" and item.get("status") == "queued"
            for item in subtasks
        )
        return {
            "status": (
                "partial_failed"
                if partial
                else "queued"
                if composition_queued
                else "completed"
            ),
            "subtasks": subtasks,
            "preflight": preflight_payload,
            "input_signature": signature,
            "provenance": {
                "delivery_version": self._DELIVERY_VERSION,
                "workflow_run_id": run_id,
                "workflow_stage_id": stage_id,
                "upstream_input_signature": signature,
            },
        }

    async def execute_job(self, job_record: Mapping[str, Any]) -> dict[str, Any]:
        """执行 6-delivery 顶层 Job，并保证失败不会被 runner 自动完成。"""

        job_id = self._text(job_record.get("id"))
        if not job_id:
            return {"status": "failed", "error": "6-delivery Job 缺少 ID"}
        current = self.repository.get(job_id)
        if current is None:
            return {"status": "failed", "error": "6-delivery Job 不存在"}
        if current.get("status") == "completed":
            return current.get("result_data") or {"status": "completed", "job_id": job_id}
        if current.get("status") not in NON_TERMINAL_STATUSES:
            return {
                "status": current.get("status"),
                "error": sanitize_error_message(current.get("error")),
            }

        run_id = ""
        stage_id = ""
        run_revision: int | None = None
        try:
            current, run, stage = self._load_context(job_id)
            run_id = str(run["id"])
            stage_id = str(stage["id"])
            run_revision = int(run["revision"]) if run.get("revision") is not None else None
            result = await self._execute_delivery(current, run, stage)
            result["job_id"] = job_id
            result_status = self._text(result.get("status"))
            if result_status in {"failed", "partial_failed", "blocked"}:
                return self._finish_job(
                    job_id,
                    result,
                    failed=True,
                    run_id=run_id,
                    stage_id=stage_id,
                    error=self._text(result.get("error")),
                    expected_run_revision=run_revision,
                )
            return self._finish_job(
                job_id,
                result,
                failed=False,
                run_id=run_id,
                stage_id=stage_id,
                expected_run_revision=run_revision,
            )
        except DeliveryDependencyError as exc:
            error = sanitize_error_message(exc)
            result = {
                "status": "partial_failed",
                "job_id": job_id,
                "error": error,
                "subtasks": [],
            }
            if run_id and stage_id:
                return self._finish_job(
                    job_id,
                    result,
                    failed=True,
                    run_id=run_id,
                    stage_id=stage_id,
                    error=error,
                    expected_run_revision=run_revision,
                )
            self.repository.fail(job_id, error)
            return result
        except asyncio.CancelledError:
            error = "6-delivery 任务已中断"
            if run_id and stage_id:
                return self._finish_job(
                    job_id,
                    {"status": "interrupted", "job_id": job_id, "error": error},
                    failed=True,
                    interrupted=True,
                    run_id=run_id,
                    stage_id=stage_id,
                    error=error,
                    expected_run_revision=run_revision,
                )
            self.repository.interrupt(job_id, error)
            return {"status": "interrupted", "job_id": job_id, "error": error}
        except Exception as exc:
            error = sanitize_error_message(exc) or "6-delivery 执行失败"
            stale = "交付输入快照已过期" in error
            partial = any(
                marker in error
                for marker in ("前置 Prompt", "媒体前置", "阶段前置确认未满足")
            )
            result = {
                "status": "interrupted" if stale else "partial_failed" if partial else "failed",
                "job_id": job_id,
                "error": error,
                "subtasks": [],
            }
            if run_id and stage_id:
                return self._finish_job(
                    job_id,
                    result,
                    failed=not stale,
                    interrupted=stale,
                    run_id=run_id,
                    stage_id=stage_id,
                    error=error,
                    expected_run_revision=run_revision,
                )
            self.repository.fail(job_id, error)
            return result


__all__ = [
    "DeliveryDependencyError",
    "DeliveryExecutionError",
    "DeliveryServiceError",
    "DynamicDeliveryJobService",
    "DynamicDeliveryService",
]
