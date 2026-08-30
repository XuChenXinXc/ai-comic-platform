"""确认后的 5a/5b Prompt stage 媒体版本和 Job 入队服务。

本模块只使用 SQLAlchemy Core 写入动态媒体目标，不调用 Provider、JobService 或旧的
MediaService。阶段确认、媒体版本、Job 和运行 revision 在一个事务中完成；外部队列回调
只在事务提交后执行，回调失败不会回滚已经保存的 queued 记录。
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import Engine, func, insert, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.db import Database, db, utc_now
from app.dynamic_pipeline.diagnostics import diagnostic, diagnostics_status
from app.dynamic_pipeline.media_bridge import (
    DynamicMediaBridgePlan,
    MediaTargetPlan,
    build_video_media_plan,
    to_job_payload,
)
from app.dynamic_pipeline.prompt_ir import prompt_input_hash
from app.dynamic_pipeline.prompt_stage import (
    PromptStageError,
    PromptStageOutput,
    stage_output_to_media_plans,
)
from app.dynamic_pipeline.schemas import Diagnostic
from app.jobs.repository import JobRepository
from app.jobs.runner import job_runner
from app.models.tables import (
    api_provider,
    job,
    keyframe_version,
    project,
    shot,
    video_version,
    workflow_run,
    workflow_stage,
)
from app.services.utils import json_dumps, row_dict

QUEUE_SERVICE_VERSION = "dynamic-media-queue-v1"
_PROMPT_STAGE_KEYS = frozenset({"5a-image-prompts", "5b-video-prompts"})
_QUEUEABLE_TARGET_STATUSES = frozenset({"ready", "warning"})
_DEFAULT_ENQUEUE = object()


class MediaQueueError(RuntimeError):
    """动态媒体入队前置条件、数据库或快照错误。"""


def _copy_json(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return copy.deepcopy(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {str(key): _copy_json(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_copy_json(item) for item in value]
    if isinstance(value, set | frozenset):
        return [_copy_json(item) for item in sorted(value, key=repr)]
    return copy.deepcopy(value)


def _json_text(value: Any) -> str:
    return json.dumps(
        _copy_json(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _required_text(value: Any, name: str) -> str:
    result = _text(value)
    if not result:
        raise MediaQueueError(f"{name} 不能为空")
    return result


def _row(row: Any) -> dict[str, Any] | None:
    return row_dict(row)


def _diagnostic(
    level: Literal["warning", "blocking"],
    code: str,
    message: str,
    *,
    path: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> Diagnostic:
    return diagnostic(level, code, message, path=path, details=details)


def _diagnostic_key(value: Diagnostic) -> str:
    return _json_text(value.model_dump(mode="json"))


def _dedupe_diagnostics(values: list[Diagnostic]) -> list[Diagnostic]:
    result: list[Diagnostic] = []
    seen: set[str] = set()
    for value in values:
        key = _diagnostic_key(value)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _json_column(value: Any, default: Any) -> Any:
    if isinstance(value, dict | list):
        return copy.deepcopy(value)
    if not value:
        return copy.deepcopy(default)
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return copy.deepcopy(default)
    return parsed


def _parse_json_column(value: Any, label: str) -> Any:
    if isinstance(value, dict | list):
        return copy.deepcopy(value)
    if value is None or (isinstance(value, str) and not value.strip()):
        return {}
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise MediaQueueError(f"{label} 不是有效 JSON") from exc


def _unwrap_stage_payload(value: Any) -> Any:
    """兼容 Prompt stage 结果提交时可能存在的结果 envelope。"""

    current = value
    if isinstance(current, str):
        try:
            current = json.loads(current)
        except (TypeError, json.JSONDecodeError) as exc:
            raise MediaQueueError("PromptStage JSON 解析失败") from exc
    if not isinstance(current, Mapping):
        return current
    for key in ("compiled_output", "stage_output", "content", "data", "output"):
        nested = current.get(key)
        if isinstance(nested, Mapping) and (
            "bundles" in nested or "stage_key" in nested or "artifact_meta" in nested
        ):
            return _unwrap_stage_payload(nested)
    return current


def _has_stage_content(value: Any) -> bool:
    unwrapped = _unwrap_stage_payload(value)
    return isinstance(unwrapped, Mapping) and bool(unwrapped)


def _number_sort(value: Any) -> tuple[int, float]:
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


@dataclass(frozen=True)
class _PreparedStage:
    output: PromptStageOutput
    shot_map: dict[str, dict[str, Any]]
    bundle_keys: tuple[str, ...]


@dataclass(frozen=True)
class _ShotMapping:
    shot_id: str | None
    diagnostics: tuple[Diagnostic, ...]


@dataclass(frozen=True)
class _KeyframeReference:
    job_id: str
    version_id: str


@dataclass(frozen=True)
class _QueuedTarget:
    target: MediaTargetPlan
    shot_id: str
    version: dict[str, Any]
    job: dict[str, Any]
    version_created: bool
    job_created: bool


def _prepare_stage_payload(
    value: Any,
    *,
    stage_key: str,
    workflow_run_id: str,
    workflow_stage_id: str,
    input_signature: str,
) -> _PreparedStage:
    root = _unwrap_stage_payload(value)
    if not isinstance(root, Mapping):
        raise MediaQueueError("PromptStage 输出必须是对象")

    raw_bundles = root.get("bundles")
    if not isinstance(raw_bundles, Mapping):
        raise MediaQueueError("PromptStage 输出缺少 bundles 对象")

    raw_shot_map = root.get("shot_map", {})
    if raw_shot_map is None:
        raw_shot_map = {}
    if not isinstance(raw_shot_map, Mapping):
        raise MediaQueueError("PromptStage 输出的 shot_map 必须是对象")
    shot_map: dict[str, dict[str, Any]] = {}
    for raw_key, raw_value in raw_shot_map.items():
        key = _text(raw_key)
        if not key:
            raise MediaQueueError("PromptStage 输出的 shot_map key 不能为空")
        if not isinstance(raw_value, Mapping):
            raise MediaQueueError(f"shot_map[{key!r}] 必须是对象")
        shot_map[key] = _copy_json(raw_value)

    normalized_bundles: dict[str, Any] = {}
    for raw_key, raw_value in raw_bundles.items():
        key = _text(raw_key)
        if not key:
            raise MediaQueueError("PromptStage bundles key 不能为空")
        if key in normalized_bundles:
            raise MediaQueueError(f"PromptStage bundles key 重复：{key!r}")
        normalized_bundles[key] = raw_value
    bundle_keys = list(normalized_bundles)
    if not bundle_keys:
        raise MediaQueueError("PromptStage bundles key 不能为空")

    def sort_key(key: str) -> tuple[Any, ...]:
        mapping = shot_map.get(key, {})
        external_key = _text(mapping.get("external_key"))
        return (
            _number_sort(mapping.get("ordinal")),
            _number_sort(mapping.get("shot_no")),
            external_key,
            key,
        )

    ordered_keys = tuple(sorted(bundle_keys, key=sort_key))
    bundles: dict[str, Any] = {}
    for key in ordered_keys:
        bundle = _copy_json(normalized_bundles[key])
        if not isinstance(bundle, Mapping):
            raise MediaQueueError(f"PromptStage bundles[{key!r}] 必须是对象")
        bundle = dict(bundle)
        provenance = bundle.get("provenance", {})
        if not isinstance(provenance, Mapping):
            provenance = {}
        provenance = dict(_copy_json(provenance))
        mapping = shot_map.get(key, {})
        # shot_map 是 stage 层显式投影；如果存在，就覆盖 bundle provenance 中的旧投影。
        for field in ("shot_id", "external_key", "ordinal", "shot_no"):
            if field in mapping and mapping[field] is not None:
                provenance[field] = _copy_json(mapping[field])
        provenance.setdefault("shot_id", key)
        provenance.setdefault("external_key", key)
        bundle["provenance"] = provenance
        bundles[key] = bundle

    provenance = root.get("provenance", {})
    if not isinstance(provenance, Mapping):
        raise MediaQueueError("PromptStage provenance 必须是对象")
    stage_provenance = dict(_copy_json(provenance))
    stage_provenance.update(
        {
            "workflow_run_id": workflow_run_id,
            "workflow_stage_id": workflow_stage_id,
            "input_signature": input_signature,
            "shot_keys": list(ordered_keys),
        }
    )

    payload = dict(_copy_json(root))
    payload.pop("shot_map", None)
    raw_stage_key = _text(root.get("stage_key"))
    if raw_stage_key and raw_stage_key != stage_key:
        raise MediaQueueError(
            f"PromptStage stage_key 不匹配：expected={stage_key!r}, actual={raw_stage_key!r}"
        )
    payload["stage_key"] = stage_key
    payload["bundles"] = bundles
    payload["provenance"] = stage_provenance
    if not payload.get("status"):
        payload["status"] = "ready"

    try:
        output = PromptStageOutput.model_validate(payload)
    except (TypeError, ValueError) as exc:
        raise MediaQueueError("PromptStage 输出不符合 PromptStage schema") from exc
    if output.stage_key != stage_key:
        raise MediaQueueError(
            f"PromptStage stage_key 不匹配：expected={stage_key!r}, actual={output.stage_key!r}"
        )
    return _PreparedStage(output=output, shot_map=shot_map, bundle_keys=ordered_keys)


def _target_with_diagnostics(
    target: MediaTargetPlan,
    diagnostics: list[Diagnostic],
) -> MediaTargetPlan:
    ordered = diagnostics_status(_dedupe_diagnostics(diagnostics)).diagnostics
    status = diagnostics_status(ordered).status
    provenance = _copy_json(target.provenance_json)
    if not isinstance(provenance, dict):
        provenance = {}
    provenance["diagnostics"] = [item.model_dump(mode="json") for item in ordered]
    provenance["status"] = status
    return target.model_copy(
        deep=True,
        update={
            "status": status,
            "diagnostics": list(ordered),
            "provenance_json": provenance,
        },
    )


def _plan_with_targets(
    source: DynamicMediaBridgePlan,
    targets: list[MediaTargetPlan],
) -> DynamicMediaBridgePlan:
    all_diagnostics: list[Diagnostic] = list(source.diagnostics)
    for target in targets:
        all_diagnostics.extend(target.diagnostics)
    gate = diagnostics_status(_dedupe_diagnostics(all_diagnostics))
    return source.model_copy(
        deep=True,
        update={
            "targets": targets,
            "status": gate.status,
            "diagnostics": list(gate.diagnostics),
        },
    )


class DynamicMediaQueueService:
    """把已确认的 Prompt stage 原子地投影为媒体版本和 queued Job。"""

    def __init__(
        self,
        database: Database | Engine | None,
        *,
        job_repository: JobRepository | None = None,
        enqueue: Callable[[str], Any] | None | object = _DEFAULT_ENQUEUE,
    ) -> None:
        if database is None:
            self.database = db
            self.engine = db.engine
        elif isinstance(database, Database):
            self.database = database
            self.engine = database.engine
        else:
            self.database = database
            self.engine = database
        self.job_repository = job_repository or JobRepository(self.engine)
        self.enqueue = job_runner.enqueue if enqueue is _DEFAULT_ENQUEUE else enqueue

    @staticmethod
    def _run(connection: Any, run_id: str) -> dict[str, Any] | None:
        return _row(
            connection.execute(select(workflow_run).where(workflow_run.c.id == run_id)).first()
        )

    @staticmethod
    def _stage(connection: Any, run_id: str, stage_id: str) -> dict[str, Any] | None:
        return _row(
            connection.execute(
                select(workflow_stage).where(
                    workflow_stage.c.id == stage_id,
                    workflow_stage.c.workflow_run_id == run_id,
                )
            ).first()
        )

    @staticmethod
    def _provider_exists(connection: Any, provider_id: str, kind: str) -> bool:
        row = connection.execute(
            select(api_provider.c.id, api_provider.c.kind).where(
                api_provider.c.id == provider_id,
                api_provider.c.kind == kind,
                api_provider.c.enabled == 1,
            )
        ).first()
        return row is not None

    @staticmethod
    def _resolve_provider(
        connection: Any,
        run: Mapping[str, Any],
        *,
        kind: str,
        provider_id: str,
        provider_model: str,
    ) -> tuple[str, str]:
        selected_id = _text(provider_id)
        selected_model = _text(provider_model)
        if not selected_id:
            column = (
                project.c.default_image_provider_id
                if kind == "image"
                else project.c.default_video_provider_id
            )
            project_id = _text(run.get("project_id"))
            if project_id:
                selected_id = _text(
                    connection.scalar(select(column).where(project.c.id == project_id))
                )
            if not selected_id:
                selected_id = _text(
                    connection.scalar(
                        select(api_provider.c.id)
                        .where(
                            api_provider.c.kind == kind,
                            api_provider.c.enabled == 1,
                            api_provider.c.is_default == 1,
                        )
                        .order_by(api_provider.c.id)
                        .limit(1)
                    )
                )
        if selected_id and not selected_model:
            selected_model = _text(
                connection.scalar(
                    select(api_provider.c.model).where(
                        api_provider.c.id == selected_id,
                        api_provider.c.kind == kind,
                        api_provider.c.enabled == 1,
                    )
                )
            )
        return selected_id, selected_model

    @staticmethod
    def _shot_mapping(
        connection: Any,
        run: Mapping[str, Any],
        target: MediaTargetPlan,
    ) -> _ShotMapping:
        external_key = _text(target.external_key)
        if not external_key:
            return _ShotMapping(
                shot_id=None,
                diagnostics=(
                    _diagnostic(
                        "blocking",
                        "media_queue_external_key_missing",
                        "媒体目标缺少显式 external_key，不能映射 Shot",
                        path="external_key",
                    ),
                ),
            )
        rows = connection.execute(
            select(shot.c.id)
            .where(
                shot.c.project_id == run.get("project_id"),
                shot.c.chapter_id == run.get("chapter_id"),
                shot.c.external_key == external_key,
            )
            .order_by(shot.c.id)
        ).all()
        if not rows:
            return _ShotMapping(
                shot_id=None,
                diagnostics=(
                    _diagnostic(
                        "blocking",
                        "media_queue_shot_external_key_not_found",
                        f"找不到 external_key={external_key!r} 对应的 Shot",
                        path="external_key",
                        details={"external_key": external_key},
                    ),
                ),
            )
        if len(rows) != 1:
            return _ShotMapping(
                shot_id=None,
                diagnostics=(
                    _diagnostic(
                        "blocking",
                        "media_queue_shot_external_key_duplicate",
                        f"external_key={external_key!r} 对应多个 Shot，拒绝猜测映射",
                        path="external_key",
                        details={
                            "external_key": external_key,
                            "shot_ids": sorted(str(row.id) for row in rows),
                        },
                    ),
                ),
            )
        return _ShotMapping(shot_id=str(rows[0].id), diagnostics=())

    @staticmethod
    def _keyframe_jobs(
        connection: Any,
        workflow_run_id: str,
        *,
        workflow_stage_id: str | None = None,
    ) -> dict[str, _KeyframeReference]:
        statement = (
            select(job, shot.c.external_key.label("shot_external_key"))
            .select_from(
                job.join(keyframe_version, job.c.target_id == keyframe_version.c.id).join(
                    shot, keyframe_version.c.shot_id == shot.c.id
                )
            )
            .where(
                job.c.workflow_run_id == workflow_run_id,
                job.c.job_type == "generate_keyframe",
                job.c.target_type == "keyframe",
                job.c.status.in_(
                    ("queued", "running", "waiting_provider", "downloading", "completed")
                ),
            )
        )
        if workflow_stage_id:
            statement = statement.where(job.c.workflow_stage_id == workflow_stage_id)
        rows = connection.execute(statement.order_by(job.c.created_at, job.c.id)).all()
        result: dict[str, _KeyframeReference] = {}
        for row in rows:
            record = _row(row)
            if record is None:
                continue
            external_key = _text(record.pop("shot_external_key", ""))
            if not external_key or not record.get("id") or not record.get("target_id"):
                continue
            version_row = _row(
                connection.execute(
                    select(keyframe_version.c.provenance_json).where(
                        keyframe_version.c.id == record["target_id"]
                    )
                ).first()
            )
            provenance = _json_column(
                version_row.get("provenance_json") if version_row else None,
                {},
            )
            if not isinstance(provenance, Mapping):
                continue
            if _text(provenance.get("workflow_run_id")) != workflow_run_id:
                continue
            result[external_key] = _KeyframeReference(
                job_id=str(record["id"]),
                version_id=str(record["target_id"]),
            )
        return result

    @staticmethod
    def _existing_dynamic_version(
        connection: Any,
        table: Any,
        *,
        shot_id: str,
        input_signature: str,
        workflow_run_id: str,
        workflow_stage_id: str,
    ) -> dict[str, Any] | None:
        rows = connection.execute(
            select(table)
            .where(
                table.c.shot_id == shot_id,
                table.c.input_signature == input_signature,
                table.c.source == "generated",
            )
            .order_by(table.c.version_no, table.c.id)
        ).all()
        for row in rows:
            record = _row(row)
            if record is None:
                continue
            provenance = _json_column(record.get("provenance_json"), {})
            if not isinstance(provenance, Mapping):
                continue
            if (
                _text(provenance.get("workflow_run_id")) == workflow_run_id
                and _text(provenance.get("workflow_stage_id")) == workflow_stage_id
            ):
                return record
        return None

    @staticmethod
    def _version_id(
        connection: Any,
        table: Any,
        *,
        base_id: str,
        input_signature: str,
    ) -> str:
        existing = connection.execute(
            select(table.c.id, table.c.input_signature).where(table.c.id == base_id)
        ).first()
        if existing is None or str(existing.input_signature or "") == input_signature:
            return base_id
        return f"{base_id}:{input_signature}"

    @staticmethod
    def _existing_job(
        connection: Any,
        *,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        return _row(
            connection.execute(select(job).where(job.c.idempotency_key == idempotency_key)).first()
        )

    @staticmethod
    def _job_id(
        *,
        workflow_stage_id: str,
        target_type: str,
        target_id: str,
        input_signature: str,
        job_type: str,
    ) -> str:
        return "dynamic-media-job:" + prompt_input_hash(
            workflow_stage_id,
            target_type,
            target_id,
            input_signature,
            job_type,
        )

    @staticmethod
    def _reference_bindings(target: MediaTargetPlan) -> list[Any]:
        prompt_ir = target.prompt_ir_json if isinstance(target.prompt_ir_json, Mapping) else {}
        slots = prompt_ir.get("reference_slots", [])
        if isinstance(slots, list):
            return _copy_json(slots)
        reference_plan = target.provenance_json.get("reference_plan", {})
        if isinstance(reference_plan, Mapping) and isinstance(reference_plan.get("slots"), list):
            return _copy_json(reference_plan["slots"])
        return []

    @classmethod
    def _job_input(
        cls,
        target: MediaTargetPlan,
        *,
        version_id: str,
        shot_id: str,
    ) -> dict[str, Any]:
        payload = to_job_payload(target)
        input_data = dict(payload.get("input_data") or {})
        reference_inputs = {
            "local": list(target.reference_inputs.local),
            "url": list(target.reference_inputs.url),
        }
        provenance = target.provenance_json if isinstance(target.provenance_json, Mapping) else {}
        reference_plan = provenance.get("reference_plan")
        duration = {
            "requested_duration_sec": target.requested_duration_sec,
            "generation_duration_sec": target.generation_duration_sec,
        }
        input_data.update(
            {
                "plan": _copy_json(target.model_dump(mode="json")),
                "prompt": target.prompt_bundle.prompt,
                "negative_prompt": target.prompt_bundle.negative_prompt,
                "reference": reference_inputs,
                "reference_inputs": reference_inputs,
                "reference_files": list(target.reference_inputs.local),
                "reference_urls": list(target.reference_inputs.url),
                "reference_bindings": cls._reference_bindings(target),
                "reference_plan": _copy_json(reference_plan)
                if isinstance(reference_plan, Mapping)
                else {},
                "workflow_stage_input_signature": provenance.get(
                    "upstream_input_signature", ""
                ),
                "workflow_stage_attempt": provenance.get("workflow_stage_attempt"),
                "workflow_run_revision": provenance.get("workflow_run_revision"),
                "path": None,
                "duration": duration,
                "version_id": version_id,
                "shot_id": shot_id,
                "external_key": target.external_key,
            }
        )
        return input_data

    @classmethod
    def _version_values(
        cls,
        target: MediaTargetPlan,
        *,
        version_id: str,
        job_id: str,
        shot_id: str,
        workflow_run_id: str,
        workflow_stage_id: str,
        workflow_run_revision: int | None,
        workflow_stage_attempt: int | None,
        keyframe_version_id: str | None,
    ) -> dict[str, Any]:
        reference_bindings = cls._reference_bindings(target)
        provenance = _copy_json(target.provenance_json)
        if not isinstance(provenance, dict):
            provenance = {}
        provenance.update(
            {
                "queue_service_version": QUEUE_SERVICE_VERSION,
                "workflow_run_id": workflow_run_id,
                "workflow_stage_id": workflow_stage_id,
                "workflow_run_revision": workflow_run_revision,
                "workflow_stage_attempt": workflow_stage_attempt,
                "shot_id": shot_id,
                "external_key": target.external_key,
                "version_id": version_id,
                "job_id": job_id,
                "input_signature": target.input_signature,
                "reference_files": list(target.reference_inputs.local),
                "reference_urls": list(target.reference_inputs.url),
                "reference_bindings": reference_bindings,
                "requested_duration_sec": target.requested_duration_sec,
                "generation_duration_sec": target.generation_duration_sec,
            }
        )
        values: dict[str, Any] = {
            "file_path": None,
            "prompt_snapshot": target.prompt_bundle.prompt,
            "negative_prompt_snapshot": target.prompt_bundle.negative_prompt,
            "prompt_ir_json": _json_text(target.prompt_ir_json),
            "provenance_json": _json_text(provenance),
            "provider_id": target.provider_id,
            "model": target.model,
            "provider_task_id": "",
            "status": "queued",
            "is_current": False,
            "input_signature": target.input_signature,
            "source": "generated",
            "error": "",
        }
        if target.kind == "image":
            values["reference_bindings_json"] = _json_text(reference_bindings)
        else:
            values.update(
                {
                    "keyframe_version_id": keyframe_version_id,
                    "end_frame_path": None,
                    "requested_duration_sec": target.requested_duration_sec,
                    "actual_duration_sec": None,
                    "cost_estimate": 0,
                }
            )
        return values

    def _queue_target(
        self,
        connection: Any,
        run: Mapping[str, Any],
        stage: Mapping[str, Any],
        target: MediaTargetPlan,
        *,
        shot_id: str,
        keyframe_version_id: str | None,
    ) -> _QueuedTarget:
        is_image = target.kind == "image"
        table = keyframe_version if is_image else video_version
        target_type = "keyframe" if is_image else "video"
        job_type = "generate_keyframe" if is_image else "generate_video"
        base_id = (
            f"dynamic-keyframe:{run['id']}:{stage['id']}:{target.external_key}"
            if is_image
            else f"dynamic-video:{run['id']}:{stage['id']}:{target.external_key}"
        )
        existing_version = self._existing_dynamic_version(
            connection,
            table,
            shot_id=shot_id,
            input_signature=target.input_signature,
            workflow_run_id=str(run["id"]),
            workflow_stage_id=str(stage["id"]),
        )
        version_id = (
            str(existing_version["id"])
            if existing_version
            else self._version_id(
                connection,
                table,
                base_id=base_id,
                input_signature=target.input_signature,
            )
        )
        # MediaBridge 已经把 run/stage/target、Prompt IR、参考图和依赖纳入目标
        # 幂等键；Job 直接复用同一个键，避免两个层级出现不同的签名语义。
        idempotency_key = target.idempotency_key
        existing_job = self._existing_job(connection, idempotency_key=idempotency_key)
        job_id = (
            str(existing_job["id"])
            if existing_job
            else self._job_id(
                workflow_stage_id=str(stage["id"]),
                target_type=target_type,
                target_id=version_id,
                input_signature=target.input_signature,
                job_type=job_type,
            )
        )

        if existing_version is None:
            version_no = (
                int(
                    connection.execute(
                        select(func.max(table.c.version_no)).where(table.c.shot_id == shot_id)
                    ).scalar()
                    or 0
                )
                + 1
            )
            values = self._version_values(
                target,
                version_id=version_id,
                job_id=job_id,
                shot_id=shot_id,
                workflow_run_id=str(run["id"]),
                workflow_stage_id=str(stage["id"]),
                workflow_run_revision=(
                    int(run["revision"]) if run.get("revision") is not None else None
                ),
                workflow_stage_attempt=(
                    int(stage["attempt"]) if stage.get("attempt") is not None else None
                ),
                keyframe_version_id=keyframe_version_id,
            )
            record = {
                "id": version_id,
                "shot_id": shot_id,
                "version_no": version_no,
                "created_at": utc_now(),
                **values,
            }
            try:
                with connection.begin_nested():
                    connection.execute(insert(table).values(**record))
            except IntegrityError as exc:
                existing_version = self._existing_dynamic_version(
                    connection,
                    table,
                    shot_id=shot_id,
                    input_signature=target.input_signature,
                    workflow_run_id=str(run["id"]),
                    workflow_stage_id=str(stage["id"]),
                )
                if existing_version is None:
                    raise MediaQueueError(f"动态媒体版本写入失败：{version_id}") from exc
                record = existing_version
                version_id = str(existing_version["id"])
                idempotency_key = target.idempotency_key
                existing_job = self._existing_job(connection, idempotency_key=idempotency_key)
                job_id = (
                    str(existing_job["id"])
                    if existing_job
                    else self._job_id(
                        workflow_stage_id=str(stage["id"]),
                        target_type=target_type,
                        target_id=version_id,
                        input_signature=target.input_signature,
                        job_type=job_type,
                    )
                )
            version_created = existing_version is None
        else:
            record = existing_version
            version_created = False

        input_data = self._job_input(target, version_id=version_id, shot_id=shot_id)
        if existing_job is None:
            now = utc_now()
            job_record = {
                "id": job_id,
                "project_id": run["project_id"],
                "chapter_id": run["chapter_id"],
                "workflow_run_id": run["id"],
                "workflow_stage_id": stage["id"],
                "job_type": job_type,
                "target_type": target_type,
                "target_id": version_id,
                "batch_id": None,
                "idempotency_key": idempotency_key,
                "provider_kind": target.kind,
                "provider_id": target.provider_id,
                "provider_task_id": "",
                "status": "queued",
                "progress_current": 0,
                "progress_total": 1,
                "message": "",
                "input_json": _json_text(input_data),
                "input_signature": target.input_signature,
                "depends_on_json": _json_text(list(target.depends_on)),
                "result_json": "{}",
                "error": "",
                "retry_count": 0,
                "created_at": now,
                "updated_at": now,
                "started_at": None,
                "finished_at": None,
            }
            try:
                with connection.begin_nested():
                    connection.execute(insert(job).values(**job_record))
            except IntegrityError as exc:
                existing_job = self._existing_job(connection, idempotency_key=idempotency_key)
                if existing_job is None:
                    raise MediaQueueError(f"动态媒体 Job 写入失败：{job_id}") from exc
                job_record = existing_job
                job_id = str(existing_job["id"])
            job_created = existing_job is None
        else:
            job_record = existing_job
            job_created = False
            if existing_job.get("status") in {"failed", "interrupted"}:
                now = utc_now()
                connection.execute(
                    update(job)
                    .where(
                        job.c.id == existing_job["id"],
                        job.c.status == existing_job["status"],
                        job.c.input_signature == target.input_signature,
                    )
                    .values(
                        status="queued",
                        error="",
                        message="媒体任务已安全重入队",
                        retry_count=job.c.retry_count + 1,
                        started_at=None,
                        finished_at=None,
                        updated_at=now,
                        depends_on_json=_json_text(list(target.depends_on)),
                        input_json=_json_text(input_data),
                    )
                )
                refreshed = self._existing_job(
                    connection,
                    idempotency_key=idempotency_key,
                )
                if refreshed is not None:
                    job_record = refreshed

        if record.get("id") != version_id:
            record = {
                **record,
                "id": version_id,
            }
        return _QueuedTarget(
            target=target,
            shot_id=shot_id,
            version=record,
            job=job_record,
            version_created=version_created,
            job_created=job_created,
        )

    @staticmethod
    def _target_summary(
        target: MediaTargetPlan,
        *,
        shot_id: str | None,
        version: Mapping[str, Any] | None = None,
        queued_job: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "external_key": target.external_key,
            "shot_id": shot_id,
            "kind": target.kind,
            "status": target.status,
            "input_signature": target.input_signature,
            "idempotency_key": target.idempotency_key,
            "depends_on": list(target.depends_on),
            "provider_id": target.provider_id,
            "provider_model": target.model,
            "prompt": target.prompt_bundle.prompt,
            "negative_prompt": target.prompt_bundle.negative_prompt,
            "prompt_ir_json": _copy_json(target.prompt_ir_json),
            "provenance_json": _copy_json(target.provenance_json),
            "reference_inputs": {
                "local": list(target.reference_inputs.local),
                "url": list(target.reference_inputs.url),
            },
            "requested_duration_sec": target.requested_duration_sec,
            "generation_duration_sec": target.generation_duration_sec,
            "version_id": version.get("id") if version else None,
            "job_id": queued_job.get("id") if queued_job else None,
            "diagnostics": [item.model_dump(mode="json") for item in target.diagnostics],
        }

    @staticmethod
    def _output_ref(
        *,
        run: Mapping[str, Any],
        stage: Mapping[str, Any],
        plan: DynamicMediaBridgePlan,
        targets: list[dict[str, Any]],
        jobs: list[Mapping[str, Any]],
        versions: list[Mapping[str, Any]],
    ) -> dict[str, Any]:
        return {
            "capture_mode": "dynamic_media_queue",
            "queue_service_version": QUEUE_SERVICE_VERSION,
            "queue_signature": prompt_input_hash(
                run["id"],
                stage["id"],
                stage.get("input_signature", ""),
                plan.provider_id,
                plan.model,
                targets,
            ),
            "workflow_run_id": run["id"],
            "workflow_stage_id": stage["id"],
            "stage_key": stage["stage_key"],
            "input_signature": stage.get("input_signature", ""),
            "provider_id": plan.provider_id,
            "provider_model": plan.model,
            "status": plan.status,
            "target_count": len(plan.targets),
            "queued_target_count": sum(item.get("status") == "queued" for item in jobs),
            "version_ids": sorted(str(item["id"]) for item in versions),
            "job_ids": sorted(str(item["id"]) for item in jobs),
            "targets": targets,
            "provenance": {
                "target_provenance": {
                    str(item.external_key): _copy_json(item.provenance_json)
                    for item in plan.targets
                },
                "diagnostics": [item.model_dump(mode="json") for item in plan.diagnostics],
            },
        }

    @staticmethod
    def _append_output_ref(
        existing: Any,
        value: dict[str, Any],
    ) -> list[dict[str, Any]]:
        refs = existing if isinstance(existing, list) else []
        refs = [item for item in refs if isinstance(item, Mapping)]
        result = [dict(_copy_json(item)) for item in refs]
        queue_signature = value["queue_signature"]
        replaced = False
        for index, item in enumerate(result):
            if (
                item.get("capture_mode") == "dynamic_media_queue"
                and item.get("queue_signature") == queue_signature
            ):
                result[index] = value
                replaced = True
                break
        if not replaced:
            result.append(value)
        return result

    def _enqueue_after_commit(
        self,
        job_ids: list[str],
        diagnostics: list[Diagnostic],
    ) -> None:
        enqueue = self.enqueue
        runner = getattr(enqueue, "__self__", None)
        if not callable(enqueue) or bool(getattr(runner, "_shutdown", False)):
            raise MediaQueueError(
                "动态媒体 Runner 未注入，已拒绝创建无法执行的 queued Job"
            )
        for job_id in job_ids:
            try:
                accepted = enqueue(job_id)
            except Exception as exc:  # 回调属于事务外部，不能影响已提交数据。
                diagnostics.append(
                    _diagnostic(
                        "warning",
                        "media_queue_enqueue_failed",
                        f"Job 已提交但外部队列入队失败：{job_id}",
                        path="enqueue",
                        details={"job_id": job_id, "error": str(exc)},
                    )
                )
                continue
            if accepted is False:
                diagnostics.append(
                    _diagnostic(
                        "warning",
                        "media_queue_enqueue_rejected",
                        f"Job 已提交但外部队列拒绝入队：{job_id}",
                        path="enqueue",
                        details={"job_id": job_id},
                    )
                )

    def queue_confirmed_prompt_stage(
        self,
        *,
        workflow_run_id: str,
        workflow_stage_id: str,
        expected_revision: int,
        provider_id: str | None = None,
        provider_model: str | None = None,
    ) -> dict[str, Any]:
        run_id = _required_text(workflow_run_id, "workflow_run_id")
        stage_id = _required_text(workflow_stage_id, "workflow_stage_id")
        runner = getattr(self.enqueue, "__self__", None)
        if not callable(self.enqueue) or bool(getattr(runner, "_shutdown", False)):
            raise MediaQueueError(
                "动态媒体 Runner 未注入，已拒绝创建无法执行的 queued Job"
            )
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            raise MediaQueueError("expected_revision 必须是整数")
        if expected_revision < 1:
            raise MediaQueueError("expected_revision 必须大于等于 1")

        # 事务外先看 draft_output 是否已经写过 provider 快照：
        # 若有则复用，避免与 default_image_provider_id 的并发读发生 TOCTOU。
        snapshot_provider_id = ""
        snapshot_provider_model = ""
        with self.engine.connect() as pre_connection:
            snapshot_row = pre_connection.execute(
                select(
                    workflow_stage.c.draft_output_json,
                ).where(
                    workflow_stage.c.id == stage_id,
                    workflow_stage.c.workflow_run_id == run_id,
                )
            ).first()
        if snapshot_row is not None:
            snapshot_dict = row_dict(snapshot_row) or {}
            try:
                payload = _parse_json_column(
                    snapshot_dict.get("draft_output_json"), "draft_output_json"
                )
            except MediaQueueError:
                payload = {}
            if isinstance(payload, Mapping):
                candidate_id = _text(
                    payload.get("provider_id") or payload.get("default_image_provider_id")
                )
                candidate_model = _text(payload.get("provider_model") or payload.get("model"))
                if candidate_id and not snapshot_provider_id:
                    snapshot_provider_id = candidate_id
                if candidate_model and not snapshot_provider_model:
                    snapshot_provider_model = candidate_model
        if snapshot_provider_id and not _text(provider_id):
            provider_id = snapshot_provider_id
        if snapshot_provider_model and not _text(provider_model):
            provider_model = snapshot_provider_model

        queued_job_ids: list[str] = []
        result: dict[str, Any] | None = None
        callback_diagnostics: list[Diagnostic] = []
        try:
            with self.engine.begin() as connection:
                run = self._run(connection, run_id)
                if run is None:
                    raise MediaQueueError(f"动态运行不存在：{run_id}")
                stage = self._stage(connection, run_id, stage_id)
                if stage is None:
                    raise MediaQueueError(f"动态阶段不存在：{stage_id}")
                stage_key = _text(stage.get("stage_key"))
                if stage_key not in _PROMPT_STAGE_KEYS:
                    raise MediaQueueError(f"阶段 {stage_key!r} 不是可入队的 5a/5b Prompt stage")
                if stage.get("status") != "confirmed":
                    raise MediaQueueError(
                        f"阶段 {stage_key} 尚未确认，当前状态为 {stage.get('status')!r}"
                    )
                actual_revision = int(run.get("revision") or 0)
                if actual_revision != expected_revision:
                    raise MediaQueueError(
                        "运行 revision 冲突："
                        f"expected={expected_revision}, actual={actual_revision}"
                    )

                provider_id, provider_model = self._resolve_provider(
                    connection,
                    run,
                    kind="image" if stage_key == "5a-image-prompts" else "video",
                    provider_id=_text(provider_id),
                    provider_model=_text(provider_model),
                )
                provider_id = _required_text(provider_id, "provider_id")
                provider_model = _required_text(provider_model, "provider_model")

                if not self._provider_exists(
                    connection,
                    provider_id,
                    "image" if stage_key == "5a-image-prompts" else "video",
                ):
                    raise MediaQueueError(f"Provider 不存在或类型不匹配：{provider_id}")

                draft = _parse_json_column(stage.get("draft_output_json"), "draft_output_json")
                normalized = _parse_json_column(
                    stage.get("normalized_output_json"), "normalized_output_json"
                )
                source = draft if _has_stage_content(draft) else normalized
                if not _has_stage_content(source):
                    raise MediaQueueError(
                        f"阶段 {stage_key} 缺少可解析的 draft/normalized PromptStage JSON"
                    )
                prepared = _prepare_stage_payload(
                    source,
                    stage_key=stage_key,
                    workflow_run_id=run_id,
                    workflow_stage_id=stage_id,
                    input_signature=_text(stage.get("input_signature"))
                    or run.get("input_signature", ""),
                )
                stage_output = prepared.output
                keyframe_jobs = (
                    self._keyframe_jobs(
                        connection,
                        run_id,
                        workflow_stage_id=str(
                            connection.scalar(
                                select(workflow_stage.c.id)
                                .where(
                                    workflow_stage.c.workflow_run_id == run_id,
                                    workflow_stage.c.stage_key == "5a-image-prompts",
                                )
                                .order_by(
                                    workflow_stage.c.attempt.desc(),
                                    workflow_stage.c.updated_at.desc(),
                                    workflow_stage.c.id.desc(),
                                )
                                .limit(1)
                            )
                            or ""
                        ),
                    )
                    if stage_key == "5b-video-prompts"
                    else {}
                )
                keyframe_job_ids = {
                    external_key: reference.job_id
                    for external_key, reference in keyframe_jobs.items()
                }
                base_plan = stage_output_to_media_plans(
                    stage_output,
                    provider_id=provider_id,
                    provider_model=provider_model,
                    keyframe_job_ids=keyframe_job_ids,
                )

                # 5b 的连续视频需要前一镜的真实 Job ID。按稳定 stage 顺序逐个构造并
                # 入队目标，避免把 idempotency key 当作数据库 Job ID。
                plans: list[MediaTargetPlan] = []
                target_summaries: list[dict[str, Any]] = []
                queued_targets: list[_QueuedTarget] = []
                versions: list[Mapping[str, Any]] = []
                jobs: list[Mapping[str, Any]] = []
                target_entries = list(zip(prepared.bundle_keys, base_plan.targets, strict=False))
                previous_video_job_id: str | None = None
                for _index, (bundle_key, base_target) in enumerate(target_entries):
                    target = base_target
                    if stage_key == "5b-video-prompts":
                        bundle = stage_output.bundles[bundle_key]
                        metadata = bundle.provenance
                        continuity = metadata.get("continuity", {})
                        previous_end_frame_path = (
                            _text(continuity.get("previous_end_frame_path"))
                            if isinstance(continuity, Mapping)
                            else ""
                        )
                        reference = keyframe_jobs.get(base_target.external_key)
                        try:
                            target = build_video_media_plan(
                                bundle,
                                workflow_run_id=run_id,
                                workflow_stage_id=stage_id,
                                shot_id=base_target.shot_id,
                                external_key=base_target.external_key,
                                input_signature=_text(stage.get("input_signature"))
                                or run.get("input_signature", ""),
                                provider_id=provider_id,
                                provider_model=provider_model,
                                reference_plan=metadata.get("reference_plan"),
                                keyframe_job_id=reference.job_id if reference else None,
                                previous_video_job_id=previous_video_job_id,
                                previous_end_frame_path=previous_end_frame_path or None,
                            )
                        except (PromptStageError, TypeError, ValueError) as exc:
                            raise MediaQueueError(
                                f"媒体目标 {base_target.external_key} 计划生成失败"
                            ) from exc
                    mapping = self._shot_mapping(connection, run, target)
                    target_diagnostics = [*target.diagnostics, *mapping.diagnostics]
                    duplicate_stage_keys = [
                        item.external_key
                        for item in base_plan.targets
                        if item.external_key == target.external_key
                    ]
                    if len(duplicate_stage_keys) > 1:
                        target_diagnostics.append(
                            _diagnostic(
                                "blocking",
                                "media_queue_duplicate_stage_external_key",
                                f"PromptStage 中 external_key={target.external_key!r} 重复",
                                path="bundles",
                                details={"external_key": target.external_key},
                            )
                        )
                    if stage_output.status == "blocked":
                        target_diagnostics.append(
                            _diagnostic(
                                "blocking",
                                "media_queue_prompt_stage_blocked",
                                f"PromptStage {stage_key} 已被阻断，不能入队媒体目标",
                                path=f"bundles.{target.external_key}",
                            )
                        )
                    if target_diagnostics:
                        target = _target_with_diagnostics(target, target_diagnostics)
                    plans.append(target)

                    mapping = self._shot_mapping(connection, run, target)
                    if target.status not in _QUEUEABLE_TARGET_STATUSES or mapping.shot_id is None:
                        target_summaries.append(
                            self._target_summary(
                                target,
                                shot_id=mapping.shot_id,
                            )
                        )
                        continue
                    keyframe_version_id: str | None = None
                    if target.kind == "video":
                        keyframe_reference = keyframe_jobs.get(target.external_key)
                        if keyframe_reference is None:
                            blocked = _target_with_diagnostics(
                                target,
                                [
                                    *target.diagnostics,
                                    _diagnostic(
                                        "blocking",
                                        "media_queue_keyframe_job_missing",
                                        f"视频目标 {target.external_key} 缺少关键帧 Job 依赖",
                                        path="depends_on",
                                    ),
                                ],
                            )
                            target_summaries.append(
                                self._target_summary(blocked, shot_id=mapping.shot_id)
                            )
                            plans[-1] = blocked
                            continue
                        keyframe_version_id = keyframe_reference.version_id
                    queued_target = self._queue_target(
                        connection,
                        run,
                        stage,
                        target,
                        shot_id=mapping.shot_id,
                        keyframe_version_id=keyframe_version_id,
                    )
                    queued_targets.append(queued_target)
                    versions.append(queued_target.version)
                    jobs.append(queued_target.job)
                    if queued_target.job.get("status") == "queued":
                        queued_job_ids.append(str(queued_target.job["id"]))
                    target_summaries.append(
                        self._target_summary(
                            target,
                            shot_id=mapping.shot_id,
                            version=queued_target.version,
                            queued_job=queued_target.job,
                        )
                    )
                    if stage_key == "5b-video-prompts":
                        previous_video_job_id = str(queued_target.job["id"])

                # base_plan 仅用于确定 bundle/target 顺序；其中 5b 的初次桥接没有
                # 上一镜真实 Job，产生的临时连续性诊断不能污染重建后的最终计划。
                stage_diagnostics = list(stage_output.diagnostics)
                if stage_output.status == "warning" and not stage_diagnostics:
                    stage_diagnostics.append(
                        _diagnostic(
                            "warning",
                            "media_queue_prompt_stage_warning",
                            f"PromptStage {stage_key} 带有警告",
                            path="stage",
                        )
                    )
                final_plan = _plan_with_targets(
                    base_plan.model_copy(deep=True, update={"diagnostics": stage_diagnostics}),
                    plans,
                )

                # 5b 某个视频因缺少关键帧或连续性而阻断时，不会污染后续的依赖链。
                # output_refs 仍然完整保存诊断和可入队目标，方便后续补偿。
                queue_ref = self._output_ref(
                    run=run,
                    stage=stage,
                    plan=final_plan,
                    targets=target_summaries,
                    jobs=jobs,
                    versions=versions,
                )
                output_refs = _parse_json_column(stage.get("output_refs_json"), "output_refs_json")
                if not isinstance(output_refs, list):
                    output_refs = []
                output_refs = self._append_output_ref(output_refs, queue_ref)
                now = utc_now()
                stage_update = connection.execute(
                    update(workflow_stage)
                    .where(
                        workflow_stage.c.id == stage_id,
                        workflow_stage.c.workflow_run_id == run_id,
                        workflow_stage.c.status == "confirmed",
                    )
                    .values(output_refs_json=json_dumps(output_refs), updated_at=now)
                )
                if stage_update.rowcount != 1:
                    raise MediaQueueError("确认后的 Prompt stage 状态已发生变化")
                run_update = connection.execute(
                    update(workflow_run)
                    .where(
                        workflow_run.c.id == run_id,
                        workflow_run.c.revision == expected_revision,
                    )
                    .values(
                        revision=workflow_run.c.revision + 1,
                        updated_at=now,
                    )
                )
                if run_update.rowcount != 1:
                    raise MediaQueueError(f"运行 revision 冲突：expected={expected_revision}")
                new_revision = expected_revision + 1
                result = {
                    "stage_id": stage_id,
                    "stage_key": stage_key,
                    "status": final_plan.status,
                    "targets": target_summaries,
                    "jobs": [
                        {
                            "id": str(item["id"]),
                            "project_id": item["project_id"],
                            "chapter_id": item["chapter_id"],
                            "workflow_run_id": item["workflow_run_id"],
                            "workflow_stage_id": item["workflow_stage_id"],
                            "job_type": item["job_type"],
                            "target_type": item["target_type"],
                            "target_id": item["target_id"],
                            "status": item["status"],
                            "provider_kind": item["provider_kind"],
                            "provider_id": item["provider_id"],
                            "input_signature": item["input_signature"],
                            "depends_on": _json_column(item.get("depends_on_json"), []),
                            "idempotency_key": item["idempotency_key"],
                            "input_data": _json_column(item.get("input_json"), {}),
                        }
                        for item in jobs
                    ],
                    "versions": [
                        {
                            "id": str(item["id"]),
                            "kind": "keyframe" if stage_key == "5a-image-prompts" else "video",
                            "shot_id": item["shot_id"],
                            "version_no": item["version_no"],
                            "status": item["status"],
                            "input_signature": item["input_signature"],
                            "source": item["source"],
                            "prompt_snapshot": item["prompt_snapshot"],
                            "negative_prompt_snapshot": item["negative_prompt_snapshot"],
                            "prompt_ir_json": _json_column(item.get("prompt_ir_json"), {}),
                            "provenance_json": _json_column(item.get("provenance_json"), {}),
                            "reference_bindings": _json_column(
                                item.get("reference_bindings_json"), []
                            ),
                            "requested_duration_sec": item.get("requested_duration_sec"),
                            "generation_duration_sec": _json_column(
                                item.get("provenance_json"), {}
                            ).get("generation_duration_sec")
                            if isinstance(_json_column(item.get("provenance_json"), {}), Mapping)
                            else None,
                        }
                        for item in versions
                    ],
                    "diagnostics": [
                        item.model_dump(mode="json") for item in final_plan.diagnostics
                    ],
                    "revision": new_revision,
                }
        except MediaQueueError:
            raise
        except (SQLAlchemyError, ValueError, TypeError) as exc:
            raise MediaQueueError(f"动态媒体入队失败：{exc}") from exc

        assert result is not None
        self._enqueue_after_commit(queued_job_ids, callback_diagnostics)
        if callback_diagnostics:
            result["diagnostics"] = [
                *result["diagnostics"],
                *[item.model_dump(mode="json") for item in callback_diagnostics],
            ]
            result["diagnostics"] = [
                item.model_dump(mode="json")
                for item in diagnostics_status(
                    [Diagnostic.model_validate(item) for item in result["diagnostics"]]
                ).diagnostics
            ]
        return result


__all__ = [
    "DynamicMediaQueueService",
    "MediaQueueError",
    "QUEUE_SERVICE_VERSION",
]
