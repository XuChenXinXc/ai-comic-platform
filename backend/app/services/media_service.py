import asyncio
import base64
import io
import logging
import os
import re
import subprocess
import time
import zipfile
from collections.abc import Mapping, Sequence
from email.utils import parsedate_to_datetime
from pathlib import Path
from threading import RLock
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit

from PIL import Image, UnidentifiedImageError
from sqlalchemy import delete, exists, func, insert, or_, select, update

from app.config import settings
from app.db import Database, db
from app.dynamic_pipeline.h3_adapter import (
    Ref2VATextOutput,
    build_ref2va_rewrite_request,
    extract_dialogue_lines,
    validate_ref2va_sections,
)
from app.models.tables import (
    api_provider,
    asset,
    asset_image,
    chapter,
    job,
    keyframe_version,
    project,
    scene_view,
    shot,
    video_version,
    workflow_run,
    workflow_stage,
)
from app.schemas import ProviderPollResult
from app.services.constants import MAX_IMAGE_SIZE, MAX_VIDEO_SIZE
from app.services.director_plan import (
    director_bundle_from_llm,
    validate_shot_consistency,
)
from app.services.errors import ServiceError, bad_request, conflict, not_found
from app.services.ffmpeg_utils import run_in_ffmpeg_executor
from app.services.file_service import (
    FileServiceError,
    atomic_copy,
    download_url_atomic,
    file_service,
    project_media_url,
)
from app.services.job_service import JobService
from app.services.natural_video_prompt import (
    parse_reference_anchors,
    validate_prompt_references,
)
from app.services.prompt_service import (
    VISUAL_PROMPT_TEMPLATE_VERSION,
    compile_asset_prompt,
    compile_keyframe_prompt,
    compile_video_prompt,
    effective_video_duration,
    join_non_empty,
    resolve_asset_negative_prompt,
    resolve_video_description,
    resolve_visual_aspect_ratio,
    shot_template_context,
)
from app.services.provider_service import ProviderService
from app.services.shot_service import ShotService
from app.services.signature_service import (
    asset_image_signature,
    calculate_signature,
    is_stale,
    keyframe_signature,
    video_signature,
)
from app.services.utils import (
    json_dumps,
    json_loads,
    new_id,
    row_dict,
    sanitize_error_message,
    utc_now,
)

logger = logging.getLogger(__name__)

MediaKind = Literal["asset_image", "keyframe", "video"]


class JobCancelledError(RuntimeError):
    """任务被用户取消，媒体处理器应尽快中止轮询。"""


class MediaDependencyError(ServiceError):
    """媒体任务的前置依赖不存在或未成功完成。"""

    def __init__(self, detail: str) -> None:
        super().__init__(409, detail)


class ProviderPollTimeoutError(RuntimeError):
    """Provider 轮询超时：上游任务可能仍在生成，不应判定为最终失败。"""


_MEDIA_TABLES = {
    "asset_image": (asset_image, "asset_id"),
    "keyframe": (keyframe_version, "shot_id"),
    "video": (video_version, "shot_id"),
}
_DELETED_MEDIA_ERROR = "__deleted_by_user__"
_ACTIVE_MEDIA_STATUSES = {"queued", "running", "waiting_provider", "downloading"}
_TERMINAL_JOB_STATUSES = {"completed", "failed", "cancelled", "interrupted"}
_DYNAMIC_PROTECTED_STATUSES = {
    "blocked",
    "cancelled",
    "failed",
    "interrupted",
    "partial_failed",
    "stale",
}
_MEDIA_VERSION_FILE_LOCK = RLock()
_GPT_IMAGE_2_SIZES = {
    # apib 按分辨率档位出图：这些像素尺寸只作为比例提示，
    # resolution=2k 时实际输出 2048 宽（3:2 / 16:9 保持）。
    # 实测 ai.rjk66.cn（OpenAI 兼容 relay）：请求 2560x1707 实际返回
    # 1536x1024、请求 2560x1440 实际返回 1672x941，即大尺寸会被静默缩图；
    # 只有 2048x1360 / 2048x1152 会按原尺寸输出真 2K，因此统一用这两个值。
    "character": "2048x1360",
    "scene": "2048x1152",
    "prop": "2048x1360",
    "keyframe": "2048x1152",
}
_ASSET_VISUAL_PROFILE_FIELDS = {
    "character": (
        "appearance",
        "face",
        "hair",
        "eyes",
        "body",
        "costume",
        "immutable_features",
    ),
    "scene": (
        "layout",
        "architecture",
        "space_keywords",
        "regions",
        "furnishings",
        "main_view",
    ),
    "prop": (
        "description",
        "silhouette",
        "front_structure",
        "material",
        "special_features",
    ),
}


class MediaService:
    def __init__(
        self,
        database: Database | None = None,
        jobs: JobService | None = None,
    ) -> None:
        self.database = database or db
        self.jobs = jobs or JobService(self.database)
        self.providers = ProviderService(self.database)

    @staticmethod
    def _image_generation_params(
        model: str,
        target_type: str,
        params: dict[str, Any],
        *,
        provider_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = dict(params)
        if model.strip().lower() == "gpt-image-2":
            size = _GPT_IMAGE_2_SIZES.get(target_type)
            if size:
                result["size"] = (
                    size
                    if (
                        MediaService._provider_supports_gpt_image_2_2k(provider_config)
                        or target_type == "keyframe"
                    )
                    else "1536x1024"
                )
        return result

    @staticmethod
    def _provider_uses_apib(provider_config: dict[str, Any] | None) -> bool:
        if not provider_config:
            return False
        base_url = str(provider_config.get("base_url") or "").lower()
        protocol = str((provider_config.get("params") or {}).get("protocol") or "").lower()
        return protocol in {"apib", "apimart"} or "apib.ai" in base_url or "apimart.ai" in base_url

    @staticmethod
    def _provider_supports_gpt_image_2_2k(provider_config: dict[str, Any] | None) -> bool:
        """天悦 gpt-image-2 与 apib 一样支持 2K 档，按域名/协议识别。"""
        if not provider_config:
            return False
        if MediaService._provider_uses_apib(provider_config):
            return True
        base_url = str(provider_config.get("base_url") or "").lower()
        protocol = str((provider_config.get("params") or {}).get("protocol") or "").lower()
        return (
            protocol == "tianyue"
            or "api.tianyue.xyz" in base_url
            or "tianyue.xyz" in base_url
        )

    @staticmethod
    def _supports_multiple_video_inputs(provider: Any) -> bool:
        capability = getattr(provider, "supports_multiple_input_images", False)
        if callable(capability):
            capability = capability()
        return bool(capability) and callable(getattr(provider, "prepare_input_images", None))

    @staticmethod
    def _effective_video_duration(model: str, duration_sec: float) -> float | int:
        return effective_video_duration(model, duration_sec)

    @staticmethod
    def _has_visual_value(value: Any) -> bool:
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, dict):
            return any(MediaService._has_visual_value(item) for item in value.values())
        if isinstance(value, list):
            return any(MediaService._has_visual_value(item) for item in value)
        return value is not None

    @classmethod
    def _asset_has_generation_content(cls, current: dict[str, Any]) -> bool:
        if cls._has_visual_value(current.get("prompt")):
            return True
        profile = current.get("profile", {})
        if not isinstance(profile, dict):
            return False
        return any(
            cls._has_visual_value(profile.get(field_name))
            for field_name in _ASSET_VISUAL_PROFILE_FIELDS.get(current["asset_type"], ())
        )

    def _project(self, project_id: str) -> dict[str, Any]:
        with self.database.engine.connect() as connection:
            record = row_dict(
                connection.execute(select(project).where(project.c.id == project_id)).first()
            )
        if record is None:
            raise not_found("项目")
        return record

    def _project_storage_name(self, project_id: str) -> str:
        current = self._project(project_id)
        return str(current.get("storage_name") or current["name"])

    def _asset(self, asset_id: str) -> dict[str, Any]:
        with self.database.engine.connect() as connection:
            record = row_dict(
                connection.execute(select(asset).where(asset.c.id == asset_id)).first()
            )
        if record is None:
            raise not_found("素材")
        record["profile"] = json_loads(record["profile_json"], {})
        record["consistency_keywords"] = json_loads(record["consistency_json"], [])
        return record

    def _shot(self, shot_id: str) -> dict[str, Any]:
        with self.database.engine.connect() as connection:
            record = row_dict(connection.execute(select(shot).where(shot.c.id == shot_id)).first())
        if record is None:
            raise not_found("分镜")
        for column in (
            "character_ids_json",
            "prop_ids_json",
            "positions_json",
            "expressions_json",
            "dialogue_json",
            "reference_bindings_json",
        ):
            record[column.removesuffix("_json")] = json_loads(record[column], [])
        record["director_plan"] = json_loads(record.get("director_plan_json") or "{}", {})
        return record

    def _chapter_folder_info(self, chapter_id: str | None) -> tuple[int | None, str]:
        """分镜媒体按章节分目录，这里取章号与标题用于拼路径。"""
        if not chapter_id:
            return None, ""
        with self.database.engine.connect() as connection:
            row = connection.execute(
                select(chapter.c.chapter_no, chapter.c.title).where(chapter.c.id == chapter_id)
            ).first()
        if row is None:
            return None, ""
        return int(row.chapter_no), str(row.title or "")

    def _chapter_project_id(self, chapter_id: str) -> str:
        """章节所属项目 ID，不存在时抛 404。"""
        with self.database.engine.connect() as connection:
            project_id = connection.execute(
                select(chapter.c.project_id).where(chapter.c.id == chapter_id)
            ).scalar()
        if not project_id:
            raise not_found("章节")
        return str(project_id)

    @staticmethod
    def _reference_role_label(role: str, name: str) -> str:
        labels = {
            "character_identity": "锁定角色身份与服装",
            "scene_layout": "锁定场景空间结构和光线",
            "scene_master": "锁定场景母版空间结构",
            "prop_identity": "锁定物品外观、材质和结构",
            "previous_keyframe": "锁定上一镜构图与连续性",
        }
        return f"{name}，{labels.get(role, '锁定当前参考信息')}"

    @staticmethod
    def _readable_project_file(project_id: str, relative_path: str) -> Path | None:
        """项目内相对路径可读时返回绝对路径，否则返回 None（不抛异常）。"""
        try:
            path = file_service.resolve_project_path(project_id, relative_path)
        except (FileServiceError, OSError, ValueError):
            return None
        try:
            if path.is_file() and path.stat().st_size > 0:
                return path
        except OSError:
            return None
        return None

    @staticmethod
    def _is_dynamic_media_input(input_data: Mapping[str, Any]) -> bool:
        return bool(
            input_data.get("workflow_run_id")
            and input_data.get("workflow_stage_id")
            and (
                input_data.get("plan")
                or input_data.get("reference_plan")
                or input_data.get("reference_inputs")
            )
        )

    @staticmethod
    def _dynamic_reference_plan(input_data: Mapping[str, Any]) -> dict[str, Any]:
        direct = input_data.get("reference_plan")
        if isinstance(direct, Mapping):
            return dict(direct)
        plan = input_data.get("plan")
        if isinstance(plan, Mapping):
            provenance = plan.get("provenance_json") or plan.get("provenance")
            if isinstance(provenance, Mapping):
                reference_plan = provenance.get("reference_plan")
                if isinstance(reference_plan, Mapping):
                    return dict(reference_plan)
        provenance = input_data.get("provenance_json") or input_data.get("provenance")
        if isinstance(provenance, Mapping) and isinstance(
            provenance.get("reference_plan"), Mapping
        ):
            return dict(provenance["reference_plan"])
        return {}

    @classmethod
    def _dynamic_reference_sources(
        cls,
        input_data: Mapping[str, Any],
    ) -> list[tuple[str, str]]:
        """按 ReferencePlan index 保留本地文件和 URL 的混合顺序。"""
        reference_plan = cls._dynamic_reference_plan(input_data)
        raw_bindings = input_data.get("reference_bindings")
        slots = reference_plan.get("slots")
        if not isinstance(slots, list):
            slots = raw_bindings if isinstance(raw_bindings, list) else []
        mapping = reference_plan.get("text_mapping")
        mapping = mapping if isinstance(mapping, Mapping) else {}

        result: list[tuple[str, str]] = []
        consumed_local: set[str] = set()
        consumed_url: set[str] = set()
        ordered_slots = sorted(
            (item for item in slots if isinstance(item, Mapping)),
            key=lambda item: int(item.get("index", 0) or 0),
        )
        for slot in ordered_slots:
            index = int(slot.get("index", 0) or 0)
            mapped = mapping.get(f"@Image{index}", {})
            mapped = mapped if isinstance(mapped, Mapping) else {}
            source = str(mapped.get("source") or "").strip().casefold()
            local = str(
                mapped.get("local_file")
                or mapped.get("local")
                or mapped.get("path")
                or (slot.get("relative_path") or "")
            ).strip()
            url = str(
                mapped.get("url")
                or (slot.get("provider_url") or "")
            ).strip()
            if source == "url" and url:
                result.append(("url", url))
                consumed_url.add(url)
            elif source == "local" and local:
                result.append(("local", local))
                consumed_local.add(local)
            elif local:
                result.append(("local", local))
                consumed_local.add(local)
            elif url:
                result.append(("url", url))
                consumed_url.add(url)

        raw_inputs = input_data.get("reference_inputs")
        if isinstance(raw_inputs, Mapping):
            local_values = raw_inputs.get("local", [])
            url_values = raw_inputs.get("url", [])
        else:
            local_values = input_data.get("reference_files", [])
            url_values = input_data.get("reference_urls", [])
        for value in local_values if isinstance(local_values, list) else []:
            text = str(value or "").strip()
            if text and text not in consumed_local:
                result.append(("local", text))
                consumed_local.add(text)
        for value in url_values if isinstance(url_values, list) else []:
            text = str(value or "").strip()
            if text and text not in consumed_url:
                result.append(("url", text))
                consumed_url.add(text)
        return result

    @staticmethod
    def _dynamic_local_path(project_id: str, value: str) -> Path | None:
        candidate = Path(value)
        if not candidate.is_absolute():
            return MediaService._readable_project_file(project_id, value)
        try:
            root = file_service.project_path(project_id).resolve()
            resolved = candidate.resolve()
            resolved.relative_to(root)
            if resolved.is_file() and resolved.stat().st_size > 0:
                return resolved
        except (FileServiceError, OSError, ValueError):
            return None
        return None

    async def _dynamic_reference_files(
        self,
        project_id: str,
        job_id: str,
        input_data: Mapping[str, Any],
    ) -> tuple[list[Path], list[str]]:
        """将动态 ReferencePlan 的本地路径和 URL 转为有序的本地输入文件。"""
        sources = self._dynamic_reference_sources(input_data)
        paths: list[Path] = []
        temporary: list[str] = []
        for index, (kind, value) in enumerate(sources, start=1):
            if kind == "local":
                path = self._dynamic_local_path(project_id, value)
                if path is None:
                    raise conflict(f"动态参考图文件不存在或为空：{value}")
                paths.append(path)
                continue
            if not self._provider_url_usable(value):
                raise conflict(f"动态参考图 URL 无效或已过期：{value}")
            relative = (
                Path("temp")
                / "dynamic-references"
                / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', job_id)}-{index}.img"
            ).as_posix()
            try:
                target = await asyncio.to_thread(
                    download_url_atomic,
                    value,
                    file_service.resolve_project_path(project_id, relative),
                    timeout=120,
                )
            except (FileServiceError, OSError) as exc:
                raise conflict(f"动态参考图 URL 下载失败：{value}") from exc
            if not target.is_file() or target.stat().st_size <= 0:
                raise conflict(f"动态参考图 URL 下载后为空：{value}")
            paths.append(target)
            temporary.append(relative)
        return paths, temporary

    @staticmethod
    def _cleanup_dynamic_reference_files(project_id: str, paths: list[str]) -> None:
        for relative in paths:
            try:
                file_service.delete_file(project_id, relative)
            except (FileServiceError, OSError) as exc:
                # 不抛错导致任务失败：临时文件残留可以下次清理，
                # 但要日志可见，便于巡检 /workspace/projects/<id>/temp/dynamic-references。
                logger.warning(
                    "动态参考图临时文件清理失败：project=%s path=%s error=%s",
                    project_id,
                    relative,
                    sanitize_error_message(exc),
                )

    @staticmethod
    def _prompt_reference_count(prompt: str) -> int:
        indexes = [int(value) for value in re.findall(r"@Image(\d+)", prompt or "")]
        return max(indexes, default=0)

    @staticmethod
    def _safe_storage_token(value: Any) -> str:
        raw = str(value or "")
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", raw) or "media"
        return safe if len(safe) <= 80 else f"media-{calculate_signature(raw)[:40]}"

    async def _wait_for_job_dependencies(self, job_record: Mapping[str, Any]) -> None:
        raw_dependencies = job_record.get("depends_on")
        if not isinstance(raw_dependencies, list):
            input_data = job_record.get("input_data")
            raw_dependencies = (
                input_data.get("depends_on", [])
                if isinstance(input_data, Mapping)
                else []
            )
        dependencies = [str(item).strip() for item in raw_dependencies if str(item).strip()]
        if not dependencies:
            return
        async with asyncio.timeout(120):
            while True:
                pending = False
                for dependency_id in dependencies:
                    dependency = self.jobs.repository.get(dependency_id)
                    if dependency is None:
                        raise MediaDependencyError(f"媒体任务依赖不存在：{dependency_id}")
                    status = str(dependency.get("status") or "")
                    if status == "completed":
                        continue
                    if status in _TERMINAL_JOB_STATUSES:
                        raise MediaDependencyError(
                            f"媒体任务依赖未成功完成：{dependency_id}（{status}）"
                        )
                    pending = True
                if not pending:
                    return
                await asyncio.sleep(settings.provider_poll_interval_sec)

    def _record_dynamic_media_failure(
        self,
        job_record: Mapping[str, Any],
        input_data: Mapping[str, Any],
        error: Any,
    ) -> None:
        run_id = str(job_record.get("workflow_run_id") or input_data.get("workflow_run_id") or "")
        stage_id = str(
            job_record.get("workflow_stage_id") or input_data.get("workflow_stage_id") or ""
        )
        if not run_id or not stage_id:
            return
        message = sanitize_error_message(error) or "动态媒体任务失败"
        job_id = str(job_record.get("id") or "")
        try:
            with self.database.engine.begin() as connection:
                current_job = row_dict(
                    connection.execute(select(job).where(job.c.id == job_id)).first()
                )
                run = row_dict(
                    connection.execute(
                        select(workflow_run).where(workflow_run.c.id == run_id)
                    ).first()
                )
                stage = row_dict(
                    connection.execute(
                        select(workflow_stage).where(
                            workflow_stage.c.id == stage_id,
                            workflow_stage.c.workflow_run_id == run_id,
                        )
                    ).first()
                )
                if (
                    current_job is None
                    or run is None
                    or stage is None
                    or current_job.get("status") not in _ACTIVE_MEDIA_STATUSES
                    or str(current_job.get("input_signature") or "")
                    != str(job_record.get("input_signature") or "")
                ):
                    return
                latest = row_dict(
                    connection.execute(
                        select(workflow_stage)
                        .where(
                            workflow_stage.c.workflow_run_id == run_id,
                            workflow_stage.c.stage_key == stage.get("stage_key"),
                        )
                        .order_by(
                            workflow_stage.c.attempt.desc(),
                            workflow_stage.c.updated_at.desc(),
                            workflow_stage.c.id.desc(),
                        )
                        .limit(1)
                    ).first())
                expected_signature = str(
                    input_data.get("workflow_stage_input_signature")
                    or json_loads(
                        stage.get("provenance_json") if stage else None,
                        {},
                    ).get("upstream_input_signature", "")
                )
                if (
                    latest is None
                    or latest.get("id") != stage_id
                    or (
                        expected_signature
                        and str(stage.get("input_signature") or "") != expected_signature
                    )
                    or str(run.get("status")) in {"cancelled", "stale", "blocked"}
                    or str(stage.get("status")) in {"cancelled", "stale", "blocked"}
                ):
                    return
                now = utc_now()
                failure_ref = {
                    "capture_mode": "dynamic_media_failure",
                    "job_id": job_id,
                    "input_signature": current_job.get("input_signature") or "",
                    "error": message,
                }
                output_refs = json_loads(stage.get("output_refs_json"), [])
                output_refs = output_refs if isinstance(output_refs, list) else []
                output_refs.append(failure_ref)
                stage_result = connection.execute(
                    update(workflow_stage)
                    .where(
                        workflow_stage.c.id == stage_id,
                        workflow_stage.c.workflow_run_id == run_id,
                        workflow_stage.c.attempt == latest.get("attempt"),
                        workflow_stage.c.input_signature == stage.get("input_signature"),
                        workflow_stage.c.updated_at == stage.get("updated_at"),
                        workflow_stage.c.status.not_in(("cancelled", "stale", "blocked")),
                    )
                    .values(
                        status="partial_failed",
                        error=f"媒体任务 {job_id} 失败：{message}",
                        output_refs_json=json_dumps(output_refs),
                        finished_at=now,
                        updated_at=now,
                    )
                )
                if stage_result.rowcount != 1:
                    return
                run_result = connection.execute(
                    update(workflow_run)
                    .where(
                        workflow_run.c.id == run_id,
                        workflow_run.c.revision == run.get("revision"),
                        workflow_run.c.updated_at == run.get("updated_at"),
                        workflow_run.c.status.not_in(("cancelled", "stale", "blocked")),
                    )
                    .values(
                        status="partial_failed",
                        error=f"动态媒体任务 {job_id} 失败：{message}",
                        revision=workflow_run.c.revision + 1,
                        updated_at=now,
                    )
                )
                if run_result.rowcount != 1:
                    return
        except Exception:
            # 媒体 Job 的原始失败仍由 Runner 记录；状态汇总失败不能掩盖原始错误。
            return

    def _enqueue_dynamic_dependents(self, completed_job_id: str) -> None:
        runner = getattr(self.jobs, "runner", None)
        enqueue = getattr(runner, "enqueue", None)
        if not callable(enqueue):
            return
        try:
            with self.database.engine.connect() as connection:
                rows = connection.execute(
                    select(job).where(
                        job.c.workflow_run_id.is_not(None),
                        job.c.status.in_(tuple(_ACTIVE_MEDIA_STATUSES | {"queued"})),
                    )
                ).all()
            for row in rows:
                record = row_dict(row) or {}
                dependencies = json_loads(record.get("depends_on_json"), [])
                if not isinstance(dependencies, list) or completed_job_id not in dependencies:
                    continue
                all_completed = True
                with self.database.engine.connect() as connection:
                    for dependency_id in dependencies:
                        status = (
                            "completed"
                            if str(dependency_id) == completed_job_id
                            else connection.scalar(
                                select(job.c.status).where(job.c.id == str(dependency_id))
                            )
                        )
                        if status != "completed":
                            all_completed = False
                            break
                if all_completed and record.get("status") == "queued":
                    try:
                        enqueue(str(record["id"]))
                    except Exception:
                        # 入队失败不掩盖主流程；记录告警后交给恢复流程重新入队。
                        logger.warning(
                            "动态媒体任务 %s 入队失败，等待恢复流程重新入队",
                            record["id"],
                            exc_info=True,
                        )
        except Exception:
            return

    def _complete_dynamic_media_job(
        self,
        job_record: Mapping[str, Any],
        input_data: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> bool:
        if not self._is_dynamic_media_input(input_data):
            return False
        job_id = str(job_record.get("id") or "")
        if not job_id:
            return False
        try:
            with self.database.engine.begin() as connection:
                updated = connection.execute(
                    update(job)
                    .where(
                        job.c.id == job_id,
                        job.c.status.in_(tuple(_ACTIVE_MEDIA_STATUSES)),
                        job.c.input_signature == job_record.get("input_signature"),
                    )
                    .values(
                        status="completed",
                        progress_current=job.c.progress_total,
                        message="任务完成",
                        error="",
                        result_json=json_dumps(dict(result)),
                        finished_at=utc_now(),
                        updated_at=utc_now(),
                    )
                )
            return updated.rowcount == 1
        except Exception:
            return False

    def _validate_dynamic_media_state(
        self,
        job_record: Mapping[str, Any],
        input_data: Mapping[str, Any],
        version: Mapping[str, Any],
    ) -> None:
        run_id = str(job_record.get("workflow_run_id") or input_data.get("workflow_run_id") or "")
        stage_id = str(
            job_record.get("workflow_stage_id") or input_data.get("workflow_stage_id") or ""
        )
        if not run_id and not stage_id:
            return
        if not run_id or not stage_id:
            raise conflict("动态媒体任务缺少完整的 run/stage 关联")
        provenance = json_loads(version.get("provenance_json"), {})
        expected_signature = str(
            input_data.get("workflow_stage_input_signature")
            or (
                provenance.get("upstream_input_signature", "")
                if isinstance(provenance, dict)
                else ""
            )
            or ""
        )
        with self.database.engine.connect() as connection:
            run = connection.execute(
                select(workflow_run).where(workflow_run.c.id == run_id)
            ).first()
            stage = connection.execute(
                select(workflow_stage).where(
                    workflow_stage.c.id == stage_id,
                    workflow_stage.c.workflow_run_id == run_id,
                )
            ).first()
            latest_stage = None
            if stage is not None:
                latest_stage = connection.execute(
                    select(workflow_stage)
                    .where(
                        workflow_stage.c.workflow_run_id == run_id,
                        workflow_stage.c.stage_key == stage.stage_key,
                    )
                    .order_by(
                        workflow_stage.c.attempt.desc(),
                        workflow_stage.c.updated_at.desc(),
                        workflow_stage.c.id.desc(),
                    )
                    .limit(1)
                ).first()
            current_job = connection.execute(
                select(
                    job.c.input_signature,
                    job.c.workflow_run_id,
                    job.c.workflow_stage_id,
                    job.c.status,
                ).where(job.c.id == job_record.get("id"))
            ).first()
        if run is None or stage is None or latest_stage is None:
            raise conflict("动态媒体任务关联的 run/stage 不存在")
        if str(latest_stage.id) != stage_id:
            raise conflict("动态媒体任务关联的 stage attempt 已过期，拒绝覆盖新结果")
        if current_job is None or str(current_job.workflow_stage_id or "") != stage_id:
            raise conflict("动态媒体任务关联的 Job 已切换到新的 stage，拒绝覆盖新结果")
        if str(current_job.workflow_run_id or "") != run_id:
            raise conflict("动态媒体任务关联的 Job 已切换到新的 run，拒绝覆盖新结果")
        if str(current_job.status or "") not in _ACTIVE_MEDIA_STATUSES:
            raise conflict("动态媒体 Job 已不处于可提交状态，拒绝激活旧结果")
        job_signature = str(current_job.input_signature or input_data.get("input_signature") or "")
        version_signature = str(version.get("input_signature") or "")
        if job_signature and str(
            job_record.get("input_signature") or job_signature
        ) != job_signature:
            raise conflict("动态媒体 Job input_signature 已变化，拒绝覆盖新结果")
        if version_signature and job_signature and version_signature != job_signature:
            raise conflict("动态媒体版本与 Job input_signature 不一致，拒绝覆盖新结果")
        if (
            str(run.status) in _DYNAMIC_PROTECTED_STATUSES
            or str(stage.status) in _DYNAMIC_PROTECTED_STATUSES
        ):
            raise conflict("动态媒体任务关联的 run/stage 已取消或过期，拒绝激活旧媒体")
        if expected_signature and str(stage.input_signature or "") != expected_signature:
            raise conflict("动态媒体任务的 stage 签名已变化，拒绝激活旧媒体")
        expected_attempt = input_data.get("workflow_stage_attempt") or (
            provenance.get("workflow_stage_attempt") if isinstance(provenance, dict) else None
        )
        if expected_attempt is not None and str(latest_stage.attempt) != str(expected_attempt):
            raise conflict("动态媒体任务的 stage attempt 已变化，拒绝激活旧媒体")

    def _binding_reference_items(
        self,
        project_id: str,
        bindings: list[dict[str, Any]],
    ) -> list[tuple[Path, str]]:
        return [
            (Path(item["path"]), item["label"])
            for item in self._binding_reference_sources(project_id, bindings)
        ]

    def _binding_reference_sources(
        self,
        project_id: str,
        bindings: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        with self.database.engine.connect() as connection:
            for binding in bindings:
                role = str(binding.get("role", ""))
                if binding.get("asset_image_id"):
                    record = connection.execute(
                        select(
                            asset_image.c.file_path,
                            asset_image.c.provider_url,
                            asset.c.name,
                        )
                        .join(asset, asset.c.id == asset_image.c.asset_id)
                        .where(
                            asset_image.c.id == binding["asset_image_id"],
                            asset.c.project_id == project_id,
                        )
                    ).first()
                    relative = record.file_path if record else ""
                    name = str(record.name) if record else ""
                    url = str(record.provider_url or "") if record else ""
                elif binding.get("keyframe_id"):
                    record = connection.execute(
                        select(
                            keyframe_version.c.file_path,
                            keyframe_version.c.provider_url,
                            shot.c.shot_no,
                        )
                        .join(shot, shot.c.id == keyframe_version.c.shot_id)
                        .where(
                            keyframe_version.c.id == binding["keyframe_id"],
                            shot.c.project_id == project_id,
                        )
                    ).first()
                    relative = record.file_path if record else ""
                    name = f"上一镜分镜 {record.shot_no}" if record else ""
                    url = str(record.provider_url or "") if record else ""
                else:
                    relative = ""
                    name = ""
                    url = ""
                if not relative:
                    continue
                path = file_service.resolve_project_path(project_id, relative)
                if path.is_file() and path.stat().st_size > 0:
                    items.append(
                        {
                            "path": path,
                            "label": self._reference_role_label(role, name),
                            "url": url,
                        }
                    )
        return items

    @staticmethod
    def _provider_url_usable(url: str) -> bool:
        if not str(url or "").startswith(("http://", "https://")):
            return False
        try:
            query = parse_qs(urlsplit(url).query)
            expires = query.get("Expires", [])
            if not expires:
                return True
            expires_value = str(expires[0])
            try:
                expires_epoch = int(expires_value)
            except ValueError:
                # 支持 HTTP-date（如 "Wed, 21 Oct 2026 07:28:00 GMT"）：
                # 解析失败时按「无过期时间」处理，避免误判 URL 不可用。
                parsed = parsedate_to_datetime(expires_value)
                expires_epoch = int(parsed.timestamp()) if parsed is not None else 0
            if expires_epoch and expires_epoch <= int(time.time()):
                return False
        except (TypeError, ValueError):
            return False
        return True

    def _store_provider_url(self, table: Any, version_id: str, url: str) -> None:
        if not self._provider_url_usable(url):
            return
        with self.database.engine.begin() as connection:
            connection.execute(
                update(table)
                .where(table.c.id == version_id)
                .values(provider_url=str(url))
            )

    def _resolve_shot_reference_bindings(
        self,
        current: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if not current.get("scene_asset_id"):
            # 手动分镜可以不绑定任何素材直接生成；
            # 只要存在角色/物品/参考绑定，仍必须提供场景素材。
            if (
                current.get("character_ids")
                or current.get("prop_ids")
                or current.get("reference_bindings")
            ):
                raise conflict("分镜尚未绑定场景素材，请先在分镜编辑中绑定场景素材")
            return []
        requested: list[tuple[str, str]] = [
            (current["scene_asset_id"], "scene_layout"),
            *((item, "character_identity") for item in current["character_ids"]),
            *((item, "prop_identity") for item in current["prop_ids"]),
        ]
        requested_keys = {(role, asset_id) for asset_id, role in requested if asset_id}
        for binding in current["reference_bindings"]:
            role = str(binding.get("role", ""))
            asset_id = binding.get("asset_id")
            key = (role, asset_id)
            # scene_master 是辅助参考（MASTER 空间锚点），单独解析、失败不阻塞。
            if (
                asset_id
                and role not in ("previous_keyframe", "scene_master")
                and key not in requested_keys
            ):
                requested.append((asset_id, role))
                requested_keys.add(key)

        refreshed: list[dict[str, Any]] = []
        missing: list[str] = []
        type_labels = {
            "character_identity": "角色",
            "scene_layout": "场景",
            "scene_master": "场景母版",
            "prop_identity": "物品",
        }
        with self.database.engine.connect() as connection:
            for asset_id, role in requested:
                if not asset_id:
                    continue
                asset_record = connection.execute(
                    select(asset.c.name).where(
                        asset.c.id == asset_id,
                        asset.c.project_id == current["project_id"],
                    )
                ).first()
                image_record = self._current_scene_image_for_shot(
                    connection,
                    current,
                    asset_id,
                    role,
                )
                image_path = (
                    file_service.resolve_project_path(
                        current["project_id"],
                        image_record.file_path,
                    )
                    if image_record and image_record.file_path
                    else None
                )
                if (
                    asset_record is None
                    or image_record is None
                    or image_path is None
                    or not image_path.is_file()
                    or image_path.stat().st_size <= 0
                ):
                    name = str(asset_record.name) if asset_record else str(asset_id)
                    missing.append(f"{type_labels.get(role, '素材')}「{name}」")
                    continue
                refreshed.append(
                    {
                        "role": role,
                        "asset_id": asset_id,
                        "asset_image_id": image_record.id,
                    }
                )
        if missing:
            raise conflict(
                f"以下分镜素材缺少已完成的当前图片：{'、'.join(missing)}。"
                "请先在素材库生成或上传图片"
            )

        previous_bindings = [
            binding
            for binding in current["reference_bindings"]
            if binding.get("role") == "previous_keyframe"
        ]
        with self.database.engine.connect() as connection:
            for binding in previous_bindings:
                record = connection.execute(
                    select(keyframe_version.c.file_path)
                    .join(shot, shot.c.id == keyframe_version.c.shot_id)
                    .where(
                        keyframe_version.c.id == binding.get("keyframe_id"),
                        shot.c.project_id == current["project_id"],
                    )
                ).first()
                path = (
                    file_service.resolve_project_path(
                        current["project_id"],
                        record.file_path,
                    )
                    if record and record.file_path
                    else None
                )
                if path is None or not path.is_file() or path.stat().st_size <= 0:
                    raise conflict("上一镜关键帧参考图文件不存在或为空，请重新生成或上传关键帧")
        refreshed.extend(previous_bindings)
        return refreshed

    def _current_scene_image_for_shot(
        self,
        connection: Any,
        current: dict[str, Any],
        asset_id: str,
        role: str,
    ) -> Any | None:
        """返回分镜生成关键帧时应使用的场景参考图记录。

        优先级：
        1. 分镜选中的 Scene View 的当前图（scene_view_id 匹配）；
        2. Scene MASTER 的当前图（无选中视图或该视图未生成图时回退）；
        3. 旧版场景图（scene_view_id IS NULL，历史数据兼容）。

        角色/道具不参与 Scene Pack，保持原有 asset_id + is_current 逻辑。
        """
        if role != "scene_layout":
            return connection.execute(
                select(asset_image.c.id, asset_image.c.file_path)
                .where(
                    asset_image.c.asset_id == asset_id,
                    asset_image.c.is_current.is_(True),
                    asset_image.c.status == "completed",
                )
                .order_by(asset_image.c.version_no.desc())
            ).first()

        scene_view_id = current.get("scene_view_id")
        if scene_view_id:
            picked = connection.execute(
                select(asset_image.c.id, asset_image.c.file_path)
                .where(
                    asset_image.c.asset_id == asset_id,
                    asset_image.c.scene_view_id == scene_view_id,
                    asset_image.c.is_current.is_(True),
                    asset_image.c.status == "completed",
                )
                .order_by(asset_image.c.version_no.desc())
            ).first()
            if picked is not None and picked.file_path:
                return picked
        master_view_id = connection.execute(
            select(scene_view.c.id).where(
                scene_view.c.asset_id == asset_id,
                scene_view.c.view_key == "master",
            )
        ).scalar()
        if master_view_id:
            master = connection.execute(
                select(asset_image.c.id, asset_image.c.file_path)
                .where(
                    asset_image.c.asset_id == asset_id,
                    asset_image.c.scene_view_id == master_view_id,
                    asset_image.c.is_current.is_(True),
                    asset_image.c.status == "completed",
                )
                .order_by(asset_image.c.version_no.desc())
            ).first()
            if master is not None and master.file_path:
                return master
        return connection.execute(
            select(asset_image.c.id, asset_image.c.file_path)
            .where(
                asset_image.c.asset_id == asset_id,
                asset_image.c.scene_view_id.is_(None),
                asset_image.c.is_current.is_(True),
                asset_image.c.status == "completed",
            )
            .order_by(asset_image.c.version_no.desc())
        ).first()

    def _with_scene_master_reference(
        self,
        current: dict[str, Any],
        bindings: list[dict[str, Any]],
        *,
        max_reference_files: int | None = None,
    ) -> list[dict[str, Any]]:
        """模型能力允许时，为关键帧/视频补充 Scene MASTER 参考。

        仅当分镜场景参考图来自某个非 master 的 Scene View 时追加：
        MASTER 作为整体空间一致性锚点（role="scene_master"），
        帮助模型保持空间布局/材质/灯光一致，不改变构图主参考。
        不写回 shot.reference_bindings（避免刷新循环），只进媒体快照。
        """
        limit = max_reference_files if max_reference_files and max_reference_files > 0 else 16
        if len(bindings) >= limit:
            return bindings
        layout_bindings = [
            binding
            for binding in bindings
            if isinstance(binding, dict)
            and binding.get("role") == "scene_layout"
            and binding.get("asset_id")
            and binding.get("asset_image_id")
        ]
        if not layout_bindings:
            return bindings
        layout_binding = layout_bindings[0]
        scene_asset_id = str(layout_binding["asset_id"])
        layout_image_id = str(layout_binding["asset_image_id"])
        with self.database.engine.connect() as connection:
            layout_view_key = connection.execute(
                select(scene_view.c.view_key)
                .join(asset_image, asset_image.c.scene_view_id == scene_view.c.id)
                .where(asset_image.c.id == layout_image_id)
            ).scalar()
            if not layout_view_key or layout_view_key == "master":
                # 场景参考已是 MASTER 或旧图：无需重复补充
                return bindings
            master_view_id = connection.execute(
                select(scene_view.c.id).where(
                    scene_view.c.asset_id == scene_asset_id,
                    scene_view.c.view_key == "master",
                )
            ).scalar()
            if not master_view_id:
                return bindings
            master_image_id = connection.execute(
                select(asset_image.c.id)
                .where(
                    asset_image.c.asset_id == scene_asset_id,
                    asset_image.c.scene_view_id == master_view_id,
                    asset_image.c.is_current.is_(True),
                    asset_image.c.status == "completed",
                )
                .order_by(asset_image.c.version_no.desc())
            ).scalar()
        if not master_image_id:
            return bindings
        if any(
            isinstance(binding, dict)
            and binding.get("role") == "scene_master"
            and binding.get("asset_id") == scene_asset_id
            for binding in bindings
        ):
            return bindings
        return [
            *bindings,
            {
                "role": "scene_master",
                "asset_id": scene_asset_id,
                "asset_image_id": str(master_image_id),
            },
        ]

    def _refresh_shot_reference_bindings(self, current: dict[str, Any]) -> dict[str, Any]:
        latest = current
        for attempt in range(3):
            refreshed = self._resolve_shot_reference_bindings(latest)
            if refreshed == latest["reference_bindings"]:
                confirmed = self._shot(latest["id"])
                if confirmed["revision"] == latest["revision"]:
                    return confirmed
                latest = confirmed
                continue
            try:
                return ShotService(self.database, self.jobs).update(
                    latest["id"],
                    {"reference_bindings": refreshed},
                    expected_revision=latest["revision"],
                )
            except ServiceError as exc:
                if exc.status_code != 409 or attempt == 2:
                    raise
                latest = self._shot(latest["id"])
        raise conflict("分镜参考图绑定正在变化，请稍后重试")

    def _shot_template_context(
        self,
        current: dict[str, Any],
        prev_end_state: str = "",
    ) -> dict[str, Any]:
        asset_ids = [
            item
            for item in [
                current.get("scene_asset_id"),
                *current.get("character_ids", []),
                *current.get("prop_ids", []),
            ]
            if item
        ]
        names: dict[str, str] = {}
        if asset_ids:
            with self.database.engine.connect() as connection:
                names = {
                    row.id: row.name
                    for row in connection.execute(
                        select(asset.c.id, asset.c.name).where(asset.c.id.in_(asset_ids))
                    )
                }
        return shot_template_context(current, names, prev_end_state)

    @staticmethod
    def _decode_version(record: dict[str, Any], project_id: str) -> dict[str, Any]:
        result = dict(record)
        result["is_current"] = bool(result["is_current"])
        if result.get("error") != _DELETED_MEDIA_ERROR:
            result["error"] = sanitize_error_message(result.get("error", ""))
        for column in ("reference_files_json", "reference_bindings_json"):
            if column in result:
                result[column] = json_loads(result[column], [])
                result[column.removesuffix("_json")] = result[column]
        if result.get("file_path"):
            result["file_url"] = project_media_url(project_id, result["file_path"])
            result["media_url"] = result["file_url"]
        return result

    @staticmethod
    def _job_input(connection: Any, version_id: str) -> dict[str, Any]:
        raw = connection.execute(
            select(job.c.input_json)
            .where(job.c.target_id == version_id)
            .order_by(job.c.created_at.desc())
            .limit(1)
        ).scalar()
        return json_loads(raw, {})

    @staticmethod
    def _job_params(
        connection: Any,
        input_data: dict[str, Any],
        provider_id: str | None,
    ) -> dict[str, Any]:
        overrides = input_data.get("params", {})
        if not provider_id:
            return overrides
        provider_params = connection.execute(
            select(api_provider.c.params_json).where(api_provider.c.id == provider_id)
        ).scalar()
        return {**json_loads(provider_params, {}), **overrides}

    def _provider_model_value(
        self,
        connection: Any,
        provider_id: str | None,
        fallback: str,
        input_data: dict[str, Any],
        created_at: str,
    ) -> str:
        if not provider_id:
            return fallback
        provider_record = connection.execute(
            select(api_provider.c.model, api_provider.c.updated_at).where(
                api_provider.c.id == provider_id
            )
        ).first()
        if provider_record is None:
            return fallback
        queued_provider_updated_at = input_data.get("provider_updated_at")
        if isinstance(queued_provider_updated_at, str) and queued_provider_updated_at:
            if provider_record.updated_at == queued_provider_updated_at:
                return fallback
            return str(provider_record.model or fallback)
        if provider_record.updated_at > created_at:
            return str(provider_record.model or fallback)
        return fallback

    def _asset_expected_signature(
        self, connection: Any, record: dict[str, Any], current: dict[str, Any]
    ) -> str:
        input_data = self._job_input(connection, record["id"])
        params = self._job_params(connection, input_data, record["provider_id"])
        project_record = row_dict(
            connection.execute(select(project).where(project.c.id == current["project_id"])).first()
        )
        if project_record is None:
            raise not_found("项目")
        # 非 MASTER 视角图的输入签名额外包含视角提示词：编辑视角提示词后该 View
        # 的图片应标记为 stale（与 queue_asset_image 入队口径一致）。
        extra = self._scene_view_prompt_extra(connection, record.get("scene_view_id"))
        return asset_image_signature(
            self._effective_asset_signature(current, project_record, extra=extra),
            record["provider_id"] or "uploaded",
            self._provider_model_value(
                connection,
                record["provider_id"],
                record["model"],
                input_data,
                record["created_at"],
            ),
            params,
            record["reference_files_json"],
            bool(input_data.get("reference_mode")),
        )

    def _keyframe_expected_signature(
        self, connection: Any, record: dict[str, Any], current: dict[str, Any]
    ) -> str:
        input_data = self._job_input(connection, record["id"])
        params = self._job_params(connection, input_data, record["provider_id"])
        project_record = row_dict(
            connection.execute(select(project).where(project.c.id == current["project_id"])).first()
        )
        if project_record is None:
            raise not_found("项目")
        # 参考绑定基于「当前分镜绑定 + 重算 MASTER 追加」：与入队时口径一致，
        # 既不因 MASTER 追加差异而恒 stale，也能感知分镜绑定的真实变化。
        provider_config = row_dict(
            connection.execute(
                select(api_provider.c.params_json).where(api_provider.c.id == record["provider_id"])
            ).first()
        )
        max_reference_files = int(
            json_loads(provider_config.get("params_json") if provider_config else {}, {}).get(
                "max_reference_files", 16
            )
        )
        expected_bindings = self._with_scene_master_reference(
            current,
            current["reference_bindings"],
            max_reference_files=max_reference_files,
        )
        return keyframe_signature(
            self._effective_shot_signature(current, project_record),
            expected_bindings,
            record["provider_id"] or "uploaded",
            self._provider_model_value(
                connection,
                record["provider_id"],
                record["model"],
                input_data,
                record["created_at"],
            ),
            params,
        )

    def _video_expected_signature(
        self, connection: Any, record: dict[str, Any], current: dict[str, Any]
    ) -> str:
        input_data = self._job_input(connection, record["id"])
        params = self._job_params(connection, input_data, record["provider_id"])
        project_record = row_dict(
            connection.execute(select(project).where(project.c.id == current["project_id"])).first()
        )
        if project_record is None:
            raise not_found("项目")
        # 参考绑定基于「当前分镜绑定 + 重算 MASTER 追加」：与入队时口径一致，
        # 感知分镜绑定变化，但不受 MASTER 系统追加影响。
        supports_multiple_input_images = bool(
            input_data.get("supports_multiple_input_images") is True
        )
        if supports_multiple_input_images:
            provider_config = row_dict(
                connection.execute(
                    select(api_provider.c.params_json).where(
                        api_provider.c.id == record["provider_id"]
                    )
                ).first()
            )
            max_reference_files = int(
                json_loads(provider_config.get("params_json") if provider_config else {}, {}).get(
                    "max_reference_files", 16
                )
            )
            snapshot_bindings = self._with_scene_master_reference(
                current,
                current["reference_bindings"],
                max_reference_files=max_reference_files,
            )
        else:
            snapshot_bindings = []
        current_model = self._provider_model_value(
            connection,
            record["provider_id"],
            record["model"],
            input_data,
            record["created_at"],
        )
        effective_duration = self._effective_video_duration(
            current_model,
            current["duration_sec"],
        )
        generation_current = {**current, "duration_sec": effective_duration}
        # 与 queue_video 同口径重算上一镜结束状态/结束帧：它们参与视频提示词
        # 渲染与入队签名，expected 侧不重算会让连续镜头视频恒判 stale。
        prev_end_state = ""
        prev_end_frame_relative = None
        if current.get("transition_from_prev") in {"continuous", "elliptical"}:
            prev_shot_row = connection.execute(
                select(shot.c.id, shot.c.end_state, shot.c.next_hook)
                .where(
                    shot.c.chapter_id == current["chapter_id"],
                    shot.c.shot_order < current["shot_order"],
                )
                .order_by(shot.c.shot_order.desc())
                .limit(1)
            ).first()
            if prev_shot_row:
                prev_end_state = str(
                    prev_shot_row.end_state or prev_shot_row.next_hook or ""
                ).strip()
                if current.get("transition_from_prev") == "continuous":
                    prev_video_row = connection.execute(
                        select(video_version.c.end_frame_path)
                        .where(
                            video_version.c.shot_id == prev_shot_row.id,
                            video_version.c.is_current.is_(True),
                            video_version.c.status == "completed",
                            video_version.c.end_frame_path.isnot(None),
                        )
                        .limit(1)
                    ).first()
                    if prev_video_row and prev_video_row.end_frame_path:
                        prev_end_frame_relative = str(prev_video_row.end_frame_path)
        return video_signature(
            record["keyframe_version_id"] or "",
            self._effective_video_prompt(
                generation_current,
                project_record,
                reference_bindings=snapshot_bindings,
                prev_end_state=prev_end_state,
                prev_end_frame_path=prev_end_frame_relative,
            ),
            effective_duration,
            record["provider_id"] or "uploaded",
            current_model,
            params,
            snapshot_bindings,
        )

    @staticmethod
    def _effective_asset_signature(
        current: dict[str, Any],
        project_record: dict[str, Any],
        *,
        extra: Any = None,
    ) -> str:
        values = [
            VISUAL_PROMPT_TEMPLATE_VERSION,
            current["asset_type"],
            current["name"],
            current["profile"],
            current["prompt"],
            resolve_asset_negative_prompt(current["asset_type"], current["negative_prompt"]),
            current["consistency_keywords"],
            current["composition"],
            project_record["style_prompt"],
            resolve_visual_aspect_ratio(current["asset_type"], project_record["aspect_ratio"]),
        ]
        # 非 MASTER 视角图的视角提示词参与签名（见 _scene_view_prompt_extra）。
        if extra:
            values.append(extra)
        # 与分镜 override 语义一致：非空覆盖值纳入签名，空串（未编辑）不参与
        prompt_override = str(current.get("prompt_override") or "").strip()
        if prompt_override:
            values.append(prompt_override)
        return calculate_signature(*values)

    @staticmethod
    def _scene_view_prompt_extra(connection: Any, scene_view_id: Any) -> str | None:
        """非 MASTER 视角图参与签名的视角提示词；MASTER/旧图/视图已删除返回 None。"""
        if not scene_view_id:
            return None
        view = row_dict(
            connection.execute(
                select(
                    scene_view.c.view_key,
                    scene_view.c.prompt,
                    scene_view.c.prompt_override,
                ).where(scene_view.c.id == scene_view_id)
            ).first()
        )
        if view is None or view["view_key"] == "master":
            return None
        return str(view.get("prompt_override") or view.get("prompt") or "").strip() or None

    def _effective_shot_signature(
        self,
        current: dict[str, Any],
        project_record: dict[str, Any],
    ) -> str:
        reference_labels = [
            label
            for _, label in self._binding_reference_items(
                current["project_id"],
                current["reference_bindings"],
            )
        ]
        return calculate_signature(
            VISUAL_PROMPT_TEMPLATE_VERSION,
            current["own_signature"],
            self._shot_template_context(current),
            reference_labels,
            project_record["style_prompt"],
            resolve_visual_aspect_ratio("keyframe", project_record["aspect_ratio"]),
        )

    def _effective_video_prompt(
        self,
        current: dict[str, Any],
        project_record: dict[str, Any],
        *,
        reference_bindings: list[dict[str, Any]] | None = None,
        prev_end_state: str = "",
        prev_end_frame_path: str | None = None,
    ) -> str:
        video_prompt = str(
            current.get("video_prompt_override") or ""
        ).strip() or resolve_video_description(
            video_prompt=current["video_prompt"],
            start_state=current["start_state"],
            action=current["action"],
            end_state=current["end_state"],
            camera_move=current["camera_move"],
            duration_sec=current["duration_sec"],
        )
        reference_labels = [
            label
            for _, label in self._binding_reference_items(
                current["project_id"],
                reference_bindings or [],
            )
        ]
        return calculate_signature(
            VISUAL_PROMPT_TEMPLATE_VERSION,
            video_prompt,
            current["negative_prompt"],
            self._shot_template_context(current),
            current.get("director_plan") or {},
            reference_labels,
            prev_end_state,
            prev_end_frame_path or "",
            project_record["style_prompt"],
            resolve_visual_aspect_ratio("video", project_record["aspect_ratio"]),
        )

    def list_asset_images(
        self,
        asset_id: str,
        *,
        scene_view_id: str | None = None,
    ) -> list[dict[str, Any]]:
        current = self._asset(asset_id)
        with self.database.engine.connect() as connection:
            statement = (
                select(asset_image)
                .where(
                    asset_image.c.asset_id == asset_id,
                    asset_image.c.error != _DELETED_MEDIA_ERROR,
                )
                .order_by(asset_image.c.version_no.desc())
            )
            if scene_view_id:
                statement = statement.where(asset_image.c.scene_view_id == scene_view_id)
            records = []
            for row in connection.execute(statement):
                record = self._decode_version(dict(row._mapping), current["project_id"])
                expected = self._asset_expected_signature(connection, record, current)
                record["is_stale"] = is_stale(record["input_signature"], expected)
                record["stale_reason"] = (
                    "素材描述、提示词或 Provider 参数已变化" if record["is_stale"] else ""
                )
                records.append(record)
        return records

    def list_keyframes(self, shot_id: str) -> list[dict[str, Any]]:
        current = self._shot(shot_id)
        with self.database.engine.connect() as connection:
            records = []
            for row in connection.execute(
                select(keyframe_version)
                .where(
                    keyframe_version.c.shot_id == shot_id,
                    keyframe_version.c.error != _DELETED_MEDIA_ERROR,
                )
                .order_by(keyframe_version.c.version_no.desc())
            ):
                record = self._decode_version(dict(row._mapping), current["project_id"])
                expected = self._keyframe_expected_signature(connection, record, current)
                record["is_stale"] = is_stale(record["input_signature"], expected)
                record["stale_reason"] = (
                    "分镜、提示词或参考图绑定已变化" if record["is_stale"] else ""
                )
                records.append(record)
        return records

    def list_videos(self, shot_id: str) -> list[dict[str, Any]]:
        current = self._shot(shot_id)
        with self.database.engine.connect() as connection:
            records = []
            for row in connection.execute(
                select(video_version)
                .where(
                    video_version.c.shot_id == shot_id,
                    video_version.c.error != _DELETED_MEDIA_ERROR,
                )
                .order_by(video_version.c.version_no.desc())
            ):
                record = self._decode_version(dict(row._mapping), current["project_id"])
                expected = self._video_expected_signature(connection, record, current)
                record["is_stale"] = is_stale(record["input_signature"], expected)
                record["stale_reason"] = (
                    "关键帧、视频提示词、时长或 Provider 参数已变化" if record["is_stale"] else ""
                )
                records.append(record)
        return records

    def _insert_version(
        self,
        *,
        table: Any,
        parent_column: str,
        parent_id: str,
        values: dict[str, Any],
    ) -> dict[str, Any]:
        version_id = new_id(
            "asset_image"
            if table is asset_image
            else "keyframe"
            if table is keyframe_version
            else "video"
        )
        with self.database.engine.begin() as connection:
            version_no = (
                connection.execute(
                    select(func.max(table.c.version_no)).where(table.c[parent_column] == parent_id)
                ).scalar()
                or 0
            ) + 1
            record = {
                "id": version_id,
                parent_column: parent_id,
                "version_no": version_no,
                "created_at": utc_now(),
                **values,
            }
            connection.execute(insert(table).values(**record))
        return record

    def queue_asset_image(self, asset_id: str, options: dict[str, Any]) -> dict[str, Any]:
        current = self._asset(asset_id)
        if not str(current.get("name") or "").strip():
            raise bad_request("素材名称不能为空")
        if not self._asset_has_generation_content(current):
            raise bad_request("素材缺少可用于生图的真实视觉描述")
        current_project = self._project(current["project_id"])
        _, config = self.providers.resolve(
            kind="image",
            provider_id=options.get("provider_id") or current_project["default_image_provider_id"],
        )
        request_params = self._image_generation_params(
            config["model"],
            current["asset_type"],
            options.get("params", {}),
            provider_config=config,
        )
        params = {**config["params"], **request_params}
        references: list[str] = []
        negative_prompt = resolve_asset_negative_prompt(
            current["asset_type"],
            current["negative_prompt"],
        )
        # ---- Scene Pack 分支：MASTER 用「干净场景参考图」模板（单图、无拼板）；
        # 其他 Scene View 以 MASTER 图为参考图 + 视角附加提示词生成。----
        prompt_override = str(current.get("prompt_override") or "").strip()
        scene_view_key = str(options.get("scene_view_key") or "")
        scene_view_id = options.get("scene_view_id")
        # 非 MASTER 视角图把视角提示词纳入输入签名：编辑视角提示词后该 View
        # 的当前图会被标记为 stale（与 _asset_expected_signature 口径一致）。
        view_signature_extra: str | None = None
        view_record: dict[str, Any] | None = None
        if scene_view_key:
            with self.database.engine.connect() as connection:
                view_record = row_dict(
                    connection.execute(
                        select(scene_view).where(scene_view.c.id == scene_view_id)
                    ).first()
                )
            if view_record is None:
                raise bad_request("场景视图不存在")
            reference_master_image_id = options.get("reference_master_image_id")
            if reference_master_image_id:
                with self.database.engine.connect() as connection:
                    master_path = connection.execute(
                        select(asset_image.c.file_path).where(
                            asset_image.c.id == reference_master_image_id
                        )
                    ).scalar()
                if master_path:
                    references.append(str(master_path))
        if scene_view_key == "master":
            # 母版：单张完整空间全景透视图（scene_master.md 模板），
            # 不再输出多视图拼板展板；素材级 prompt_override（用户旧版编辑）仍然优先。
            master_prompt_override = str(current.get("prompt_override") or "").strip()
            if master_prompt_override:
                prompt = master_prompt_override
            else:
                prompt = compile_asset_prompt(
                style_prompt=current_project["style_prompt"],
                aspect_ratio=current_project["aspect_ratio"],
                asset_type=current["asset_type"],
                asset_name=current["name"],
                profile=current["profile"],
                prompt=current["prompt"],
                negative_prompt=negative_prompt,
                consistency_keywords=current["consistency_keywords"],
                composition=current["composition"],
                master_mode=True,
            )
        elif scene_view_key and view_record is not None:
            # 视角：以 MASTER 语义为基准 + 视角附加提示词；参考图 = MASTER 图。
            master_body = compile_asset_prompt(
                style_prompt=current_project["style_prompt"],
                aspect_ratio=current_project["aspect_ratio"],
                asset_type=current["asset_type"],
                asset_name=current["name"],
                profile=current["profile"],
                prompt=current["prompt"],
                negative_prompt=negative_prompt,
                consistency_keywords=current["consistency_keywords"],
                composition=current["composition"],
                reference_mode=True,
            )
            view_prompt = str(
                view_record.get("prompt_override") or view_record.get("prompt") or ""
            ).strip()
            if not view_prompt:
                raise bad_request(f"场景视图「{view_record.get('name')}」缺少视角提示词")
            view_signature_extra = view_prompt
            if current_project.get("is_free_create"):
                prompt = view_prompt
            else:
                prompt = join_non_empty(
                    [
                        master_body,
                        f"【{view_record.get('name')}视角】\n{view_prompt}\n"
                        "以上视角要求仅改变摄影机位置、方向、景别与构图；"
                        "空间布局、门窗/家具位置、固定装饰、材质、主色调、灯光环境与"
                        "空间比例必须与 MASTER 参考图完全一致，不重新设计场景，"
                        "画面为无人纯背景空镜。",
                    ]
                )
        else:
            if current_project.get("is_free_create"):
                # 自由制作直接使用用户填写的素材提示词生图，不套模板、不追加风格设定
                prompt = str(current.get("prompt") or "").strip()
            else:
                prompt = compile_asset_prompt(
                    style_prompt=current_project["style_prompt"],
                    aspect_ratio=current_project["aspect_ratio"],
                    asset_type=current["asset_type"],
                    asset_name=current["name"],
                    profile=current["profile"],
                    prompt=current["prompt"],
                    negative_prompt=negative_prompt,
                    consistency_keywords=current["consistency_keywords"],
                    composition=current["composition"],
                    reference_mode=bool(options.get("reference_mode")),
                )
        # 素材级 prompt_override（用户在素材库编辑过的最终提示词）只用于
        # 旧版场景图（非 Scene View 分支）与 MASTER；Scene View 分支使用
        # 各自的视角提示词，不被素材级 override 覆盖。
        if prompt_override and not scene_view_key:
            # 用户编辑过的最终提示词优先，不再按模板重新编译
            prompt = prompt_override
        reference_mode = bool(options.get("reference_mode"))
        version_values: dict[str, Any] = {
            "file_path": None,
            "prompt_snapshot": prompt,
            "negative_prompt_snapshot": negative_prompt,
            "reference_files_json": json_dumps(references),
            "provider_id": config["id"],
            "model": config["model"],
            "provider_task_id": "",
            "status": "queued",
            "is_current": False,
            "input_signature": asset_image_signature(
                self._effective_asset_signature(
                    current, current_project, extra=view_signature_extra
                ),
                config["id"],
                config["model"],
                params,
                references,
                reference_mode,
            ),
            "source": "generated",
            "error": "",
        }
        if scene_view_id is not None:
            # 场景视图图片：记录所属 View，激活时按 (asset_id, scene_view_id) 分区。
            version_values["scene_view_id"] = scene_view_id
        version = self._insert_version(
            table=asset_image,
            parent_column="asset_id",
            parent_id=asset_id,
            values=version_values,
        )
        return self.jobs.create(
            project_id=current["project_id"],
            job_type="generate_asset_image",
            target_type="asset_image",
            target_id=version["id"],
            provider_kind="image",
            provider_id=config["id"],
            input_data={
                "asset_id": asset_id,
                "provider_updated_at": str(config.get("updated_at") or ""),
                "params": request_params,
                "reference_mode": reference_mode,
            },
            batch_id=options.get("_batch_id"),
        )

    def queue_asset_image_batch(self, project_id: str, options: dict[str, Any]) -> dict[str, Any]:
        current_project = self._project(project_id)
        statement = select(asset.c.id).where(asset.c.project_id == project_id)
        if options.get("ids"):
            statement = statement.where(asset.c.id.in_(options["ids"]))
        if options.get("asset_type"):
            statement = statement.where(asset.c.asset_type == options["asset_type"])
        if options.get("only_missing"):
            statement = statement.where(
                ~select(asset_image.c.id)
                .where(
                    asset_image.c.asset_id == asset.c.id,
                    or_(
                        asset_image.c.is_current.is_(True),
                        asset_image.c.status.in_(_ACTIVE_MEDIA_STATUSES),
                    ),
                )
                .exists()
            )
        with self.database.engine.connect() as connection:
            ids = list(connection.execute(statement).scalars())
        batch_id = new_id("batch")
        jobs = []
        failures: list[dict[str, str]] = []
        for asset_id in ids:
            try:
                job_record = self.queue_asset_image(
                    asset_id,
                    {
                        "provider_id": options.get("provider_id")
                        or current_project["default_image_provider_id"],
                        "params": options.get("params", {}),
                        "_batch_id": batch_id,
                    },
                )
            except Exception as exc:
                failures.append(
                    {
                        "target_id": asset_id,
                        "error": sanitize_error_message(exc),
                    }
                )
                continue
            jobs.append(job_record)
        return {"batch_id": batch_id, "jobs": jobs, "preflight_failures": failures}

    def queue_keyframe(self, shot_id: str, options: dict[str, Any]) -> dict[str, Any]:
        current = self._shot(shot_id)
        current_project = self._project(current["project_id"])
        image_prompt_override = str(current.get("image_prompt_override") or "").strip()
        image_prompt = image_prompt_override or str(current["image_prompt"] or "").strip()
        if not image_prompt:
            raise bad_request("分镜缺少明确的关键帧提示词")
        current = self._refresh_shot_reference_bindings(current)
        bindings = current["reference_bindings"]
        _, config = self.providers.resolve(
            kind="image",
            provider_id=options.get("provider_id") or current_project["default_image_provider_id"],
        )
        request_params = self._image_generation_params(
            config["model"],
            "keyframe",
            options.get("params", {}),
            provider_config=config,
        )
        params = {**config["params"], **request_params}
        # 模型能力允许时补充 MASTER 空间锚点参考（媒体快照级，不写回分镜）。
        max_reference_files = int(config.get("params", {}).get("max_reference_files") or 16)
        bindings = self._with_scene_master_reference(
            current,
            bindings,
            max_reference_files=max_reference_files,
        )
        reference_items = self._binding_reference_items(current["project_id"], bindings)
        reference_anchors = [
            f"@Image{index}：{label}。" for index, (_, label) in enumerate(reference_items, start=1)
        ]
        prompt_snapshot = image_prompt_override
        if not prompt_snapshot:
            prompt_snapshot = compile_keyframe_prompt(
                style_prompt=current_project["style_prompt"],
                aspect_ratio=current_project["aspect_ratio"],
                image_prompt=image_prompt,
                anchors=reference_anchors,
                shot_context=self._shot_template_context(current),
                negative_prompt=current["negative_prompt"],
            )
        version = self._insert_version(
            table=keyframe_version,
            parent_column="shot_id",
            parent_id=shot_id,
            values={
                "file_path": None,
                "prompt_snapshot": prompt_snapshot,
                "negative_prompt_snapshot": current["negative_prompt"],
                "reference_bindings_json": json_dumps(bindings),
                "provider_id": config["id"],
                "model": config["model"],
                "provider_task_id": "",
                "status": "queued",
                "is_current": False,
                "input_signature": keyframe_signature(
                    self._effective_shot_signature(current, current_project),
                    bindings,
                    config["id"],
                    config["model"],
                    params,
                ),
                "source": "generated",
                "error": "",
            },
        )
        return self.jobs.create(
            project_id=current["project_id"],
            chapter_id=current["chapter_id"],
            job_type="generate_keyframe",
            target_type="keyframe",
            target_id=version["id"],
            provider_kind="image",
            provider_id=config["id"],
            input_data={
                "shot_id": shot_id,
                "provider_updated_at": str(config.get("updated_at") or ""),
                "params": request_params,
            },
            batch_id=options.get("_batch_id"),
        )

    def queue_keyframe_batch(self, project_id: str, options: dict[str, Any]) -> dict[str, Any]:
        return self._queue_shot_media_batch(project_id, options, media_kind="keyframe")

    def queue_keyframe_batch_chapter(
        self,
        chapter_id: str,
        options: dict[str, Any],
    ) -> dict[str, Any]:
        return self._queue_shot_media_batch(
            "",
            options,
            media_kind="keyframe",
            chapter_id=chapter_id,
        )

    def queue_video(self, shot_id: str, options: dict[str, Any]) -> dict[str, Any]:
        current = self._shot(shot_id)
        current_project = self._project(current["project_id"])
        video_prompt_override = str(current.get("video_prompt_override") or "").strip()
        video_prompt = video_prompt_override or str(current["video_prompt"] or "").strip()
        if not video_prompt:
            raise bad_request("分镜缺少明确的视频提示词")
        depends_on: list[str] = []
        with self.database.engine.connect() as connection:
            keyframe = row_dict(
                connection.execute(
                    select(keyframe_version).where(
                        keyframe_version.c.shot_id == shot_id,
                        keyframe_version.c.is_current.is_(True),
                        keyframe_version.c.status == "completed",
                    )
                ).first()
            )
            if keyframe is not None:
                keyframe_job_id = connection.execute(
                    select(job.c.id)
                    .where(
                        job.c.target_type == "keyframe",
                        job.c.target_id == keyframe["id"],
                    )
                    .order_by(job.c.created_at.desc(), job.c.id.desc())
                    .limit(1)
                ).scalar()
                if keyframe_job_id:
                    depends_on.append(str(keyframe_job_id))

            # 上一镜视频末帧：仅在本镜声明动作与上一镜连续（continuous）时引用。
            # 不能用「场景素材是否相同」推断——同场景可能是对切或时间跳跃，
            # 跨场景也可能是连续动作（如推门从室内走到室外）。
            prev_end_frame_relative = None
            prev_end_state = ""
            if current.get("transition_from_prev") in {"continuous", "elliptical"}:
                prev_shot_row = connection.execute(
                    select(shot.c.id, shot.c.end_state, shot.c.next_hook)
                    .where(
                        shot.c.chapter_id == current["chapter_id"],
                        shot.c.shot_order < current["shot_order"],
                    )
                    .order_by(shot.c.shot_order.desc())
                    .limit(1)
                ).first()

                if prev_shot_row:
                    prev_end_state = str(
                        prev_shot_row.end_state or prev_shot_row.next_hook or ""
                    ).strip()
                    if current.get("transition_from_prev") == "continuous":
                        prev_video_row = connection.execute(
                            select(video_version.c.id, video_version.c.end_frame_path)
                            .where(
                                video_version.c.shot_id == prev_shot_row.id,
                                video_version.c.is_current.is_(True),
                                video_version.c.status == "completed",
                                video_version.c.end_frame_path.isnot(None),
                            )
                            .limit(1)
                        ).first()

                        if prev_video_row and prev_video_row.end_frame_path:
                            prev_end_frame_relative = str(prev_video_row.end_frame_path)
                            previous_video_job_id = connection.execute(
                                select(job.c.id)
                                .where(
                                    job.c.target_type == "video",
                                    job.c.target_id == prev_video_row.id,
                                )
                                .order_by(job.c.created_at.desc(), job.c.id.desc())
                                .limit(1)
                            ).scalar()
                            if previous_video_job_id:
                                depends_on.append(str(previous_video_job_id))

        if keyframe is None or not keyframe.get("file_path"):
            raise conflict("分镜缺少已完成的当前关键帧")
        keyframe_path = file_service.resolve_project_path(
            current["project_id"],
            keyframe["file_path"],
        )
        if not keyframe_path.is_file() or keyframe_path.stat().st_size <= 0:
            raise conflict("当前分镜关键帧文件不存在或为空，请重新生成或上传关键帧")
        # 入队即校验：路径失效时当场放弃该引用。prompt 一旦快照就无法改 @Image 编号，
        # 留到执行期才发现对不上只能整单失败
        if prev_end_frame_relative and not self._readable_project_file(
            current["project_id"],
            prev_end_frame_relative,
        ):
            logger.warning(
                "上一镜结束帧文件失效，本次视频不引用：project=%s path=%s",
                current["project_id"],
                prev_end_frame_relative,
            )
            prev_end_frame_relative = None
        current = self._refresh_shot_reference_bindings(current)
        provider, config = self.providers.resolve(
            kind="video",
            provider_id=options.get("provider_id") or current_project["default_video_provider_id"],
        )
        effective_duration = self._effective_video_duration(
            config["model"],
            current["duration_sec"],
        )
        params = {**config["params"], **options.get("params", {})}
        generation_current = {**current, "duration_sec": effective_duration}
        supports_multiple_input_images = self._supports_multiple_video_inputs(provider)
        if supports_multiple_input_images:
            # 多图视频 Provider：可携带 MASTER 空间锚点辅助参考（媒体快照级）。
            reference_bindings = self._with_scene_master_reference(
                current,
                current["reference_bindings"],
                max_reference_files=int(config.get("params", {}).get("max_reference_files") or 16),
            )
        else:
            reference_bindings = []
        if not supports_multiple_input_images:
            # 单图 Provider 只下发首图，不能引用上一镜末帧，否则 @Image2 会悬空
            prev_end_frame_relative = None
        reference_items = self._binding_reference_items(
            current["project_id"],
            reference_bindings,
        )

        # 自由制作分镜从第二镜起默认「连续承接」：上一镜末帧作为本镜第 0 秒起始帧，
        # 当前关键帧作为结束目标帧。其余分镜保持关键帧为首图的旧语义。
        prev_end_frame_first = (
            current.get("source_ref") == "manual"
            and bool(prev_end_frame_relative)
        )
        reference_anchors = (
            [
                "@Image1：上一镜结束帧，作为本镜第0秒起始帧（严格从该画面开始）。",
                "@Image2：当前分镜关键帧，作为本镜动作结束的目标帧。",
            ]
            if prev_end_frame_first
            else [
                "@Image1：当前分镜关键帧，锁定构图、人物左右关系和起始站位。",
            ]
        )
        if prev_end_frame_relative and not prev_end_frame_first:
            reference_anchors.append(
                "@Image2：上一镜结束帧，仅用于衔接连贯，不是本镜起始画面。"
            )
        # 先算基准编号：extend 的生成器里取 len() 会随列表增长而漂移
        base_index = len(reference_anchors) + 1
        reference_anchors.extend(
            f"@Image{base_index + index}：{label}。"
            for index, (_, label) in enumerate(reference_items)
        )

        shot_context = self._shot_template_context(
            generation_current,
            prev_end_state=prev_end_state,
        )
        prompt = video_prompt_override
        if not prompt:
            prompt = compile_video_prompt(
                style_prompt=current_project["style_prompt"],
                aspect_ratio=current_project["aspect_ratio"],
                video_prompt=video_prompt,
                duration_sec=effective_duration,
                anchors=reference_anchors,
                shot_context=shot_context,
                negative_prompt=current["negative_prompt"],
                director_plan=generation_current.get("director_plan") or None,
            )
        # 最终一致性校验：导演计划存在时执行硬校验，blocking 问题直接阻断入队。
        consistency_issues: list[dict[str, Any]] = []
        director_plan_value = generation_current.get("director_plan") or None
        if director_plan_value:
            bundle = director_bundle_from_llm(director_plan_value)
            consistency_issues = [
                {
                    "code": issue.code,
                    "severity": issue.severity,
                    "message": issue.message,
                    "details": issue.details,
                }
                for issue in validate_shot_consistency(
                    shot={
                        **generation_current,
                        "prop_names": shot_context.get("props") or [],
                    },
                    timeline=bundle.timeline,
                    dialogue_bindings=bundle.dialogue_bindings,
                    duration_sec=effective_duration,
                    prev_end_state=prev_end_state,
                )
            ]
            blocking_issues = [
                issue for issue in consistency_issues if issue["severity"] == "blocking"
            ]
            if blocking_issues:
                raise conflict(
                    "导演设计一致性校验未通过："
                    + "；".join(
                        f"[{issue['code']}] {issue['message']}"
                        for issue in blocking_issues
                    )
                    + "。请重新生成导演设计或修改分镜。"
                )
        # 素材引用确定性校验：@Image 编号必须与本次实际下发的参考图数组一一对应。
        # 出现悬空编号时拒绝入队，避免把错误引用送给视频模型。
        reference_tokens = parse_reference_anchors(reference_anchors)
        reference_validation = validate_prompt_references(
            prompt,
            reference_tokens,
            require_keyframe=not bool(video_prompt_override),
        )
        for issue in reference_validation.issues:
            if bool(video_prompt_override):
                # 用户手工覆盖版：关键帧引用缺失只记录告警，不阻断用户自己的文案；
                # 悬空编号（引用了不存在的图片）仍然阻断。
                if issue.startswith("当前分镜关键帧引用"):
                    logger.warning(
                        "视频提示词覆盖版缺少关键帧引用：project=%s shot=%s",
                        current["project_id"],
                        shot_id,
                    )
                    continue
                logger.warning(
                    "视频提示词覆盖版引用校验失败：project=%s shot=%s issue=%s",
                    current["project_id"],
                    shot_id,
                    issue,
                )
            raise conflict(
                "视频提示词素材引用校验未通过："
                + issue
                + "。请重新生成或手动修正视频提示词后再生成视频。"
            )
        version = self._insert_version(
            table=video_version,
            parent_column="shot_id",
            parent_id=shot_id,
            values={
                "keyframe_version_id": keyframe["id"],
                "file_path": None,
                "prompt_snapshot": prompt,
                "negative_prompt_snapshot": current["negative_prompt"],
                "requested_duration_sec": effective_duration,
                "actual_duration_sec": None,
                "provider_id": config["id"],
                "model": config["model"],
                "provider_task_id": "",
                "status": "queued",
                "is_current": False,
                "input_signature": video_signature(
                    keyframe["id"],
                    self._effective_video_prompt(
                        generation_current,
                        current_project,
                        reference_bindings=reference_bindings,
                        prev_end_state=prev_end_state,
                        prev_end_frame_path=prev_end_frame_relative,
                    ),
                    effective_duration,
                    config["id"],
                    config["model"],
                    params,
                    reference_bindings,
                ),
                "source": "generated",
                "cost_estimate": 0,
                "error": "",
            },
        )
        created_job = self.jobs.create(
            project_id=current["project_id"],
            chapter_id=current["chapter_id"],
            job_type="generate_video",
            target_type="video",
            target_id=version["id"],
            provider_kind="video",
            provider_id=config["id"],
            input_data={
                "shot_id": shot_id,
                "keyframe_version_id": keyframe["id"],
                "reference_bindings": reference_bindings,
                # 项目内相对路径；为空表示本镜不引用上一镜结束帧
                "prev_end_frame_path": prev_end_frame_relative,
                "prev_end_frame_first": prev_end_frame_first,
                "supports_multiple_input_images": supports_multiple_input_images,
                "depends_on": depends_on,
                "provider_updated_at": str(config.get("updated_at") or ""),
                "params": options.get("params", {}),
            },
            depends_on=depends_on,
            batch_id=options.get("_batch_id"),
        )
        if consistency_issues:
            created_job["consistency_issues"] = consistency_issues
        return created_job

    def queue_video_batch(self, project_id: str, options: dict[str, Any]) -> dict[str, Any]:
        return self._queue_shot_media_batch(project_id, options, media_kind="video")

    def queue_video_batch_chapter(
        self,
        chapter_id: str,
        options: dict[str, Any],
    ) -> dict[str, Any]:
        return self._queue_shot_media_batch(
            "",
            options,
            media_kind="video",
            chapter_id=chapter_id,
        )

    def _queue_shot_media_batch(
        self,
        project_id: str,
        options: dict[str, Any],
        *,
        media_kind: Literal["keyframe", "video"],
        chapter_id: str | None = None,
    ) -> dict[str, Any]:
        if chapter_id:
            project_id = self._chapter_project_id(chapter_id)
        self._project(project_id)
        statement = select(shot.c.id).where(shot.c.project_id == project_id)
        if chapter_id:
            statement = statement.where(shot.c.chapter_id == chapter_id)
        if options.get("ids"):
            statement = statement.where(shot.c.id.in_(options["ids"]))
        if options.get("scene_no"):
            # 转义 LIKE 通配符（%/_），避免 scene_no 含这些字符时意外匹配其他分镜。
            escaped = (
                str(options["scene_no"])
                .replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            statement = statement.where(
                shot.c.source_ref.like(f"scene:{escaped}|%", escape="\\")
            )
        table = keyframe_version if media_kind == "keyframe" else video_version
        if options.get("only_missing"):
            statement = statement.where(
                ~select(table.c.id)
                .where(
                    table.c.shot_id == shot.c.id,
                    or_(
                        table.c.is_current.is_(True),
                        table.c.status.in_(_ACTIVE_MEDIA_STATUSES),
                    ),
                )
                .exists()
            )
        with self.database.engine.connect() as connection:
            ids = list(connection.execute(statement).scalars())
        batch_id = new_id("batch")
        records: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        queue_one = self.queue_keyframe if media_kind == "keyframe" else self.queue_video
        for shot_id in ids:
            try:
                records.append(queue_one(shot_id, {**options, "_batch_id": batch_id}))
            except Exception as exc:
                failures.append(
                    {
                        "target_id": shot_id,
                        "error": sanitize_error_message(exc),
                    }
                )
        return {
            "batch_id": batch_id,
            "jobs": records,
            "preflight_failures": failures,
        }

    @staticmethod
    def _image_suffix(filename: str) -> str:
        suffix = Path(filename).suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise bad_request("图片仅支持 PNG、JPG、JPEG 或 WEBP")
        return suffix

    @staticmethod
    def _verify_image(data: bytes) -> None:
        try:
            with Image.open(io.BytesIO(data)) as image:
                image.verify()
        except (UnidentifiedImageError, OSError) as exc:
            raise bad_request("上传内容不是可解码图片") from exc

    @staticmethod
    def _video_suffix(filename: str) -> str:
        suffix = Path(filename).suffix.lower()
        if suffix not in {".mp4", ".webm", ".mov"}:
            raise bad_request("视频仅支持 MP4、WEBM 或 MOV")
        return suffix

    @staticmethod
    def _verify_video(path: Path) -> float:
        try:
            probe = subprocess.run(
                [
                    settings.ffprobe_path,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration:stream=codec_type",
                    "-of",
                    "json",
                    str(path),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise bad_request("无法校验视频，请确认 FFmpeg 已安装且可用") from exc
        payload = json_loads(probe.stdout, {})
        streams = payload.get("streams", [])
        try:
            duration = float(payload.get("format", {}).get("duration", 0))
        except (TypeError, ValueError):
            duration = 0
        if (
            probe.returncode != 0
            or duration <= 0
            or not any(stream.get("codec_type") == "video" for stream in streams)
        ):
            raise bad_request("视频文件无法解码")
        return duration

    @staticmethod
    def _extract_last_frame(video_path: Path, output_path: Path) -> None:
        """从视频中提取最后一帧"""
        try:
            result = subprocess.run(
                [
                    settings.ffmpeg_path,
                    "-sseof",
                    "-1",  # 从视频末尾倒数
                    "-i",
                    str(video_path),
                    "-update",
                    "1",
                    "-q:v",
                    "1",  # 高质量
                    "-vframes",
                    "1",
                    "-y",  # 覆盖输出文件
                    str(output_path),
                ],
                check=False,
                capture_output=True,
                timeout=30,
            )
            if (
                result.returncode != 0
                or not output_path.is_file()
                or output_path.stat().st_size <= 0
            ):
                raise FileServiceError(
                    f"FFmpeg 提取最后一帧失败: {result.stderr.decode('utf-8', errors='ignore')}"
                )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise FileServiceError("无法提取视频最后一帧，请确认 FFmpeg 已安装且可用") from exc

    def upload_asset_image(
        self,
        asset_id: str,
        filename: str,
        content_type: str,
        data: bytes,
    ) -> dict[str, Any]:
        del content_type
        if len(data) > MAX_IMAGE_SIZE:
            raise bad_request("图片大小超过限制（最大 10MB）")
        self._verify_image(data)
        current = self._asset(asset_id)
        suffix = self._image_suffix(filename)
        version = self._insert_version(
            table=asset_image,
            parent_column="asset_id",
            parent_id=asset_id,
            values={
                "file_path": None,
                "prompt_snapshot": current["prompt"],
                "negative_prompt_snapshot": current["negative_prompt"],
                "reference_files_json": "[]",
                "provider_id": None,
                "model": "",
                "provider_task_id": "",
                "status": "completed",
                "is_current": False,
                "input_signature": asset_image_signature(
                    self._effective_asset_signature(current, self._project(current["project_id"])),
                    "uploaded",
                    "",
                    {},
                    [],
                ),
                "source": "uploaded",
                "error": "",
            },
        )
        target = file_service.asset_image_path(
            current["project_id"],
            current["asset_type"],
            asset_id,
            version["version_no"],
            suffix,
            asset_name=current["name"],
            storage_name=self._project_storage_name(current["project_id"]),
        )
        return self._finish_upload(
            kind="asset_image",
            version_id=version["id"],
            project_id=current["project_id"],
            target=target,
            data=data,
        )

    def upload_keyframe(
        self,
        shot_id: str,
        filename: str,
        content_type: str,
        data: bytes,
    ) -> dict[str, Any]:
        del content_type
        if len(data) > MAX_IMAGE_SIZE:
            raise bad_request("图片大小超过限制（最大 10MB）")
        self._verify_image(data)
        current = self._shot(shot_id)
        suffix = self._image_suffix(filename)
        # 与 _keyframe_expected_signature 同口径：场景参考来自非 master 视图时
        # 追加 MASTER 参考再算签名，否则上传的关键帧落库即被恒判 stale。
        expected_bindings = self._with_scene_master_reference(
            current,
            current["reference_bindings"],
            max_reference_files=16,
        )
        version = self._insert_version(
            table=keyframe_version,
            parent_column="shot_id",
            parent_id=shot_id,
            values={
                "file_path": None,
                "prompt_snapshot": str(
                    current.get("image_prompt_override") or current["image_prompt"] or ""
                ).strip(),
                "negative_prompt_snapshot": current["negative_prompt"],
                "reference_bindings_json": current["reference_bindings_json"],
                "provider_id": None,
                "model": "",
                "provider_task_id": "",
                "status": "completed",
                "is_current": False,
                "input_signature": keyframe_signature(
                    self._effective_shot_signature(current, self._project(current["project_id"])),
                    expected_bindings,
                    "uploaded",
                    "",
                    {},
                ),
                "source": "uploaded",
                "error": "",
            },
        )
        chapter_no, chapter_title = self._chapter_folder_info(current.get("chapter_id"))
        target = file_service.keyframe_path(
            current["project_id"],
            shot_id,
            version["version_no"],
            suffix,
            shot_no=current["shot_no"],
            storage_name=self._project_storage_name(current["project_id"]),
            chapter_no=chapter_no,
            chapter_title=chapter_title,
        )
        return self._finish_upload(
            kind="keyframe",
            version_id=version["id"],
            project_id=current["project_id"],
            target=target,
            data=data,
        )

    def upload_video(
        self,
        shot_id: str,
        filename: str,
        content_type: str,
        data: bytes,
    ) -> dict[str, Any]:
        del content_type
        if not data:
            raise bad_request("上传视频不能为空")
        if len(data) > MAX_VIDEO_SIZE:
            raise bad_request("视频大小超过限制（最大 100MB）")
        current = self._shot(shot_id)
        suffix = self._video_suffix(filename)
        with self.database.engine.connect() as connection:
            keyframe_id = connection.execute(
                select(keyframe_version.c.id).where(
                    keyframe_version.c.shot_id == shot_id,
                    keyframe_version.c.is_current.is_(True),
                )
            ).scalar()
        version = self._insert_version(
            table=video_version,
            parent_column="shot_id",
            parent_id=shot_id,
            values={
                "keyframe_version_id": keyframe_id,
                "file_path": None,
                "prompt_snapshot": str(
                    current.get("video_prompt_override") or current["video_prompt"] or ""
                ).strip(),
                "negative_prompt_snapshot": current["negative_prompt"],
                "requested_duration_sec": current["duration_sec"],
                "actual_duration_sec": None,
                "provider_id": None,
                "model": "",
                "provider_task_id": "",
                "status": "completed",
                "is_current": False,
                "input_signature": video_signature(
                    keyframe_id or "",
                    self._effective_video_prompt(current, self._project(current["project_id"])),
                    current["duration_sec"],
                    "uploaded",
                    "",
                    {},
                ),
                "source": "uploaded",
                "cost_estimate": 0,
                "error": "",
            },
        )
        chapter_no, chapter_title = self._chapter_folder_info(current.get("chapter_id"))
        target = file_service.video_path(
            current["project_id"],
            shot_id,
            version["version_no"],
            suffix,
            shot_no=current["shot_no"],
            storage_name=self._project_storage_name(current["project_id"]),
            chapter_no=chapter_no,
            chapter_title=chapter_title,
        )
        return self._finish_upload(
            kind="video",
            version_id=version["id"],
            project_id=current["project_id"],
            target=target,
            data=data,
        )

    def _media_version_storage_path(
        self,
        kind: MediaKind,
        record: dict[str, Any],
        *,
        current: bool,
    ) -> tuple[str, Path]:
        file_path = str(record.get("file_path") or "")
        suffix = Path(file_path).suffix
        if not suffix:
            raise FileServiceError("媒体文件缺少扩展名")
        if kind == "asset_image":
            parent = self._asset(record["asset_id"])
            return parent["project_id"], file_service.asset_image_path(
                parent["project_id"],
                parent["asset_type"],
                parent["id"],
                record["version_no"],
                suffix,
                asset_name=parent["name"],
                current=current,
                storage_name=self._project_storage_name(parent["project_id"]),
                view_key=self._asset_image_view_key(record),
            )
        parent = self._shot(record["shot_id"])
        chapter_no, chapter_title = self._chapter_folder_info(parent.get("chapter_id"))
        path_builder = file_service.keyframe_path if kind == "keyframe" else file_service.video_path
        provenance = json_loads(record.get("provenance_json"), {})
        storage_shot_id = parent["id"]
        if isinstance(provenance, Mapping) and provenance.get("workflow_run_id"):
            storage_shot_id = self._safe_storage_token(storage_shot_id)
        return parent["project_id"], path_builder(
            parent["project_id"],
            storage_shot_id,
            record["version_no"],
            suffix,
            shot_no=parent["shot_no"],
            current=current,
            storage_name=self._project_storage_name(parent["project_id"]),
            chapter_no=chapter_no,
            chapter_title=chapter_title,
        )

    def _asset_image_view_key(self, record: Mapping[str, Any]) -> str | None:
        """返回素材图所属 Scene View 的 view_key（用于文件分区目录）。

        旧版场景图 / 角色 / 道具没有 scene_view_id，返回 None（保持原路径）。
        """
        view_id = record.get("scene_view_id")
        if not view_id:
            return None
        from app.models.tables import scene_view as scene_view_table

        with self.database.engine.connect() as connection:
            return str(
                connection.execute(
                    select(scene_view_table.c.view_key).where(
                        scene_view_table.c.id == view_id
                    )
                ).scalar() or ""
            ) or None

    @staticmethod
    def _rollback_media_moves(
        project_id: str,
        moves: list[tuple[Path, Path]],
    ) -> None:
        errors: list[str] = []
        for source, target in reversed(moves):
            if not target.exists():
                continue
            try:
                file_service.move_file(project_id, target, source)
            except FileServiceError as exc:
                errors.append(str(exc))
        if errors:
            raise FileServiceError("媒体文件回滚失败：" + "；".join(errors))

    def _dynamic_activation_guard(
        self,
        connection: Any,
        job_record: Mapping[str, Any],
        input_data: Mapping[str, Any],
        version: Mapping[str, Any],
    ) -> dict[str, Any]:
        """在激活事务内读取并固定当前 Job/stage/run 的条件快照。"""
        run_id = str(
            job_record.get("workflow_run_id") or input_data.get("workflow_run_id") or ""
        )
        stage_id = str(
            job_record.get("workflow_stage_id") or input_data.get("workflow_stage_id") or ""
        )
        if not run_id or not stage_id:
            raise conflict("动态媒体任务缺少完整的 run/stage 关联")
        current_job = row_dict(
            connection.execute(select(job).where(job.c.id == job_record.get("id"))).first()
        )
        run = row_dict(
            connection.execute(select(workflow_run).where(workflow_run.c.id == run_id)).first()
        )
        stage = row_dict(
            connection.execute(
                select(workflow_stage).where(
                    workflow_stage.c.id == stage_id,
                    workflow_stage.c.workflow_run_id == run_id,
                )
            ).first()
        )
        if current_job is None or run is None or stage is None:
            raise conflict("动态媒体任务关联的 run/stage 不存在")
        if current_job.get("status") not in _ACTIVE_MEDIA_STATUSES:
            raise conflict("动态媒体 Job 已不处于可提交状态，拒绝激活旧结果")
        if (
            str(current_job.get("workflow_run_id") or "") != run_id
            or str(current_job.get("workflow_stage_id") or "") != stage_id
            or str(current_job.get("input_signature") or "")
            != str(job_record.get("input_signature") or "")
        ):
            raise conflict("动态媒体 Job 关联或 input_signature 已发生变化")

        latest = row_dict(
            connection.execute(
                select(workflow_stage)
                .where(
                    workflow_stage.c.workflow_run_id == run_id,
                    workflow_stage.c.stage_key == stage.get("stage_key"),
                )
                .order_by(
                    workflow_stage.c.attempt.desc(),
                    workflow_stage.c.updated_at.desc(),
                    workflow_stage.c.id.desc(),
                )
                .limit(1)
            ).first()
        )
        if latest is None or latest.get("id") != stage_id:
            raise conflict("动态媒体任务关联的 stage attempt 已过期，拒绝激活旧结果")
        provenance = json_loads(version.get("provenance_json"), {})
        expected_signature = str(
            input_data.get("workflow_stage_input_signature")
            or (
                provenance.get("upstream_input_signature", "")
                if isinstance(provenance, Mapping)
                else ""
            )
            or ""
        )
        if expected_signature and str(stage.get("input_signature") or "") != expected_signature:
            raise conflict("动态媒体任务的 stage 签名已变化，拒绝激活旧媒体")
        expected_attempt = input_data.get("workflow_stage_attempt") or (
            provenance.get("workflow_stage_attempt")
            if isinstance(provenance, Mapping)
            else None
        )
        if expected_attempt is not None and str(latest.get("attempt")) != str(expected_attempt):
            raise conflict("动态媒体任务的 stage attempt 已变化，拒绝激活旧媒体")
        if (
            str(run.get("status")) in _DYNAMIC_PROTECTED_STATUSES
            or str(stage.get("status")) in _DYNAMIC_PROTECTED_STATUSES
        ):
            raise conflict("动态媒体任务关联的 run/stage 已取消或过期，拒绝激活旧媒体")
        if (
            str(version.get("input_signature") or "")
            != str(current_job.get("input_signature") or "")
        ):
            raise conflict("动态媒体版本与 Job input_signature 不一致，拒绝激活旧媒体")
        return {
            "run_id": run_id,
            "stage_id": stage_id,
            "run_revision": run.get("revision"),
            "stage_key": stage.get("stage_key"),
            "stage_attempt": latest.get("attempt"),
            "stage_signature": stage.get("input_signature"),
        }

    def _media_view_condition(self, table: Any, record: Mapping[str, Any]):
        """asset_image 的 current 分区条件：按 (asset_id, scene_view_id) 分区。

        每个 Scene View（含 NULL=旧版场景图）各有自己的当前图；keyframe/video
        没有 scene_view_id 列，直接返回无条件 True（保持原单分区行为）。
        """
        if "scene_view_id" not in table.c:
            return None
        view_id = record.get("scene_view_id")
        if view_id is None:
            return table.c.scene_view_id.is_(None)
        return table.c.scene_view_id == view_id

    def _activate_media_version(
        self,
        kind: MediaKind,
        version_id: str,
        *,
        source_relative: str | None = None,
        complete: bool = False,
        actual_duration_sec: float | None = None,
        job_record: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        table, parent_column = _MEDIA_TABLES[kind]
        with _MEDIA_VERSION_FILE_LOCK:
            with self.database.engine.connect() as connection:
                record = row_dict(
                    connection.execute(select(table).where(table.c.id == version_id)).first()
                )
                if record is None:
                    raise not_found("媒体版本")
                if source_relative is not None:
                    record["file_path"] = source_relative
                if not record.get("file_path"):
                    raise FileServiceError("媒体版本缺少文件路径")
                if job_record is not None:
                    input_data = job_record.get("input_data")
                    if not isinstance(input_data, Mapping):
                        input_data = json_loads(job_record.get("input_json"), {})
                    self._validate_dynamic_media_state(job_record, input_data, record)
                view_condition = self._media_view_condition(table, record)
                old_current_statement = select(table).where(
                    table.c[parent_column] == record[parent_column],
                    table.c.is_current.is_(True),
                    table.c.id != version_id,
                )
                if view_condition is not None:
                    old_current_statement = old_current_statement.where(view_condition)
                old_current = row_dict(
                    connection.execute(old_current_statement).first()
                )

            project_id, current_target = self._media_version_storage_path(
                kind,
                record,
                current=True,
            )
            source = file_service.resolve_project_path(project_id, record["file_path"])
            moves: list[tuple[Path, Path]] = []
            old_history_relative: str | None = None
            try:
                if old_current and old_current.get("file_path"):
                    _, old_history_target = self._media_version_storage_path(
                        kind,
                        old_current,
                        current=False,
                    )
                    old_source = file_service.resolve_project_path(
                        project_id,
                        old_current["file_path"],
                    )
                    if old_source != old_history_target:
                        file_service.move_file(project_id, old_source, old_history_target)
                        moves.append((old_source, old_history_target))
                    old_history_relative = file_service.relative_project_path(
                        project_id,
                        old_history_target,
                    )
                if source != current_target:
                    file_service.move_file(project_id, source, current_target)
                    moves.append((source, current_target))
                if job_record is not None:
                    input_data = job_record.get("input_data")
                    if not isinstance(input_data, Mapping):
                        input_data = json_loads(job_record.get("input_json"), {})
                    self._validate_dynamic_media_state(job_record, input_data, record)
                current_relative = file_service.relative_project_path(
                    project_id,
                    current_target,
                )
                values: dict[str, Any] = {
                    "file_path": current_relative,
                    "is_current": True,
                }
                if complete:
                    values.update(status="completed", error="")
                if kind == "video" and complete:
                    values["actual_duration_sec"] = actual_duration_sec
                with self.database.engine.begin() as connection:
                    activation_guard = None
                    activation_job: Mapping[str, Any] | None = None
                    if job_record is not None:
                        activation_job = job_record
                        input_data = job_record.get("input_data")
                        if not isinstance(input_data, Mapping):
                            input_data = json_loads(job_record.get("input_json"), {})
                        activation_guard = self._dynamic_activation_guard(
                            connection,
                            activation_job,
                            input_data,
                            record,
                        )
                    current_update = update(table).where(
                        table.c[parent_column] == record[parent_column],
                        table.c.is_current.is_(True),
                    )
                    target_update = update(table).where(
                        table.c.id == version_id,
                        table.c[parent_column] == record[parent_column],
                    )
                    if view_condition is not None:
                        # 场景图按 Scene View 分区激活：只影响同一视图内的 current，
                        # 不触碰其他视图或旧版场景图（NULL 分区）。
                        current_update = current_update.where(view_condition)
                        target_update = target_update.where(view_condition)
                    if old_current is not None:
                        current_update = current_update.where(table.c.id == old_current["id"])
                    else:
                        no_current_statement = select(table.c.id).where(
                            table.c[parent_column] == record[parent_column],
                            table.c.is_current.is_(True),
                        )
                        if view_condition is not None:
                            no_current_statement = no_current_statement.where(view_condition)
                        target_update = target_update.where(~exists(no_current_statement))
                    if activation_guard is not None:
                        if activation_job is None:
                            raise FileServiceError("动态媒体激活缺少 Job 条件")
                        active_state = (
                            select(job.c.id)
                            .select_from(
                                job.join(
                                    workflow_stage,
                                    job.c.workflow_stage_id == workflow_stage.c.id,
                                ).join(
                                    workflow_run,
                                    job.c.workflow_run_id == workflow_run.c.id,
                                )
                            )
                            .where(
                                job.c.id == activation_job.get("id"),
                                job.c.status.in_(tuple(_ACTIVE_MEDIA_STATUSES)),
                                job.c.input_signature == activation_job.get("input_signature"),
                                workflow_stage.c.id == activation_guard["stage_id"],
                                workflow_stage.c.workflow_run_id == activation_guard["run_id"],
                                workflow_stage.c.attempt == activation_guard["stage_attempt"],
                                workflow_stage.c.input_signature
                                == activation_guard["stage_signature"],
                                workflow_stage.c.status.not_in(
                                    tuple(_DYNAMIC_PROTECTED_STATUSES)
                                ),
                                workflow_run.c.id == activation_guard["run_id"],
                                workflow_run.c.revision == activation_guard["run_revision"],
                                workflow_run.c.status.not_in(
                                    tuple(_DYNAMIC_PROTECTED_STATUSES)
                                ),
                                ~exists(
                                    select(workflow_stage.c.id).where(
                                        workflow_stage.c.workflow_run_id
                                        == activation_guard["run_id"],
                                        workflow_stage.c.stage_key
                                        == activation_guard["stage_key"],
                                        workflow_stage.c.attempt
                                        > activation_guard["stage_attempt"],
                                    )
                                ),
                            )
                            .exists()
                        )
                        current_update = current_update.where(active_state)
                        target_update = target_update.where(active_state)
                        target_update = target_update.where(
                            table.c.input_signature == activation_job.get("input_signature"),
                            table.c.status.in_(tuple(_ACTIVE_MEDIA_STATUSES)),
                            table.c.is_current.is_(False),
                        )
                    current_result = connection.execute(current_update.values(is_current=False))
                    if old_current and current_result.rowcount != 1:
                        raise FileServiceError("旧媒体 current 条件更新未命中")
                    if old_current and old_history_relative:
                        history_result = connection.execute(
                            update(table)
                            .where(table.c.id == old_current["id"])
                            .values(file_path=old_history_relative)
                        )
                        if history_result.rowcount != 1:
                            raise FileServiceError("旧媒体历史路径更新未命中")
                    activation_result = connection.execute(target_update.values(**values))
                    if activation_result.rowcount != 1:
                        # 手动与动态路径统一校验：条件更新未命中说明并发下状态已变化，
                        # 抛错以回滚已做的文件移动，避免文件与数据库状态错位。
                        raise FileServiceError("媒体版本激活条件未命中，结果已过期")
            except Exception as exc:
                try:
                    self._rollback_media_moves(project_id, moves)
                except FileServiceError as rollback_exc:
                    raise FileServiceError(f"媒体版本切换失败，且回滚失败：{rollback_exc}") from exc
                raise
        return self._get_version(kind, version_id)

    def _deactivate_media_version(
        self,
        kind: MediaKind,
        version_id: str,
    ) -> dict[str, Any]:
        table, _ = _MEDIA_TABLES[kind]
        with _MEDIA_VERSION_FILE_LOCK:
            with self.database.engine.connect() as connection:
                record = row_dict(
                    connection.execute(select(table).where(table.c.id == version_id)).first()
                )
            if record is None or record["error"] == _DELETED_MEDIA_ERROR:
                raise not_found("媒体版本")
            if not record["is_current"]:
                return self._get_version(kind, version_id)
            project_id, history_target = self._media_version_storage_path(
                kind,
                record,
                current=False,
            )
            source = file_service.resolve_project_path(project_id, record["file_path"])
            moved = source != history_target
            try:
                if moved:
                    file_service.move_file(project_id, source, history_target)
                history_relative = file_service.relative_project_path(
                    project_id,
                    history_target,
                )
                with self.database.engine.begin() as connection:
                    connection.execute(
                        update(table)
                        .where(table.c.id == version_id)
                        .values(file_path=history_relative, is_current=False)
                    )
            except Exception as exc:
                if moved and history_target.exists():
                    try:
                        file_service.move_file(project_id, history_target, source)
                    except FileServiceError as rollback_exc:
                        raise FileServiceError(
                            f"取消当前版本失败，且回滚失败：{rollback_exc}"
                        ) from exc
                raise
        return self._get_version(kind, version_id)

    def _finish_upload(
        self,
        *,
        kind: MediaKind,
        version_id: str,
        project_id: str,
        target: Path,
        data: bytes,
    ) -> dict[str, Any]:
        table, _ = _MEDIA_TABLES[kind]
        relative = file_service.relative_project_path(project_id, target)
        working_target = file_service.resolve_project_path(
            project_id,
            Path("temp")
            / "media-results"
            / f"{self._safe_storage_token(version_id)}{target.suffix}",
        )
        working_relative = file_service.relative_project_path(project_id, working_target)
        target_created = False
        try:
            file_service.write_bytes_atomic(project_id, working_relative, data)
            file_service.move_file(project_id, working_target, target)
            target_created = True
            actual_duration_sec = self._verify_video(target) if kind == "video" else None
            activated = self._activate_media_version(
                kind,
                version_id,
                source_relative=relative,
                complete=True,
                actual_duration_sec=actual_duration_sec,
            )
        except Exception:
            self._delete_failed_target(project_id, working_target)
            if target_created:
                self._delete_failed_target(project_id, target)
            with self.database.engine.begin() as connection:
                connection.execute(delete(table).where(table.c.id == version_id))
            raise
        if kind == "asset_image":
            ShotService(self.database, self.jobs).sync_asset_image_binding(
                activated["asset_id"],
                version_id,
            )
        if kind == "video":
            # 手动上传的视频同样提取末帧，保证下一镜可引用
            end_frame_working: Path | None = None
            try:
                current_video = file_service.resolve_project_path(
                    project_id,
                    activated["file_path"],
                )
                end_frame_target = current_video.with_name(f"{current_video.stem}-end.jpg")
                end_frame_working = file_service.resolve_project_path(
                    project_id,
                    Path("temp")
                    / "media-results"
                    / f"{self._safe_storage_token(version_id)}-end.jpg",
                )
                end_frame_working.parent.mkdir(parents=True, exist_ok=True)
                self._extract_last_frame(current_video, end_frame_working)
                file_service.move_file(project_id, end_frame_working, end_frame_target)
                end_frame_relative = file_service.relative_project_path(
                    project_id,
                    end_frame_target,
                )
                with self.database.engine.begin() as connection:
                    connection.execute(
                        update(video_version)
                        .where(video_version.c.id == version_id)
                        .values(end_frame_path=end_frame_relative)
                    )
            except Exception as frame_exc:
                if end_frame_working is not None:
                    self._delete_failed_target(project_id, end_frame_working)
                logger.warning(
                    "上传视频提取末帧失败，下一镜将不引用该帧：version=%s error=%s",
                    version_id,
                    frame_exc,
                )
        return activated

    def _get_version(self, kind: MediaKind, version_id: str) -> dict[str, Any]:
        table, parent_column = _MEDIA_TABLES[kind]
        with self.database.engine.connect() as connection:
            record = row_dict(
                connection.execute(select(table).where(table.c.id == version_id)).first()
            )
            if record is None or record["error"] == _DELETED_MEDIA_ERROR:
                raise not_found("媒体版本")
            if kind == "asset_image":
                project_id = connection.execute(
                    select(asset.c.project_id).where(asset.c.id == record[parent_column])
                ).scalar()
            else:
                project_id = connection.execute(
                    select(shot.c.project_id).where(shot.c.id == record[parent_column])
                ).scalar()
        if not project_id:
            raise not_found("媒体所属项目")
        return self._decode_version(record, project_id)

    def set_current(self, kind: MediaKind, version_id: str) -> dict[str, Any]:
        table, _ = _MEDIA_TABLES[kind]
        with self.database.engine.connect() as connection:
            record = row_dict(
                connection.execute(select(table).where(table.c.id == version_id)).first()
            )
            if record is None or record["error"] == _DELETED_MEDIA_ERROR:
                raise not_found("媒体版本")
            if record["status"] != "completed" or not record.get("file_path"):
                raise conflict("只有已完成且文件存在的版本可以设为当前版本")
        activated = self._activate_media_version(kind, version_id)
        if kind == "asset_image":
            ShotService(self.database, self.jobs).sync_asset_image_binding(
                activated["asset_id"],
                version_id,
            )
        return activated

    def unset_current(self, kind: MediaKind, version_id: str) -> dict[str, Any]:
        return self._deactivate_media_version(kind, version_id)

    def delete_version(self, kind: MediaKind, version_id: str) -> None:
        table, parent_column = _MEDIA_TABLES[kind]
        # 与 _activate/_deactivate_media_version 共用同一把锁并做乐观条件更新，
        # 避免与「设置当前版本/任务完成激活」并发时把刚激活的 current 版本删掉。
        with _MEDIA_VERSION_FILE_LOCK:
            with self.database.engine.begin() as connection:
                record = row_dict(
                    connection.execute(select(table).where(table.c.id == version_id)).first()
                )
                if record is None or record["error"] == _DELETED_MEDIA_ERROR:
                    raise not_found("媒体版本")
                if record["is_current"]:
                    raise conflict("当前媒体版本不能直接删除，请先切换其他版本")
                if record["status"] in _ACTIVE_MEDIA_STATUSES:
                    raise conflict("正在执行的媒体版本不能删除，请等待任务结束")
                if kind == "keyframe":
                    self._ensure_keyframe_not_referenced(connection, version_id)
                if kind == "asset_image":
                    project_id = connection.execute(
                        select(asset.c.project_id).where(asset.c.id == record[parent_column])
                    ).scalar()
                else:
                    project_id = connection.execute(
                        select(shot.c.project_id).where(shot.c.id == record[parent_column])
                    ).scalar()
                values: dict[str, Any] = {
                    "file_path": None,
                    "status": "failed",
                    "is_current": False,
                    "error": _DELETED_MEDIA_ERROR,
                }
                if kind == "video":
                    values["end_frame_path"] = None
                # 乐观条件：仅当版本仍处于读取时的非激活、非执行状态时才删除，
                # 防止读取后被 set_current/任务完成激活的版本被误删。
                result = connection.execute(
                    update(table)
                    .where(
                        table.c.id == version_id,
                        table.c.is_current.is_(False),
                        table.c.status.not_in(_ACTIVE_MEDIA_STATUSES),
                    )
                    .values(**values)
                )
                if result.rowcount != 1:
                    raise conflict("媒体版本状态已变化，请刷新后重试")
                if project_id and record.get("file_path"):
                    file_service.delete_file(project_id, record["file_path"])
                if kind == "video" and project_id and record.get("end_frame_path"):
                    file_service.delete_file(project_id, record["end_frame_path"])

    @staticmethod
    def _ensure_keyframe_not_referenced(connection: Any, keyframe_id: str) -> None:
        video_rows = connection.execute(
            select(video_version.c.id, video_version.c.status).where(
                video_version.c.keyframe_version_id == keyframe_id,
                video_version.c.error != _DELETED_MEDIA_ERROR,
            )
        ).all()
        if any(row.status in _ACTIVE_MEDIA_STATUSES for row in video_rows):
            raise conflict("关键帧仍被生成中的视频引用，不能删除")

        video_ids = {row.id for row in video_rows}
        if video_ids:
            active_video_job = connection.execute(
                select(job.c.id)
                .where(
                    job.c.target_type == "video",
                    job.c.target_id.in_(video_ids),
                    job.c.status.in_(_ACTIVE_MEDIA_STATUSES),
                )
                .limit(1)
            ).first()
            if active_video_job:
                raise conflict("关键帧仍被活动视频任务引用，不能删除")

        # 按项目收敛扫描范围：关键帧所属分镜的项目，避免全表扫描所有分镜。
        project_id = connection.execute(
            select(shot.c.project_id)
            .select_from(keyframe_version)
            .join(shot, shot.c.id == keyframe_version.c.shot_id)
            .where(keyframe_version.c.id == keyframe_id)
        ).scalar()
        project_statement = select(shot.c.reference_bindings_json)
        if project_id is not None:
            project_statement = project_statement.where(shot.c.project_id == project_id)
        for raw_bindings in connection.scalars(project_statement):
            bindings = json_loads(raw_bindings, [])
            if not isinstance(bindings, list):
                continue
            if any(
                isinstance(binding, dict)
                and binding.get("role") == "previous_keyframe"
                and binding.get("keyframe_id") == keyframe_id
                for binding in bindings
            ):
                raise conflict("关键帧仍被后续分镜引用，不能删除")

    def resolve_video_download(self, video_id: str) -> Path:
        record = self._get_version("video", video_id)
        if record["status"] != "completed" or not record.get("file_path"):
            raise conflict("视频尚未完成")
        current = self._shot(record["shot_id"])
        path = file_service.resolve_project_path(current["project_id"], record["file_path"])
        if not path.is_file():
            raise not_found("视频文件")
        return path

    def export_current_videos(self, project_id: str) -> Path:
        self._project(project_id)
        with self.database.engine.connect() as connection:
            records = connection.execute(
                select(
                    video_version.c.file_path,
                    shot.c.shot_no,
                    shot.c.id.label("shot_id"),
                )
                .join(shot, shot.c.id == video_version.c.shot_id)
                .where(
                    shot.c.project_id == project_id,
                    video_version.c.is_current.is_(True),
                    video_version.c.status == "completed",
                )
                .order_by(shot.c.shot_order)
            ).all()
        if not records:
            raise conflict("当前项目没有可导出的当前视频")
        # 流式写出 zip：直接写临时文件而非整体驻留内存 BytesIO，
        # 避免多视频（单个最大 100MB）导出时 OOM。
        relative = f"export/current-videos-{utc_now().replace(':', '-')}.zip"
        target = file_service.resolve_project_path(project_id, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp_zip = target.with_name(f"{target.stem}.part")
        wrote_any = False
        try:
            with zipfile.ZipFile(temp_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for record in records:
                    path = file_service.resolve_project_path(project_id, record.file_path)
                    if path.is_file():
                        archive.write(
                            path,
                            arcname=f"{record.shot_no:03d}_{record.shot_id}{path.suffix.lower()}",
                        )
                        wrote_any = True
            if not wrote_any:
                raise conflict("当前视频文件均不存在，无法导出")
            os.replace(temp_zip, target)
        finally:
            temp_zip.unlink(missing_ok=True)
        # 清理历史导出的时间戳 zip：该目录会被多次导出反复堆积，
        # 保留本次结果、删除其余 current-videos-*.zip 即可。
        try:
            for stale in target.parent.glob("current-videos-*.zip"):
                if stale != target:
                    stale.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("清理历史导出 zip 失败：%s", exc)
        return target

    async def handle_asset_image(self, job_record: dict[str, Any]) -> dict[str, Any]:
        return await self._handle_image_job(job_record, kind="asset_image")

    async def handle_keyframe(self, job_record: dict[str, Any]) -> dict[str, Any]:
        return await self._handle_image_job(job_record, kind="keyframe")

    async def _handle_image_job(
        self, job_record: dict[str, Any], *, kind: Literal["asset_image", "keyframe"]
    ) -> dict[str, Any]:
        table = asset_image if kind == "asset_image" else keyframe_version
        version_id = job_record["target_id"]
        version = self._get_version(kind, version_id)
        project_id = ""
        target: Path | None = None
        working_target: Path | None = None
        target_created = False
        temporary_references: list[str] = []
        input_data: dict[str, Any] = {}
        try:
            raw_input = job_record.get("input_data")
            input_data = (
                dict(raw_input)
                if isinstance(raw_input, Mapping)
                else json_loads(job_record.get("input_json"), {})
            )
            await self._wait_for_job_dependencies(job_record)
            version = self._get_version(kind, version_id)
            self._validate_dynamic_media_state(job_record, input_data, version)
            if kind == "asset_image":
                current = self._asset(version["asset_id"])
                project_id = current["project_id"]
                # 每项只 resolve 一次；参考图绑定存在但文件缺失/失效时不允许静默降级，
                # 与关键帧路径同口径：直接失败，避免提示词参考数量与实际输入不一致。
                reference_items = list(version["reference_files_json"] or [])
                references = []
                missing_references: list[str] = []
                for item in reference_items:
                    resolved_reference = file_service.resolve_project_path(project_id, item)
                    if resolved_reference.is_file():
                        references.append(resolved_reference)
                    else:
                        missing_references.append(str(item))
                if reference_items and missing_references:
                    raise conflict(
                        "素材图参考文件缺失或失效（素材已绑定参考图，但磁盘文件不存在或"
                        "记录无法解析），请先在素材库重新生成或上传图片后重试"
                    )
                target = file_service.asset_image_path(
                    project_id,
                    current["asset_type"],
                    current["id"],
                    version["version_no"],
                    asset_name=current["name"],
                    storage_name=self._project_storage_name(project_id),
                    view_key=self._asset_image_view_key(version),
                )
            else:
                current = self._shot(version["shot_id"])
                project_id = current["project_id"]
                if self._is_dynamic_media_input(input_data):
                    references, temporary_references = await self._dynamic_reference_files(
                        project_id,
                        str(job_record["id"]),
                        input_data,
                    )
                    expected_references = self._prompt_reference_count(
                        str(version.get("prompt_snapshot") or "")
                    )
                    if expected_references and len(references) < expected_references:
                        raise conflict(
                            "动态关键帧参考图输入不完整，提示词中的 @Image 编号无法对应"
                        )
                else:
                    bound_items = self._binding_reference_items(
                        project_id,
                        version.get("reference_bindings") or [],
                    )
                    references = [path for path, _ in bound_items]
                    if (version.get("reference_bindings") or []) and len(bound_items) < len(
                        version.get("reference_bindings") or []
                    ):
                        # 参考图绑定存在但文件缺失/记录失效：不允许静默降级（含部分缺失）
                        raise conflict(
                            "关键帧参考图文件缺失或失效（分镜已绑定素材图，但磁盘文件不存在或"
                            "记录无法解析），请先在素材库重新生成或上传图片后重试"
                        )
                chapter_no, chapter_title = self._chapter_folder_info(current.get("chapter_id"))
                target = file_service.keyframe_path(
                    project_id,
                    self._safe_storage_token(current["id"])
                    if self._is_dynamic_media_input(input_data)
                    else current["id"],
                    version["version_no"],
                    shot_no=current["shot_no"],
                    storage_name=self._project_storage_name(project_id),
                    chapter_no=chapter_no,
                    chapter_title=chapter_title,
                )
            working_target = file_service.resolve_project_path(
                project_id,
                Path("temp")
                / "media-results"
                / f"{self._safe_storage_token(version_id)}{target.suffix}",
            )
            provider, config = self.providers.resolve(
                kind="image",
                provider_id=version["provider_id"],
            )
            target_type = current["asset_type"] if kind == "asset_image" else "keyframe"
            request_params = self._image_generation_params(
                config["model"],
                target_type,
                input_data.get("params", {}),
                provider_config=config,
            )
            params = {**config["params"], **request_params}
            self._set_media_status(table, version_id, "running")
            if self._has_saved_result(job_record):
                result = ProviderPollResult.model_validate(job_record["result_data"])
            elif job_record.get("provider_task_id") or version["provider_task_id"]:
                result = await self._poll_existing(
                    provider,
                    job_record,
                    table,
                    version_id,
                    job_record.get("provider_task_id") or version["provider_task_id"],
                    config["timeout_sec"],
                )
            else:
                submission = await provider.submit(
                    prompt=version["prompt_snapshot"],
                    negative_prompt=version["negative_prompt_snapshot"],
                    reference_files=references,
                    params=params,
                )
                result = await self._await_provider_result(
                    provider,
                    job_record,
                    table,
                    version_id,
                    submission,
                    config["timeout_sec"],
                )
            await self._download_provider_result(provider, result, working_target)
            self._verify_image(working_target.read_bytes())
            file_service.move_file(project_id, working_target, target)
            target_created = True
            relative = file_service.relative_project_path(project_id, target)
            self._validate_dynamic_media_state(job_record, input_data, version)
            completed = self._complete_media(
                table,
                version_id,
                relative,
                job_record=job_record,
            )
            self._store_provider_url(table, version_id, result.result_url)
            self._delete_staged_provider_result(project_id, result)
            self._cleanup_dynamic_reference_files(project_id, temporary_references)
            completed_result = {"version_id": version_id, "file_path": completed["file_path"]}
            if self._complete_dynamic_media_job(job_record, input_data, completed_result):
                self._enqueue_dynamic_dependents(str(job_record["id"]))
            return completed_result
        except Exception as exc:
            self._delete_failed_target(project_id, working_target)
            if target_created:
                self._delete_failed_target(project_id, target)
            self._cleanup_dynamic_reference_files(project_id, temporary_references)
            self._set_media_status(table, version_id, "failed", str(exc))
            self._record_dynamic_media_failure(job_record, input_data, exc)
            if isinstance(exc, MediaDependencyError):
                self.jobs.repository.fail(str(job_record["id"]), str(exc))
                return {
                    "status": "failed",
                    "version_id": version_id,
                    "error": sanitize_error_message(exc),
                }
            raise

    def _resolve_binding_files(self, project_id: str, bindings: list[dict[str, Any]]) -> list[Path]:
        return [path for path, _ in self._binding_reference_items(project_id, bindings)]

    @staticmethod
    def _uses_h3_video_protocol(config: Mapping[str, Any]) -> bool:
        params = config.get("params")
        params = params if isinstance(params, Mapping) else {}
        protocol = str(params.get("protocol", "")).strip().lower()
        if protocol:
            return protocol == "h3"
        hostname = (urlsplit(str(config.get("base_url") or "")).hostname or "").lower()
        return hostname == "h3.zhongbiao.cloud"

    @staticmethod
    def _ref2va_dialogue_lines(
        input_data: Mapping[str, Any],
        source: str,
    ) -> list[tuple[str, str] | tuple[str, str, tuple[float, float]]]:
        """提取 Ref2VA 改写所需对白：动态链路优先结构化 cues，旧链路回退文本解析。"""

        plan = input_data.get("plan")
        bundle = plan.get("prompt_bundle") if isinstance(plan, Mapping) else None
        semantic = bundle.get("semantic_plan") if isinstance(bundle, Mapping) else None
        cues = semantic.get("cues") if isinstance(semantic, Mapping) else None
        dialogue = cues.get("dialogue") if isinstance(cues, Mapping) else None
        lines: list[tuple[str, str] | tuple[str, str, tuple[float, float]]] = []
        if isinstance(dialogue, list):
            for item in dialogue:
                if not isinstance(item, Mapping):
                    continue
                speaker = str(item.get("speaker") or "").strip()
                text = str(item.get("text") or "").strip()
                if not text:
                    continue
                start = item.get("start_time")
                end = item.get("end_time")
                if isinstance(start, int | float) and isinstance(end, int | float):
                    lines.append((speaker, text, (float(start), float(end))))
                else:
                    lines.append((speaker, text))
        if lines:
            return lines
        return list(extract_dialogue_lines(source))

    async def _ref2va_video_prompt(
        self,
        *,
        source: str,
        duration_sec: float,
        dialogue_lines: Sequence[tuple[str, str] | tuple[str, str, tuple[float, float]]],
    ) -> str:
        """H3 原生六段改写；改写或校验失败时回退原提示词，不阻塞视频生成。"""

        if not source.strip():
            return source
        try:
            request = build_ref2va_rewrite_request(
                source,
                dialogue_lines=dialogue_lines,
                duration_sec=duration_sec,
            )
            text_provider, text_config = self.providers.resolve(kind="text", provider_id=None)
            text_params = dict(text_config.get("params") or {})
            text_params.setdefault("reasoning_effort", "medium")
            output = await text_provider.generate_json(
                system_prompt=request.system_prompt,
                user_prompt=request.user_payload,
                schema=Ref2VATextOutput,
                params=text_params,
            )
            rewritten = str(getattr(output, "prompt", "") or "").strip()
            errors = validate_ref2va_sections(
                rewritten,
                expect_dialogue=bool(dialogue_lines),
                expect_dialogue_count=len(dialogue_lines) if dialogue_lines else None,
                expected_dialogue_texts=[item[1] for item in dialogue_lines],
                expected_duration_sec=duration_sec,
            )
            if errors:
                logger.warning(
                    "H3 Ref2VA 改写校验失败，回退原提示词：%s",
                    "；".join(errors[:3]),
                )
                return source
            return rewritten
        except Exception:
            # 改写是可选的增强出口：任何失败都退回原提示词并保留完整堆栈日志，
            # 不能因此中断视频生成。
            logger.exception("H3 Ref2VA 改写失败，回退原提示词")
            return source

    async def handle_video(self, job_record: dict[str, Any]) -> dict[str, Any]:
        version_id = job_record["target_id"]
        version = self._get_version("video", version_id)
        project_id = ""
        target: Path | None = None
        working_target: Path | None = None
        target_created = False
        temporary_references: list[str] = []
        input_data: dict[str, Any] = {}
        try:
            current = self._shot(version["shot_id"])
            project_id = current["project_id"]
            raw_input = job_record.get("input_data")
            input_data = (
                dict(raw_input)
                if isinstance(raw_input, Mapping)
                else json_loads(job_record.get("input_json"), {})
            )
            # 视频只能在关键帧和上一镜视频依赖成功后读取任何媒体路径；否则
            # 失败依赖也会被误读成文件缺失，甚至提前触发 Provider。
            await self._wait_for_job_dependencies(job_record)
            self._validate_dynamic_media_state(job_record, input_data, version)
            # 依赖完成后重新读取关键帧版本，避免等待期间完成的新文件仍被旧快照遮蔽。
            keyframe = self._get_version("keyframe", version["keyframe_version_id"])
            version = self._get_version("video", version_id)
            self._validate_dynamic_media_state(job_record, input_data, version)
            dynamic_input = self._is_dynamic_media_input(input_data)
            dynamic_paths: list[Path] = []
            legacy_reference_sources: list[dict[str, Any]] = []
            if dynamic_input:
                dynamic_paths, temporary_references = await self._dynamic_reference_files(
                    project_id,
                    str(job_record["id"]),
                    input_data,
                )
            else:
                bindings = input_data.get("reference_bindings")
                if not isinstance(bindings, list):
                    bindings = current["reference_bindings"]
                if bindings:
                    legacy_reference_sources = self._binding_reference_sources(
                        project_id,
                        bindings,
                    )
                    if len(legacy_reference_sources) < len(bindings):
                        raise conflict(
                            "视频参考图文件缺失或失效（分镜已绑定素材图，但磁盘文件不存在或"
                            "记录无法解析），请先在素材库重新生成或上传图片后重试"
                        )
            if not keyframe.get("file_path"):
                raise conflict("视频依赖的关键帧缺少文件路径")
            keyframe_path = file_service.resolve_project_path(project_id, keyframe["file_path"])
            if not keyframe_path.is_file() or keyframe_path.stat().st_size <= 0:
                raise conflict("视频依赖的当前关键帧文件不存在或为空")
            provider, config = self.providers.resolve(
                kind="video",
                provider_id=version["provider_id"],
            )
            params = {**config["params"], **input_data.get("params", {})}
            chapter_no, chapter_title = self._chapter_folder_info(current.get("chapter_id"))
            target = file_service.video_path(
                project_id,
                self._safe_storage_token(current["id"])
                if dynamic_input
                else current["id"],
                version["version_no"],
                shot_no=current["shot_no"],
                storage_name=self._project_storage_name(project_id),
                chapter_no=chapter_no,
                chapter_title=chapter_title,
            )
            working_target = file_service.resolve_project_path(
                project_id,
                Path("temp")
                / "media-results"
                / f"{self._safe_storage_token(version_id)}{target.suffix}",
            )
            self._set_media_status(video_version, version_id, "running")
            if self._has_saved_result(job_record):
                result = ProviderPollResult.model_validate(job_record["result_data"])
            elif job_record.get("provider_task_id") or version["provider_task_id"]:
                result = await self._poll_existing(
                    provider,
                    job_record,
                    video_version,
                    version_id,
                    job_record.get("provider_task_id") or version["provider_task_id"],
                    config["timeout_sec"],
                )
            else:
                supports_multiple_input_images = input_data.get("supports_multiple_input_images")
                runtime_supports_multiple = self._supports_multiple_video_inputs(provider)
                if supports_multiple_input_images is None:
                    supports_multiple_input_images = runtime_supports_multiple
                if supports_multiple_input_images and not runtime_supports_multiple:
                    raise RuntimeError("视频 Provider 当前不支持任务所需的多参考图输入")
                prepare_multiple = getattr(provider, "prepare_input_images", None)
                prefer_public_urls = bool(
                    getattr(provider, "prefers_public_reference_urls", False)
                )
                if dynamic_input:
                    all_input_paths = [keyframe_path, *dynamic_paths]
                    expected_references = self._prompt_reference_count(
                        str(version.get("prompt_snapshot") or "")
                    )
                    if expected_references and len(all_input_paths) < expected_references:
                        raise conflict(
                            "动态视频参考图输入不完整，提示词中的 @Image 编号无法对应"
                        )
                    if len(all_input_paths) > 1 and not supports_multiple_input_images:
                        raise RuntimeError("动态视频任务需要 Provider 支持多参考图输入")
                    if len(all_input_paths) > 1:
                        if prepare_multiple is None:
                            raise RuntimeError("视频 Provider 缺少多参考图准备方法")
                        prepared = await prepare_multiple(all_input_paths)
                    else:
                        prepared = await provider.prepare_input_image(keyframe_path)
                elif supports_multiple_input_images:
                    bindings = input_data.get("reference_bindings")
                    if not isinstance(bindings, list):
                        bindings = current["reference_bindings"]
                    reference_sources = legacy_reference_sources or self._binding_reference_sources(
                        project_id,
                        bindings,
                    )
                    reference_paths = [Path(item["path"]) for item in reference_sources]
                    if bindings and len(reference_paths) < len(bindings):
                        # 参考图绑定存在但文件缺失/记录失效：不允许静默降级（含部分缺失）
                        raise conflict(
                            "视频参考图文件缺失或失效（分镜已绑定素材图，但磁盘文件不存在或"
                            "记录无法解析），请先在素材库重新生成或上传图片后重试"
                        )

                    # 图片顺序必须与 queue_video 写进 prompt 的 @Image 编号一一对应：
                    # 自由制作连续承接时为「上一镜结束帧 → 关键帧 → 各素材绑定」，
                    # 其他分镜保持「关键帧 → 上一镜结束帧（可选）→ 各素材绑定」。
                    prev_end_frame_path = input_data.get("prev_end_frame_path")
                    resolved_prev: Path | None = None
                    if prev_end_frame_path:
                        resolved_prev = self._readable_project_file(
                            project_id,
                            str(prev_end_frame_path),
                        )
                        if resolved_prev is None:
                            # prompt 已快照且声明了该图，缺图会让后续 @Image 编号整体错位，
                            # 与素材绑定缺失同样按失败处理，不静默降级
                            raise conflict(
                                "上一镜结束帧文件缺失或失效，提示词中的参考图编号将无法对应。"
                                "请重新生成上一镜视频，或重新生成本镜视频提示词后重试"
                            )
                    all_input_paths: list[Path] = []
                    all_input_urls: list[str] = []
                    if resolved_prev is not None and input_data.get(
                        "prev_end_frame_first"
                    ):
                        all_input_paths.append(resolved_prev)
                        all_input_urls.append("")
                    all_input_paths.append(keyframe_path)
                    all_input_urls.append(str(keyframe.get("provider_url") or ""))
                    if resolved_prev is not None and not input_data.get(
                        "prev_end_frame_first"
                    ):
                        all_input_paths.append(resolved_prev)
                        all_input_urls.append("")
                    all_input_paths.extend(reference_paths)
                    all_input_urls.extend(str(item.get("url") or "") for item in reference_sources)
                    if prefer_public_urls:
                        values: list[str] = []
                        for path, url in zip(
                            all_input_paths,
                            all_input_urls,
                            strict=True,
                        ):
                            if self._provider_url_usable(url):
                                values.append(url)
                            else:
                                values.append(
                                    (await provider.prepare_input_image(path))["value"]
                                )
                        prepared = {"values": values, "mode": "url"}
                    else:
                        if prepare_multiple is None:
                            raise RuntimeError("视频 Provider 缺少多参考图准备方法")
                        prepared = await prepare_multiple(all_input_paths)
                else:
                    prepared = await provider.prepare_input_image(keyframe_path)
                submit_prompt = str(version["prompt_snapshot"] or "")
                if self._uses_h3_video_protocol(config):
                    submit_prompt = await self._ref2va_video_prompt(
                        source=submit_prompt,
                        duration_sec=float(version["requested_duration_sec"]),
                        dialogue_lines=self._ref2va_dialogue_lines(input_data, submit_prompt),
                    )
                submission = await provider.submit(
                    prompt=submit_prompt,
                    input_image=prepared,
                    duration_sec=version["requested_duration_sec"],
                    params=params,
                )
                result = await self._await_provider_result(
                    provider,
                    job_record,
                    video_version,
                    version_id,
                    submission,
                    config["timeout_sec"],
                )
            self._set_media_status(video_version, version_id, "downloading")
            self.jobs.repository.set_status(
                job_record["id"], "downloading", message="正在下载视频结果"
            )
            await self._download_provider_result(provider, result, working_target)
            if not working_target.is_file() or working_target.stat().st_size <= 0:
                raise FileServiceError("视频文件为空")
            # 同步 ffprobe 校验放入 FFmpeg 线程池，避免阻塞事件循环
            actual_duration_sec = await run_in_ffmpeg_executor(
                self._verify_video, working_target
            )
            file_service.move_file(project_id, working_target, target)
            target_created = True
            relative = file_service.relative_project_path(project_id, target)
            self._validate_dynamic_media_state(job_record, input_data, version)
            completed = self._complete_media(
                video_version,
                version_id,
                relative,
                actual_duration_sec=actual_duration_sec,
                job_record=job_record,
            )

            # 提取视频末帧，供下一镜衔接引用。失败不影响本次视频生成成功
            end_frame_working: Path | None = None
            try:
                # 与视频同目录同名，仅扩展名不同：自动跟随项目存储布局与章节分目录，
                # 激活后文件可能已被移动到 current 路径，须以 completed.file_path 为准
                current_video = file_service.resolve_project_path(
                    project_id,
                    completed["file_path"],
                )
                end_frame_target = current_video.with_name(f"{current_video.stem}-end.jpg")
                end_frame_working = file_service.resolve_project_path(
                    project_id,
                    Path("temp")
                    / "media-results"
                    / f"{self._safe_storage_token(version_id)}-end.jpg",
                )
                end_frame_working.parent.mkdir(parents=True, exist_ok=True)
                # 同步 ffmpeg 提取放入 FFmpeg 线程池，避免阻塞事件循环
                await run_in_ffmpeg_executor(
                    self._extract_last_frame,
                    current_video,
                    end_frame_working,
                )
                file_service.move_file(project_id, end_frame_working, end_frame_target)
                end_frame_relative = file_service.relative_project_path(
                    project_id,
                    end_frame_target,
                )
                with self.database.engine.begin() as connection:
                    connection.execute(
                        update(video_version)
                        .where(video_version.c.id == version_id)
                        .values(end_frame_path=end_frame_relative)
                    )
            except Exception as frame_exc:
                if end_frame_working is not None:
                    self._delete_failed_target(project_id, end_frame_working)
                logger.warning(
                    "提取视频末帧失败，下一镜将不引用该帧：version=%s error=%s",
                    version_id,
                    frame_exc,
                )

            self._delete_staged_provider_result(project_id, result)
            self._cleanup_dynamic_reference_files(project_id, temporary_references)
            completed_result = {"version_id": version_id, "file_path": completed["file_path"]}
            if self._complete_dynamic_media_job(job_record, input_data, completed_result):
                self._enqueue_dynamic_dependents(str(job_record["id"]))
            return completed_result
        except ProviderPollTimeoutError as exc:
            # 轮询超时：上游任务可能仍在生成，不按最终失败处理。
            # version 保留 provider_task_id 置回 waiting_provider；job 置
            # interrupted（可手动重试，重试走 _poll_existing 继续轮询原任务，
            # 不会重新提交）。动态 stage/run 照常汇总为 partial_failed，
            # 由流水线的恢复流程或 Job 面板的重试按钮继续推进。
            self._cleanup_dynamic_reference_files(project_id, temporary_references)
            self._set_media_status(video_version, version_id, "waiting_provider")
            self._record_dynamic_media_failure(job_record, input_data, exc)
            self.jobs.repository.interrupt(str(job_record["id"]), str(exc))
            return {
                "status": "interrupted",
                "version_id": version_id,
                "error": sanitize_error_message(exc),
            }
        except Exception as exc:
            self._delete_failed_target(project_id, working_target)
            if target_created:
                self._delete_failed_target(project_id, target)
            self._cleanup_dynamic_reference_files(project_id, temporary_references)
            self._set_media_status(video_version, version_id, "failed", str(exc))
            self._record_dynamic_media_failure(job_record, input_data, exc)
            if isinstance(exc, MediaDependencyError):
                self.jobs.repository.fail(str(job_record["id"]), str(exc))
                return {
                    "status": "failed",
                    "version_id": version_id,
                    "error": sanitize_error_message(exc),
                }
            raise

    async def _await_provider_result(
        self,
        provider: Any,
        job_record: dict[str, Any],
        table: Any,
        version_id: str,
        submission: Any,
        timeout_sec: int,
    ) -> ProviderPollResult:
        self._raise_if_job_cancelled(job_record["id"])
        task_id = submission.task_id
        if task_id:
            self._set_media_status(table, version_id, "waiting_provider", provider_task_id=task_id)
            self.jobs.repository.set_provider_task(
                job_record["id"], task_id, status="waiting_provider"
            )
        if submission.status == "completed":
            result = ProviderPollResult(
                task_id=task_id,
                status="completed",
                **submission.result,
            )
            return self._save_provider_result(job_record["id"], result)
        if not task_id:
            raise RuntimeError(submission.message or "Provider 未返回任务 ID")
        return await self._poll_existing(
            provider,
            job_record,
            table,
            version_id,
            task_id,
            timeout_sec,
        )

    async def _poll_existing(
        self,
        provider: Any,
        job_record: dict[str, Any],
        table: Any,
        version_id: str,
        task_id: str,
        timeout_sec: int,
    ) -> ProviderPollResult:
        self._raise_if_job_cancelled(job_record["id"])
        self._set_media_status(table, version_id, "waiting_provider", provider_task_id=task_id)
        self.jobs.repository.set_provider_task(job_record["id"], task_id, status="waiting_provider")
        try:
            async with asyncio.timeout(timeout_sec):
                while True:
                    self._raise_if_job_cancelled(job_record["id"])
                    result = await provider.poll(task_id)
                    if result.status == "completed":
                        return self._save_provider_result(job_record["id"], result)
                    if result.status == "failed":
                        raise RuntimeError(result.error or "Provider 任务失败")
                    await asyncio.sleep(settings.provider_poll_interval_sec)
        except TimeoutError as exc:
            # 轮询超时不代表上游失败：任务可能仍在生成。保留 task_id 由调用方
            # 转入可恢复状态，重试时继续轮询原任务而不会重新提交计费。
            raise ProviderPollTimeoutError(
                "Provider 轮询超时，任务可能仍在生成；"
                "可稍后点击重试继续轮询原任务（不会重复提交计费）"
            ) from exc

    def _raise_if_job_cancelled(self, job_id: str) -> None:
        record = self.jobs.repository.get(job_id)
        if record is None or record["status"] == "cancelled":
            raise JobCancelledError("任务已取消")

    def _save_provider_result(
        self,
        job_id: str,
        result: ProviderPollResult,
    ) -> ProviderPollResult:
        payload = result.model_dump(mode="json")
        data: dict[str, Any] = {}
        if result.data.get("local_file"):
            data["local_file"] = result.data["local_file"]
        if result.content_base64:
            encoded = result.content_base64
            if encoded.startswith("data:") and "," in encoded:
                encoded = encoded.split(",", 1)[1]
            # 解码前按任务类型预估上限拦截：Base64 约为原始字节的 4/3，
            # 防止恶意/异常 Provider 返回超大 base64 耗尽内存。
            job_type = str(self.jobs.repository.require(job_id).get("job_type") or "")
            is_video = "video" in job_type
            byte_limit = MAX_VIDEO_SIZE if is_video else MAX_IMAGE_SIZE
            if len(encoded) > (byte_limit * 4 // 3) + 1024:
                raise FileServiceError("Provider 返回的 Base64 文件超过大小上限")
            try:
                content = base64.b64decode(encoded, validate=True)
            except ValueError as exc:
                raise FileServiceError("Provider 返回的 Base64 文件无效") from exc
            if len(content) > byte_limit:
                raise FileServiceError("Provider 返回的 Base64 文件超过大小上限")
            job_record = self.jobs.repository.require(job_id)
            safe_job_id = re.sub(r"[^A-Za-z0-9_.-]", "_", job_id)
            relative = f"temp/provider-results/{safe_job_id}.bin"
            staged = file_service.write_bytes_atomic(
                job_record["project_id"],
                relative,
                content,
            )
            data["local_file"] = str(staged)
            payload["content_base64"] = ""
        payload["data"] = data
        self.jobs.repository.save_result(job_id, payload)
        return ProviderPollResult.model_validate(payload)

    @staticmethod
    async def _download_provider_result(
        provider: Any,
        result: ProviderPollResult,
        target: Path,
    ) -> None:
        local_file = result.data.get("local_file")
        if local_file:
            await asyncio.to_thread(atomic_copy, local_file, target)
            return
        await provider.download(result, target)

    @staticmethod
    def _delete_staged_provider_result(
        project_id: str,
        result: ProviderPollResult,
    ) -> None:
        local_file = result.data.get("local_file")
        if not local_file:
            return
        try:
            relative = file_service.relative_project_path(project_id, local_file)
            if relative.startswith("temp/provider-results/"):
                file_service.delete_file(project_id, relative)
        except (FileServiceError, OSError):
            return

    @staticmethod
    def _delete_failed_target(project_id: str, target: Path | None) -> None:
        if target is None:
            return
        try:
            if target.is_file():
                relative = file_service.relative_project_path(project_id, target)
                file_service.delete_file(project_id, relative)
        except (FileServiceError, OSError):
            return

    @staticmethod
    def _has_saved_result(job_record: dict[str, Any]) -> bool:
        result = job_record.get("result_data") or {}
        return any(
            result.get(key)
            for key in ("result_url", "content_base64", "local_file", "download_url")
        ) or bool(result.get("data", {}).get("local_file"))

    def _set_media_status(
        self,
        table: Any,
        version_id: str,
        status: str,
        error: str = "",
        *,
        provider_task_id: str | None = None,
    ) -> None:
        values: dict[str, Any] = {
            "status": status,
            "error": sanitize_error_message(error),
        }
        if provider_task_id is not None:
            values["provider_task_id"] = provider_task_id
        with self.database.engine.begin() as connection:
            connection.execute(
                update(table)
                .where(
                    table.c.id == version_id,
                    table.c.status.in_(tuple(_ACTIVE_MEDIA_STATUSES)),
                )
                .values(**values)
            )

    def _complete_media(
        self,
        table: Any,
        version_id: str,
        relative: str,
        *,
        actual_duration_sec: float | None = None,
        job_record: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        kind: MediaKind = (
            "asset_image"
            if table is asset_image
            else "video"
            if table is video_version
            else "keyframe"
        )
        activation_job = job_record
        if activation_job is not None:
            activation_input = activation_job.get("input_data")
            if not isinstance(activation_input, Mapping):
                activation_input = json_loads(activation_job.get("input_json"), {})
            if not self._is_dynamic_media_input(activation_input):
                activation_job = None
        activated = self._activate_media_version(
            kind,
            version_id,
            source_relative=relative,
            complete=True,
            actual_duration_sec=actual_duration_sec,
            job_record=activation_job,
        )
        if kind == "asset_image":
            ShotService(self.database, self.jobs).sync_asset_image_binding(
                activated["asset_id"],
                version_id,
            )
        return activated
