"""合成服务

处理章节视频合成，包括视频拼接、配音混音、BGM添加、字幕烧录等。
"""

import json
import logging
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import insert, select, update

from app.config import settings
from app.db import Database, db
from app.models.tables import (
    chapter,
    chapter_composition,
    job,
    project_bgm,
    shot,
    shot_audio,
    video_version,
    workflow_run,
    workflow_stage,
)
from app.services.errors import bad_request, not_found
from app.services.file_service import file_service
from app.services.job_service import JobService
from app.services.subtitle_service import SubtitleService
from app.services.utils import json_dumps, json_loads, new_id, row_dict, utc_now

logger = logging.getLogger(__name__)

_DYNAMIC_ACTIVE_JOB_STATUSES = ("queued", "running", "waiting_provider", "downloading")


def _ffconcat_escape(value: str) -> str:
    """转义 ffconcat 单引号包裹的路径：路径内的单引号用 '\'' 表示。"""
    return value.replace("'", "'\\''")


def _ffmpeg_filter_quote(value: str) -> str:
    """转义并用单引号包裹 ffmpeg filter 参数值。"""
    escaped = value.replace("\\", "\\\\").replace("'", "'\\''")
    return f"'{escaped}'"


def _ffmpeg_filter_path(value: str) -> str:
    """转义并用单引号包裹 ffmpeg filter 中的文件路径。

    filtergraph 会把冒号当作参数分隔符，Windows 盘符（``C:/...``）即使包在单引号里
    也会被拆开，导致后半段路径被当成下一个位置参数。必须额外转义冒号。
    """
    escaped = value.replace("'", "'\\''").replace(":", "\\:")
    return f"'{escaped}'"


def _validate_file_path(file_path: Path, base_path: Path) -> Path:
    """验证文件路径，防止路径遍历攻击

    Args:
        file_path: 要验证的文件路径
        base_path: 允许的基础路径

    Returns:
        解析后的绝对路径

    Raises:
        bad_request: 如果路径不安全或不存在
    """
    try:
        # 解析为绝对路径
        resolved = file_path.resolve()
        base_resolved = base_path.resolve()

        # 检查是否在允许的基础路径内
        if not str(resolved).startswith(str(base_resolved)):
            raise bad_request(f"文件路径不在允许的目录内: {file_path}")

        # 检查文件是否存在
        if not resolved.exists():
            raise bad_request(f"文件不存在: {file_path}")

        return resolved
    except (OSError, ValueError) as exc:
        raise bad_request(f"无效的文件路径: {file_path}") from exc


class CompositionService:
    def __init__(
        self,
        database: Database | None = None,
        jobs: JobService | None = None,
    ) -> None:
        self.database = database or db
        self.jobs = jobs or JobService(self.database)
        self.subtitle_service = SubtitleService(self.database)

    def compose_chapter(
        self,
        chapter_id: str,
        config: dict[str, Any],
        *,
        enqueue: bool = True,
        workflow_context: Mapping[str, Any] | None = None,
        depends_on: Sequence[str] = (),
    ) -> dict[str, Any]:
        """
        创建章节合成任务

        Args:
            chapter_id: 章节 ID
            config: 合成配置
                - resolution: 分辨率 (720p/1080p/4k)
                - include_audio: 是否包含配音
                - burn_subtitles: 是否烧录字幕
                - bgm_id: BGM ID（可选）
                - bgm_volume: BGM 音量（默认 0.3）

        Returns:
            合成任务信息
        """
        with self.database.engine.begin() as connection:
            # 整个 select + 失效旧记录 + insert 都必须跑在同一 IMMEDIATE 事务里，
            # 避免并发请求同时通过 existing 检查后各自插入新行。
            assert connection.in_transaction()

            # 检查章节是否存在
            chapter_record = connection.execute(
                select(chapter.c.id, chapter.c.project_id, chapter.c.title).where(
                    chapter.c.id == chapter_id
                )
            ).first()
            if chapter_record is None:
                raise not_found(f"章节不存在：{chapter_id}")
            assert chapter_record is not None

            project_id = chapter_record.project_id
            context = dict(workflow_context or {})
            run_id = str(context.get("workflow_run_id") or "") or None
            stage_id = str(context.get("workflow_stage_id") or "") or None
            input_signature = str(context.get("input_signature") or "")
            delivery_generation = str(context.get("delivery_generation") or "")

            # 检查是否已有进行中的合成任务：只做幂等检测，不在循环里写 status。
            # 失效旧记录交由 _invalidate_stale_compositions 统一批量处理。
            if run_id:
                existing_query = (
                    select(chapter_composition)
                    .select_from(
                        chapter_composition.join(
                            job,
                            chapter_composition.c.job_id == job.c.id,
                        )
                    )
                    .where(
                        chapter_composition.c.chapter_id == chapter_id,
                        chapter_composition.c.workflow_run_id == run_id,
                        chapter_composition.c.input_signature == input_signature,
                        chapter_composition.c.status.in_(("pending", "processing")),
                    )
                )
                if stage_id:
                    existing_query = existing_query.where(job.c.workflow_stage_id == stage_id)
                existing = None
                for existing_row in connection.execute(existing_query).all():
                    existing_record = row_dict(existing_row) or {}
                    provenance = json_loads(existing_record.get("provenance_json"), {})
                    old_generation = (
                        str(provenance.get("delivery_generation") or "")
                        if isinstance(provenance, Mapping)
                        else ""
                    )
                    if delivery_generation and old_generation != delivery_generation:
                        # delivery_generation 不匹配：留给 _invalidate_stale_compositions
                        # 统一走 JobRepository.batch_fail + chapter_composition 状态翻转。
                        continue
                    existing = existing_record
                    break
            else:
                existing = connection.execute(
                    select(chapter_composition).where(
                        chapter_composition.c.chapter_id == chapter_id,
                        chapter_composition.c.status.in_(["pending", "processing"]),
                    )
                ).first()
            if existing:
                raise bad_request("章节已有合成任务在进行中")
            if run_id:
                self._invalidate_stale_compositions(
                    connection,
                    chapter_id=chapter_id,
                    run_id=run_id,
                    stage_id=stage_id or "",
                    input_signature=input_signature,
                    delivery_generation=delivery_generation,
                )

            # 创建 Job
            job_id = new_id("job")
            now = utc_now()
            input_data = {**dict(config), **context}
            dependency_ids = [str(value) for value in depends_on if str(value).strip()]
            connection.execute(
                insert(job).values(
                    id=job_id,
                    project_id=project_id,
                    chapter_id=chapter_id,
                    workflow_run_id=run_id,
                    workflow_stage_id=stage_id,
                    job_type="compose_chapter",
                    target_type="chapter",
                    target_id=chapter_id,
                    idempotency_key=context.get("idempotency_key"),
                    provider_kind="text",
                    status="queued",
                    input_json=json_dumps(input_data),
                    input_signature=input_signature,
                    depends_on_json=json_dumps(dependency_ids),
                    result_json="{}",
                    error="",
                    retry_count=0,
                    progress_current=0,
                    progress_total=1,
                    created_at=now,
                    updated_at=now,
                )
            )

            # 创建合成记录
            composition_id = new_id("compose")
            connection.execute(
                insert(chapter_composition).values(
                    id=composition_id,
                    chapter_id=chapter_id,
                    job_id=job_id,
                    workflow_run_id=run_id,
                    input_signature=input_signature,
                    preflight_json=json_dumps(context.get("preflight", {})),
                    provenance_json=json_dumps(context.get("provenance", {})),
                    resolution=config.get("resolution", "1080p"),
                    include_audio=int(config.get("include_audio", True)),
                    burn_subtitles=int(config.get("burn_subtitles", True)),
                    bgm_id=config.get("bgm_id"),
                    status="pending",
                    created_at=now,
                    updated_at=now,
                )
            )

            record = connection.execute(
                select(chapter_composition).where(chapter_composition.c.id == composition_id)
            ).first()

        result = row_dict(record)
        if result is None:
            raise RuntimeError("合成记录创建后无法读取")

        # 必须先提交任务，再入队：runner 用独立连接读取任务，
        # 在未提交事务内入队会读不到刚插入的行，导致任务永远停在排队中。
        if enqueue:
            self.jobs.runner.enqueue(job_id)

        return result

    def _invalidate_stale_compositions(
        self,
        connection: Any,
        *,
        chapter_id: str,
        run_id: str,
        stage_id: str,
        input_signature: str,
        delivery_generation: str,
    ) -> list[str]:
        """把同一章节里所有与当前 (run, stage, signature, generation) 不一致的进行中
        合成记录标为失败，关联的 job 通过 JobRepository.batch_fail 走 interrupt 路径。

        调用方需保证 connection 处于 IMMEDIATE 事务中。返回被失效的 composition id 列表。
        """
        assert connection.in_transaction(), "必须在 IMMEDIATE 事务内失效旧 composition"
        old_compositions = connection.execute(
            select(
                chapter_composition.c.id,
                chapter_composition.c.job_id,
                chapter_composition.c.workflow_run_id,
                chapter_composition.c.input_signature,
                chapter_composition.c.provenance_json,
            ).where(
                chapter_composition.c.chapter_id == chapter_id,
                chapter_composition.c.status.in_(("pending", "processing")),
            )
        ).all()
        failed_composition_ids: list[str] = []
        interrupted_job_ids: list[str] = []
        message = "旧动态 Composition attempt 已失效"
        for old_row in old_compositions:
            old_record = row_dict(old_row) or {}
            composition_id = str(old_record.get("id") or "")
            if not composition_id:
                continue
            old_job_id = str(old_record.get("job_id") or "")
            old_stage_id = ""
            if old_job_id:
                old_job = connection.execute(
                    select(job.c.workflow_stage_id).where(job.c.id == old_job_id)
                ).first()
                if old_job is not None:
                    old_stage_id = str(getattr(old_job, "workflow_stage_id", "") or "")
            old_provenance = json_loads(old_record.get("provenance_json"), {})
            old_generation = (
                str(old_provenance.get("delivery_generation") or "")
                if isinstance(old_provenance, Mapping)
                else ""
            )
            if (
                str(old_record.get("workflow_run_id") or "") != run_id
                or old_stage_id != str(stage_id or "")
                or str(old_record.get("input_signature") or "") != input_signature
                or (delivery_generation and old_generation != delivery_generation)
            ):
                connection.execute(
                    update(chapter_composition)
                    .where(chapter_composition.c.id == composition_id)
                    .values(
                        status="failed",
                        error=message,
                        updated_at=utc_now(),
                    )
                )
                failed_composition_ids.append(composition_id)
                if old_job_id:
                    interrupted_job_ids.append(old_job_id)
        if interrupted_job_ids:
            self.jobs.repository.batch_fail(
                interrupted_job_ids,
                message,
                connection=connection,
            )
        return failed_composition_ids

    @staticmethod
    def _dynamic_context(job_record: Mapping[str, Any]) -> dict[str, Any]:
        input_data = job_record.get("input_data")
        if not isinstance(input_data, Mapping):
            input_data = json_loads(job_record.get("input_json"), {})
        if not isinstance(input_data, Mapping):
            return {}
        context = {
            key: input_data.get(key)
            for key in (
                "workflow_run_id",
                "workflow_stage_id",
                "workflow_stage_attempt",
                "workflow_run_revision",
                "input_signature",
                "upstream_input_signature",
            )
            if input_data.get(key) is not None
        }
        return (
            context
            if context.get("workflow_run_id") and context.get("workflow_stage_id")
            else {}
        )

    def _assert_dynamic_current(
        self,
        connection: Any,
        job_record: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        context = self._dynamic_context(job_record)
        if not context:
            return None
        run_id = str(context["workflow_run_id"])
        stage_id = str(context["workflow_stage_id"])
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
        if run is None or stage is None:
            raise RuntimeError("动态 Composition 关联的 run/stage 不存在")
        latest = row_dict(
            connection.execute(
                select(workflow_stage)
                .where(
                    workflow_stage.c.workflow_run_id == run_id,
                    workflow_stage.c.stage_key == stage["stage_key"],
                )
                .order_by(
                    workflow_stage.c.attempt.desc(),
                    workflow_stage.c.updated_at.desc(),
                    workflow_stage.c.id.desc(),
                )
                .limit(1)
            ).first()
        )
        if latest is None or latest["id"] != stage_id:
            raise RuntimeError("动态 Composition stage attempt 已过期")
        if str(job_record.get("input_signature") or "") != str(stage.get("input_signature") or ""):
            raise RuntimeError("动态 Composition input_signature 已变化")
        if context.get("workflow_stage_attempt") is not None and str(
            context["workflow_stage_attempt"]
        ) != str(stage.get("attempt")):
            raise RuntimeError("动态 Composition stage attempt 已变化")
        if str(job_record.get("workflow_run_id") or run_id) != run_id:
            raise RuntimeError("动态 Composition workflow_run_id 已变化")
        if str(job_record.get("workflow_stage_id") or stage_id) != stage_id:
            raise RuntimeError("动态 Composition workflow_stage_id 已变化")
        if run.get("status") in {"cancelled", "stale", "blocked"} or stage.get("status") in {
            "cancelled",
            "stale",
            "blocked",
        }:
            raise RuntimeError("动态 Composition 关联的 run/stage 已不可执行")
        return run, stage

    def _update_dynamic_workflow(
        self,
        connection: Any,
        job_record: Mapping[str, Any],
        *,
        stage_status: str,
        error: str,
        job_status: str,
        result: Mapping[str, Any],
    ) -> None:
        current = self._assert_dynamic_current(connection, job_record)
        if current is None:
            raise RuntimeError("动态 Composition 缺少 workflow 上下文")
        run, stage = current
        now = utc_now()
        stage_update = connection.execute(
            update(workflow_stage)
            .where(
                workflow_stage.c.id == stage["id"],
                workflow_stage.c.workflow_run_id == run["id"],
                workflow_stage.c.attempt == stage["attempt"],
                workflow_stage.c.input_signature == stage.get("input_signature"),
                workflow_stage.c.status.not_in(("cancelled", "stale")),
            )
            .values(
                status=stage_status,
                error=error,
                finished_at=now,
                updated_at=now,
            )
        )
        if stage_update.rowcount != 1:
            raise RuntimeError("动态 Composition stage 条件更新未命中")
        run_update = connection.execute(
            update(workflow_run)
            .where(
                workflow_run.c.id == run["id"],
                workflow_run.c.revision == run["revision"],
                workflow_run.c.status.not_in(("cancelled", "stale")),
            )
            .values(
                status=stage_status,
                error=error,
                revision=workflow_run.c.revision + 1,
                finished_at=now,
                updated_at=now,
            )
        )
        if run_update.rowcount != 1:
            raise RuntimeError("动态 Composition run 条件更新未命中")
        job_signature = str(job_record.get("input_signature") or "")
        job_update = connection.execute(
            update(job)
            .where(
                job.c.id == job_record["id"],
                job.c.project_id == run.get("project_id"),
                job.c.workflow_run_id == run["id"],
                job.c.workflow_stage_id == stage["id"],
                job.c.status.in_(_DYNAMIC_ACTIVE_JOB_STATUSES),
                job.c.input_signature == job_signature,
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
        if job_update.rowcount != 1:
            raise RuntimeError("动态 Composition Job 条件更新未命中")

    async def handle_compose(self, job_record: dict) -> dict[str, Any]:
        """任务处理器：执行合成"""
        chapter_id = job_record["chapter_id"]
        config = job_record.get("input_data")
        if not isinstance(config, Mapping):
            config = json_loads(job_record["input_json"], {})
        config = dict(config) if isinstance(config, Mapping) else {}
        dynamic_context = self._dynamic_context(job_record)

        logger.info(f"开始合成章节 {chapter_id}，配置：{config}")

        with self.database.engine.begin() as connection:
            if dynamic_context:
                self._assert_dynamic_current(connection, job_record)
                connection.execute(
                    update(chapter_composition)
                    .where(
                        chapter_composition.c.job_id == job_record["id"],
                        chapter_composition.c.workflow_run_id == dynamic_context[
                            "workflow_run_id"
                        ],
                        chapter_composition.c.input_signature
                        == job_record.get("input_signature"),
                        chapter_composition.c.status.in_(("pending", "processing")),
                    )
                    .values(status="processing", updated_at=utc_now())
                )
            else:
                # legacy compose 保持原有章节级行为。
                connection.execute(
                    update(chapter_composition)
                    .where(chapter_composition.c.chapter_id == chapter_id)
                    .values(status="processing", updated_at=utc_now())
                )

        try:
            if dynamic_context:
                dependencies = job_record.get("depends_on")
                if isinstance(dependencies, list):
                    for dependency_id in dependencies:
                        dependency = self.jobs.repository.get(str(dependency_id))
                        if dependency is None:
                            raise RuntimeError(f"Composition 依赖不存在：{dependency_id}")
                        if dependency.get("status") != "completed":
                            raise RuntimeError(
                                "Composition 依赖尚未完成："
                                f"{dependency_id}（{dependency.get('status')}）"
                            )
            # 1. 获取所有分镜视频
            media_snapshot = config.get("media_snapshot") if dynamic_context else None
            shots_data = self._get_chapter_shots(
                chapter_id,
                media_snapshot=media_snapshot,
            )
            if not shots_data:
                raise RuntimeError("章节无分镜视频")

            # 2. 只合成已生成视频的分镜；自由制作预置的空分镜没有视频，直接跳过。
            skipped_videos = [s["shot_no"] for s in shots_data if not s.get("video_path")]
            shots_data = [s for s in shots_data if s.get("video_path")]
            if not shots_data:
                raise RuntimeError("没有已生成的分镜视频，请先生成至少一个分镜视频")
            if skipped_videos:
                logger.info("合成跳过未生成视频的分镜：%s", skipped_videos)

            # 3. 获取项目路径
            with self.database.engine.connect() as connection:
                chapter_record = connection.execute(
                    select(chapter.c.project_id, chapter.c.chapter_no, chapter.c.title).where(
                        chapter.c.id == chapter_id
                    )
                ).first()
            if chapter_record is None:
                raise RuntimeError(f"章节不存在：{chapter_id}")

            project_id = chapter_record.project_id
            project_path = file_service.project_path(project_id)

            # 4. 创建输出目录
            output_dir = project_path / "成片"
            output_dir.mkdir(parents=True, exist_ok=True)

            chapter_name = f"第{chapter_record.chapter_no:02d}章"
            if chapter_record.title:
                chapter_name += f"-{chapter_record.title}"

            output_filename = f"{chapter_name}.mp4"
            output_path = output_dir / output_filename

            # 5. 合成视频
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir)

                crossfade_enabled = bool(config.get("crossfade"))
                crossfade_seconds = float(config.get("crossfade_seconds") or 0.5)

                # 5.1 拼接视频
                concat_video = self._concat_videos(
                    shots_data,
                    project_path,
                    temp_path,
                    crossfade=crossfade_enabled,
                    crossfade_seconds=crossfade_seconds,
                )

                # 5.2 添加配音（如果启用）
                if config.get("include_audio"):
                    concat_video = self._add_audio(
                        concat_video,
                        shots_data,
                        project_path,
                        temp_path,
                        crossfade_seconds=(
                            crossfade_seconds if crossfade_enabled else None
                        ),
                    )

                # 5.3 添加 BGM（如果指定）
                if config.get("bgm_id"):
                    concat_video = self._add_bgm(
                        concat_video,
                        config["bgm_id"],
                        project_path,
                        temp_path,
                        volume=config.get("bgm_volume", 0.3),
                    )

                # 5.4 烧录字幕（如果启用）
                if config.get("burn_subtitles"):
                    concat_video = self._burn_subtitles(concat_video, chapter_id, temp_path)

                # 5.5 最终输出
                self._finalize_output(concat_video, output_path, config.get("resolution", "1080p"))

            # 6. 获取视频时长
            duration_sec = self._get_video_duration(output_path)

            # 7. 更新数据库
            with self.database.engine.begin() as connection:
                values = {
                    "file_path": str(output_path.relative_to(project_path)),
                    "duration_sec": duration_sec,
                    "status": "completed",
                    "updated_at": utc_now(),
                }
                if dynamic_context:
                    self._assert_dynamic_current(connection, job_record)
                    updated = connection.execute(
                        update(chapter_composition)
                        .where(
                            chapter_composition.c.job_id == job_record["id"],
                            chapter_composition.c.workflow_run_id
                            == dynamic_context["workflow_run_id"],
                            chapter_composition.c.input_signature
                            == job_record.get("input_signature"),
                            chapter_composition.c.status == "processing",
                        )
                        .values(**values)
                    )
                    if updated.rowcount != 1:
                        raise RuntimeError("动态 Composition 终态条件更新未命中")
                    self._update_dynamic_workflow(
                        connection,
                        job_record,
                        stage_status="confirmed",
                        error="",
                        job_status="completed",
                        result={
                            "status": "completed",
                            "file_path": values["file_path"],
                            "duration_sec": duration_sec,
                        },
                    )
                else:
                    connection.execute(
                        update(chapter_composition)
                        .where(chapter_composition.c.chapter_id == chapter_id)
                        .values(**values)
                    )

            logger.info(f"章节 {chapter_id} 合成完成：{output_path}")

            return {
                "file_path": str(output_path.relative_to(project_path)),
                "duration_sec": duration_sec,
            }

        except Exception as exc:
            logger.error(f"章节 {chapter_id} 合成失败：{exc}")
            with self.database.engine.begin() as connection:
                if dynamic_context:
                    self._assert_dynamic_current(connection, job_record)
                    failed_update = connection.execute(
                        update(chapter_composition)
                        .where(
                            chapter_composition.c.job_id == job_record["id"],
                            chapter_composition.c.workflow_run_id
                            == dynamic_context["workflow_run_id"],
                            chapter_composition.c.input_signature
                            == job_record.get("input_signature"),
                            chapter_composition.c.status.in_(
                                ("pending", "processing", "failed")
                            ),
                        )
                        .values(status="failed", error=str(exc), updated_at=utc_now())
                    )
                    if failed_update.rowcount != 1:
                        raise RuntimeError("动态 Composition 失败状态条件更新未命中") from exc
                    self._update_dynamic_workflow(
                        connection,
                        job_record,
                        stage_status="partial_failed",
                        error=str(exc),
                        job_status="failed",
                        result={
                            "status": "partial_failed",
                            "error": str(exc),
                        },
                    )
                else:
                    connection.execute(
                        update(chapter_composition)
                        .where(chapter_composition.c.chapter_id == chapter_id)
                        .values(status="failed", error=str(exc), updated_at=utc_now())
                    )
            raise

    @staticmethod
    def _validate_snapshot_media(
        record: Mapping[str, Any],
        expected: Mapping[str, Any],
        *,
        kind: str,
    ) -> None:
        expected_id = str(expected.get("version_id") or "")
        if not expected_id or str(record.get("id") or "") != expected_id:
            raise RuntimeError(f"动态 Composition {kind} version 已切换")
        expected_path = str(expected.get("file_path") or "")
        if str(record.get("file_path") or "") != expected_path:
            raise RuntimeError(f"动态 Composition {kind} file_path 已切换")
        expected_signature = str(expected.get("input_signature") or "")
        if expected_signature and str(record.get("input_signature") or "") != expected_signature:
            raise RuntimeError(f"动态 Composition {kind} input_signature 已切换")
        if record.get("is_current") not in (1, True):
            raise RuntimeError(f"动态 Composition {kind} current version 已切换")
        provenance = json_loads(record.get("provenance_json"), {})
        provenance = provenance if isinstance(provenance, Mapping) else {}
        expected_stage_id = str(expected.get("workflow_stage_id") or "")
        actual_stage_id = str(
            provenance.get("workflow_stage_id") or provenance.get("stage_id") or ""
        )
        if expected_stage_id and actual_stage_id != expected_stage_id:
            raise RuntimeError(f"动态 Composition {kind} workflow stage 已切换")
        expected_attempt = expected.get("attempt")
        actual_attempt = provenance.get("workflow_stage_attempt") or provenance.get(
            "source_attempt"
        )
        if expected_attempt is not None and str(actual_attempt) != str(expected_attempt):
            raise RuntimeError(f"动态 Composition {kind} attempt 已切换")
        expected_revision = expected.get("revision")
        actual_revision = provenance.get("workflow_run_revision") or provenance.get("run_revision")
        if expected_revision is not None and actual_revision is not None:
            if int(actual_revision) > int(expected_revision):
                raise RuntimeError(f"动态 Composition {kind} revision 已切换")

    def _get_chapter_shots(
        self,
        chapter_id: str,
        *,
        media_snapshot: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """获取章节分镜媒体；动态任务严格使用已冻结的 version snapshot。"""
        dynamic_snapshot = media_snapshot is not None
        snapshot_by_shot: dict[str, Mapping[str, Any]] = {}
        if dynamic_snapshot:
            raw_shots = media_snapshot.get("shots")
            if not isinstance(raw_shots, list):
                raise RuntimeError("没有已生成的分镜视频：动态 Composition 缺少媒体 snapshot")
            snapshot_by_shot = {
                str(item.get("shot_id")): item
                for item in raw_shots
                if isinstance(item, Mapping) and item.get("shot_id")
            }
        with self.database.engine.connect() as connection:
            # 获取分镜列表
            shot_records = connection.execute(
                select(shot.c.id, shot.c.shot_no, shot.c.duration_sec, shot.c.project_id)
                .where(shot.c.chapter_id == chapter_id)
                .order_by(shot.c.shot_order)
            ).fetchall()

            shots_data = []
            for shot_record in shot_records:
                shot_id = shot_record.id

                if dynamic_snapshot:
                    snapshot_item = snapshot_by_shot.get(str(shot_id))
                    if snapshot_item is None:
                        raise RuntimeError(f"动态 Composition 缺少 shot snapshot：{shot_id}")
                    video_expected = snapshot_item.get("video")
                    audio_expected = snapshot_item.get("audio")
                    video_record = None
                    audio_record = None
                    if isinstance(video_expected, Mapping) and video_expected.get("version_id"):
                        video_record = connection.execute(
                            select(video_version).where(
                                video_version.c.id == video_expected["version_id"],
                                video_version.c.shot_id == shot_id,
                            )
                        ).first()
                        if video_record is None:
                            raise RuntimeError(f"动态 Composition video version 不存在：{shot_id}")
                        self._validate_snapshot_media(
                            dict(video_record._mapping),
                            video_expected,
                            kind="video",
                        )
                    if isinstance(audio_expected, Mapping) and audio_expected.get("version_id"):
                        audio_record = connection.execute(
                            select(shot_audio).where(
                                shot_audio.c.id == audio_expected["version_id"],
                                shot_audio.c.shot_id == shot_id,
                            )
                        ).first()
                        if audio_record is None:
                            raise RuntimeError(f"动态 Composition audio version 不存在：{shot_id}")
                        self._validate_snapshot_media(
                            dict(audio_record._mapping),
                            audio_expected,
                            kind="audio",
                        )
                else:
                    # legacy compose 继续读取章节当前版本。
                    video_record = connection.execute(
                        select(
                            video_version.c.id,
                            video_version.c.file_path,
                            video_version.c.actual_duration_sec,
                        ).where(
                            video_version.c.shot_id == shot_id,
                            video_version.c.is_current == 1,
                        )
                    ).first()
                    audio_record = connection.execute(
                        select(
                            shot_audio.c.id,
                            shot_audio.c.file_path,
                            shot_audio.c.duration_sec,
                        ).where(
                            shot_audio.c.shot_id == shot_id, shot_audio.c.is_current == 1
                        )
                    ).first()

                shots_data.append(
                    {
                        "shot_id": shot_id,
                        "shot_no": shot_record.shot_no,
                        "duration_sec": shot_record.duration_sec,
                        "video_path": video_record.file_path if video_record else None,
                        "video_version_id": video_record.id if video_record else None,
                        "audio_path": audio_record.file_path if audio_record else None,
                        "audio_version_id": audio_record.id if audio_record else None,
                        "audio_duration": audio_record.duration_sec if audio_record else None,
                    }
                )

            return shots_data

    def _concat_videos(
        self,
        shots_data: list[dict[str, Any]],
        project_path: Path,
        temp_path: Path,
        *,
        crossfade: bool = False,
        crossfade_seconds: float = 0.5,
    ) -> Path:
        """拼接视频"""
        if crossfade:
            return self._concat_videos_with_crossfade(
                shots_data,
                project_path,
                temp_path,
                crossfade_seconds,
            )
        # 创建 concat 文件
        concat_file = temp_path / "concat.txt"
        with concat_file.open("w", encoding="utf-8") as f:
            for shot in shots_data:
                video_full_path = project_path / shot["video_path"]
                # FFmpeg concat 需要绝对路径或用单引号包裹
                f.write(f"file '{_ffconcat_escape(video_full_path.as_posix())}'\n")

        output = temp_path / "concat.mp4"

        try:
            subprocess.run(
                [
                    settings.ffmpeg_path,
                    "-y",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(concat_file),
                    "-c",
                    "copy",
                    str(output),
                ],
                check=True,
                timeout=600,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except subprocess.CalledProcessError as exc:
            error_msg = exc.stderr.decode("utf-8", errors="ignore") if exc.stderr else "未知错误"
            raise bad_request(f"视频拼接失败: {error_msg[:200]}") from exc

        return output

    @staticmethod
    def _crossfade_offsets(durations: list[float], crossfade_seconds: float) -> list[float]:
        """计算 xfade 链中每个转场在组合时间轴上的起始偏移。"""
        offsets: list[float] = []
        combined = durations[0]
        for index in range(1, len(durations)):
            offset = combined - crossfade_seconds
            offsets.append(max(0.0, offset))
            combined = combined + durations[index] - crossfade_seconds
        return offsets

    @staticmethod
    def _effective_crossfade_seconds(
        durations: list[float],
        crossfade_seconds: float,
    ) -> float:
        """实际使用的叠化时长：单段不叠化，且不能超过最短素材的一半。"""
        if len(durations) < 2:
            return 0.0
        return max(0.1, min(float(crossfade_seconds), min(durations) / 2))

    @staticmethod
    def _audio_offsets(
        durations: list[float],
        crossfade_seconds: float | None,
    ) -> list[float]:
        """按真实视频时长计算每段素材在成片时间轴上的起始时间。"""
        fade = (
            CompositionService._effective_crossfade_seconds(
                durations,
                crossfade_seconds,
            )
            if crossfade_seconds is not None
            else 0.0
        )
        offsets: list[float] = []
        cursor = 0.0
        for index, duration in enumerate(durations):
            offsets.append(cursor)
            cursor += duration - (fade if index < len(durations) - 1 else 0)
        return offsets

    @staticmethod
    def _get_video_stream_info(path: Path) -> dict[str, Any]:
        probe = subprocess.run(
            [
                settings.ffprobe_path,
                "-v",
                "error",
                "-show_streams",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        streams = json.loads(probe.stdout).get("streams", [])
        video = next((s for s in streams if s.get("codec_type") == "video"), {})
        try:
            num, den = str(video.get("r_frame_rate", "25/1")).split("/")
            fps = max(1, round(int(num) / int(den)))
        except (ValueError, ZeroDivisionError):
            fps = 25
        return {
            "width": int(video.get("width", 1280)),
            "height": int(video.get("height", 720)),
            "fps": fps,
            "has_audio": any(
                stream.get("codec_type") == "audio" for stream in streams
            ),
        }

    def _concat_videos_with_crossfade(
        self,
        shots_data: list[dict[str, Any]],
        project_path: Path,
        temp_path: Path,
        crossfade_seconds: float,
    ) -> Path:
        """用 xfade 叠化拼接分镜视频（每个转场 0.5s）。"""
        video_paths = [
            project_path / shot["video_path"]
            for shot in shots_data
            if shot.get("video_path")
        ]
        if len(video_paths) < 2:
            return self._concat_videos(shots_data, project_path, temp_path)

        durations = [self._get_video_duration(path) for path in video_paths]
        first = self._get_video_stream_info(video_paths[0])
        width, height, fps = first["width"], first["height"], first["fps"]
        has_audio = all(
            self._get_video_stream_info(path)["has_audio"] for path in video_paths
        )
        crossfade_seconds = self._effective_crossfade_seconds(
            durations,
            crossfade_seconds,
        )
        offsets = self._crossfade_offsets(durations, crossfade_seconds)

        cmd = [settings.ffmpeg_path, "-y"]
        for path in video_paths:
            cmd.extend(["-i", str(path)])

        filters: list[str] = []
        for index in range(len(video_paths)):
            filters.append(
                f"[{index}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
                f"fps={fps},format=yuv420p[v{index}]"
            )
        previous = "v0"
        for index in range(1, len(video_paths)):
            label = f"vx{index}" if index < len(video_paths) - 1 else "vout"
            filters.append(
                f"[{previous}][v{index}]xfade=transition=fade:"
                f"duration={crossfade_seconds}:offset={offsets[index - 1]}[{label}]"
            )
            previous = label

        if has_audio:
            for index in range(len(video_paths)):
                filters.append(
                    f"[{index}:a]aformat=sample_rates=44100:"
                    f"channel_layouts=stereo[a{index}]"
                )
            previous = "a0"
            for index in range(1, len(video_paths)):
                label = f"ax{index}" if index < len(video_paths) - 1 else "aout"
                filters.append(
                    f"[{previous}][a{index}]acrossfade=d={crossfade_seconds}[{label}]"
                )
                previous = label

        cmd.extend(
            [
                "-filter_complex",
                ";".join(filters),
                "-map",
                "[vout]",
            ]
        )
        if has_audio:
            cmd.extend(["-map", "[aout]"])
        cmd.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "18",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
                str(temp_path / "concat.mp4"),
            ]
        )

        try:
            subprocess.run(
                cmd,
                check=True,
                timeout=1200,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except subprocess.CalledProcessError as exc:
            error_msg = (
                exc.stderr.decode("utf-8", errors="ignore") if exc.stderr else "未知错误"
            )
            raise bad_request(f"叠化拼接失败: {error_msg[:300]}") from exc

        return temp_path / "concat.mp4"

    def _add_audio(
        self,
        video_path: Path,
        shots_data: list[dict[str, Any]],
        project_path: Path,
        temp_path: Path,
        *,
        crossfade_seconds: float | None = None,
    ) -> Path:
        """添加配音音轨"""
        # 时间轴必须按真实视频时长计算：xfade 会重叠缩短成片，
        # 使用拍摄计划里的 duration_sec 会让后续配音整体前移。
        video_durations: list[float] = []
        for item in shots_data:
            if item.get("video_path"):
                video_durations.append(
                    self._get_video_duration(project_path / item["video_path"])
                )
            else:
                video_durations.append(float(item.get("duration_sec") or 0))
        starts = self._audio_offsets(video_durations, crossfade_seconds)
        audio_files = [
            (offset, project_path / item["audio_path"])
            for offset, item in zip(starts, shots_data, strict=True)
            if item.get("audio_path")
        ]

        if not audio_files:
            return video_path

        # 合并所有配音（带时间偏移）
        concat_audio = temp_path / "concat_audio.mp3"
        filter_complex = []
        for idx, (offset, _audio_path) in enumerate(audio_files):
            delay_ms = int(offset * 1000)
            filter_complex.append(f"[{idx}]adelay={delay_ms}|{delay_ms}[a{idx}]")

        mix_inputs = "".join(f"[a{i}]" for i in range(len(audio_files)))
        filter_complex.append(
            f"{mix_inputs}amix=inputs={len(audio_files)}[aout]"
        )

        cmd = [settings.ffmpeg_path, "-y"]
        for _, audio_path in audio_files:
            cmd.extend(["-i", str(audio_path)])
        cmd.extend(
            [
                "-filter_complex",
                ";".join(filter_complex),
                "-map",
                "[aout]",
                str(concat_audio),
            ]
        )

        try:
            subprocess.run(
                cmd,
                check=True,
                timeout=300,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except subprocess.CalledProcessError as exc:
            error_msg = exc.stderr.decode("utf-8", errors="ignore") if exc.stderr else "未知错误"
            raise bad_request(f"音频混音失败: {error_msg[:200]}") from exc

        # 混合到视频
        output = temp_path / "with_audio.mp4"
        subprocess.run(
            [
                settings.ffmpeg_path,
                "-y",
                "-i",
                str(video_path),
                "-i",
                str(concat_audio),
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                str(output),
            ],
            check=True,
            timeout=600,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        return output

    def _add_bgm(
        self, video_path: Path, bgm_id: str, project_path: Path, temp_path: Path, volume: float
    ) -> Path:
        """添加 BGM 音轨"""
        with self.database.engine.connect() as connection:
            bgm_record = connection.execute(
                select(project_bgm.c.file_path).where(project_bgm.c.id == bgm_id)
            ).first()

        if bgm_record is None:
            logger.warning(f"BGM 不存在：{bgm_id}，跳过")
            return video_path

        bgm_full_path = project_path / bgm_record.file_path
        output = temp_path / "with_bgm.mp4"

        # 获取视频时长
        video_duration = self._get_video_duration(video_path)
        has_video_audio = self._get_video_stream_info(video_path)["has_audio"]
        fade_out_start = max(0.0, video_duration - 2)
        bgm_chain = (
            f"[0:a]afade=t=in:st=0:d=2,"
            f"afade=t=out:st={fade_out_start}:d=2,"
            f"volume={volume}[bgm]"
        )
        if has_video_audio:
            filter_complex = (
                f"{bgm_chain};"
                "[1:a][bgm]amix=inputs=2:duration=first[aout]"
            )
            audio_map = "[aout]"
        else:
            # 视频没有音轨时不能引用 [1:a]，直接使用 BGM 音轨。
            filter_complex = bgm_chain
            audio_map = "[bgm]"

        # 混音：视频音轨 + BGM（循环并淡入淡出）
        subprocess.run(
            [
                settings.ffmpeg_path,
                "-y",
                "-stream_loop",
                "-1",  # 无限循环 BGM
                "-i",
                str(bgm_full_path),
                "-i",
                str(video_path),
                "-t",
                str(video_duration),
                "-filter_complex",
                filter_complex,
                "-map",
                "1:v:0",
                "-map",
                audio_map,
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                str(output),
            ],
            check=True,
            timeout=600,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        return output

    def _burn_subtitles(self, video_path: Path, chapter_id: str, temp_path: Path) -> Path:
        """烧录字幕"""
        # 导出 SRT 文件
        srt_content = self.subtitle_service.export_chapter_srt(chapter_id)
        if not srt_content.strip():
            logger.info(f"章节 {chapter_id} 无字幕，跳过烧录")
            return video_path

        srt_file = temp_path / "subtitles.srt"
        srt_file.write_text(srt_content, encoding="utf-8")

        output = temp_path / "with_subtitles.mp4"

        # 字幕样式
        style = (
            "FontName=Microsoft YaHei,FontSize=24,PrimaryColour=&HFFFFFF,"
            "OutlineColour=&H000000,Outline=2"
        )

        subprocess.run(
            [
                settings.ffmpeg_path,
                "-y",
                "-i",
                str(video_path),
                "-vf",
                (
                    f"subtitles={_ffmpeg_filter_path(srt_file.as_posix())}"
                    f":force_style={_ffmpeg_filter_quote(style)}"
                ),
                "-c:a",
                "copy",
                str(output),
            ],
            check=True,
            timeout=600,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        return output

    def _finalize_output(self, video_path: Path, output_path: Path, resolution: str) -> None:
        """最终输出（可选：调整分辨率）"""
        if resolution == "720p":
            scale = "scale=1280:720"
        elif resolution == "4k":
            scale = "scale=3840:2160"
        else:  # 1080p 或其他
            scale = None

        if scale:
            subprocess.run(
                [
                    settings.ffmpeg_path,
                    "-y",
                    "-i",
                    str(video_path),
                    "-vf",
                    scale,
                    "-c:a",
                    "copy",
                    str(output_path),
                ],
                check=True,
                timeout=600,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        else:
            # 直接复制
            subprocess.run(
                [
                    settings.ffmpeg_path,
                    "-y",
                    "-i",
                    str(video_path),
                    "-c",
                    "copy",
                    str(output_path),
                ],
                check=True,
                timeout=600,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )

    @staticmethod
    def _get_video_duration(video_path: Path) -> float:
        """获取视频时长"""
        try:
            result = subprocess.run(
                [
                    settings.ffprobe_path,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(video_path),
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            return float(result.stdout.strip())
        except subprocess.CalledProcessError as exc:
            error_msg = exc.stderr if exc.stderr else "未知错误"
            raise bad_request(f"获取视频时长失败: {error_msg[:200]}") from exc
        except ValueError as exc:
            logger.warning(f"无法获取视频时长：{exc}")
            return 0.0

    def get_chapter_composition(self, chapter_id: str) -> dict[str, Any] | None:
        """获取章节合成记录"""
        with self.database.engine.connect() as connection:
            record = connection.execute(
                select(chapter_composition)
                .where(chapter_composition.c.chapter_id == chapter_id)
                .order_by(chapter_composition.c.created_at.desc())
            ).first()

        if record is None:
            return None

        return row_dict(record)
