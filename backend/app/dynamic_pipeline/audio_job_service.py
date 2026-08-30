"""Phase 6A 动态对白音频 Job runtime。

动态音频 Job 的 Provider 调用和文件写入都发生在数据库事务之外。数据库事务只负责
校验最终状态、切换同一对白行的 current 版本、写入 ``shot_audio``，以及完成 Job。
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import math
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import func, insert, select, update
from sqlalchemy import inspect as sa_inspect

from app.config import settings
from app.db import Database, db, utc_now
from app.models.tables import job, shot, shot_audio, workflow_run, workflow_stage
from app.services.file_service import FileService, FileServiceError
from app.services.provider_service import ProviderService
from app.services.utils import json_dumps, json_loads, new_id, sanitize_error_message

_ACTIVE_JOB_STATUSES = frozenset({"queued", "running", "waiting_provider", "downloading"})
_TERMINAL_JOB_STATUSES = frozenset({"completed", "failed", "cancelled", "interrupted"})
_JOB_TYPE = "generate_dialogue_audio"
_TARGET_TYPE = "shot_audio"
_EDGE_PREFIX = "edge-tts:"
_AUDIO_SUFFIXES = frozenset({"mp3", "wav", "ogg", "flac", "aac", "m4a"})


class DynamicAudioJobError(RuntimeError):
    """动态音频 Job 输入、Provider 或落库失败。"""


class _JobNoLongerActive(DynamicAudioJobError):
    """Provider 返回后，Job 已被取消或被其他执行器完成。"""


@dataclass(frozen=True)
class _AudioInput:
    job_id: str
    project_id: str
    workflow_run_id: str
    workflow_stage_id: str
    shot_id: str
    dialogue_line_id: str
    line_order: int
    text: str
    source_text_snapshot: str
    speaker: str
    voice_id: str
    provider_id: str
    provider_model: str
    input_signature: str
    upstream_input_signature: str
    params: dict[str, Any]


@dataclass(frozen=True)
class _CommitOutcome:
    result: dict[str, Any]
    committed: bool


def _row_dict(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row._mapping) if hasattr(row, "_mapping") else dict(row)


def _text(value: Any) -> str:
    if value is None:
        return ""
    return value.strip() if isinstance(value, str) else str(value).strip()


def _snapshot_text(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _mapping_value(value: Any, *names: str) -> Any:
    if not isinstance(value, Mapping):
        return None
    for name in names:
        if name in value and value[name] is not None:
            return value[name]
    return None


def _decode_result(record: Mapping[str, Any]) -> dict[str, Any]:
    value = record.get("result_data")
    if isinstance(value, Mapping):
        return dict(value)
    value = record.get("result_json")
    if isinstance(value, Mapping):
        return dict(value)
    parsed = json_loads(value, {})
    return dict(parsed) if isinstance(parsed, Mapping) else {}


class DynamicAudioJobService:
    """执行一条 ``generate_dialogue_audio`` 动态 Job。"""

    def __init__(
        self,
        database: Database | None,
        provider_service: Any | None = None,
        file_service: FileService | None = None,
    ) -> None:
        self.database = database or db
        self.provider_service = provider_service or ProviderService(self.database)
        self.file_service = file_service or FileService(database=self.database)

    async def handle_job(self, job_record: Mapping[str, Any] | Any) -> dict[str, Any]:
        """执行 Job，并返回与 ``job.result_json`` 一致的结果对象。"""

        incoming = _row_dict(job_record) or {}
        job_id = _text(incoming.get("id"))
        if not job_id:
            return self._failure_result("failed", "动态音频任务缺少 Job ID")

        current = self._get_job(job_id)
        if current is None:
            return self._failure_result("failed", "动态音频任务不存在")
        if current["status"] == "completed":
            return self._completed_result(current)
        if current["status"] not in _ACTIVE_JOB_STATUSES:
            return self._status_result(current)

        # Repository 解码后的 input_data 优先；直接传入原始数据库行时回退到 input_json。
        record = dict(current)
        if isinstance(incoming.get("input_data"), Mapping):
            record["input_data"] = dict(incoming["input_data"])
        for name in ("job_type", "target_type", "target_id", "provider_kind"):
            if name in incoming and incoming[name] is not None:
                record[name] = incoming[name]

        try:
            audio_input = self._validate_input(record)
            self._validate_shot_reference(audio_input)
            self._assert_dynamic_state(audio_input)
            existing = self._find_reusable_audio(audio_input)
            if existing is not None:
                result = self._audio_result_from_row(existing)
                return self._complete_existing_job(audio_input, result)

            self._assert_dynamic_state(audio_input)
            provider, provider_id, provider_model = await self._resolve_provider(audio_input)
            audio_bytes = await self._synthesize(
                provider,
                audio_input,
                provider_model=provider_model,
            )
            if not audio_bytes:
                raise DynamicAudioJobError("TTS Provider 返回空音频")

            # 这里只生成项目内相对路径；FileService 会再次校验路径并使用项目 temp 目录
            # 完成原子写入。文件名不直接使用用户输入，避免路径穿越和重名覆盖。
            file_path, absolute_path = self._write_audio_file(audio_input, audio_bytes)
            try:
                duration_sec = self._get_audio_duration(absolute_path)
            except BaseException:
                self._remove_audio_file(audio_input.project_id, file_path)
                raise

            provenance = self._build_provenance(
                audio_input,
                provider_id=provider_id,
                provider_model=provider_model,
            )
            try:
                outcome = self._commit_audio(
                    audio_input,
                    file_path=file_path,
                    duration_sec=duration_sec,
                    provider_id=provider_id,
                    provider_model=provider_model,
                    provenance=provenance,
                )
            except BaseException:
                self._remove_audio_file(audio_input.project_id, file_path)
                raise
            if not outcome.committed:
                self._remove_audio_file(audio_input.project_id, file_path)
            return outcome.result
        except asyncio.CancelledError:
            return self._interrupt_job(job_id, "动态音频任务已中断")
        except DynamicAudioJobError as exc:
            return self._fail_job(job_id, self._safe_error("动态音频任务失败", exc))
        except Exception as exc:  # Provider 和数据库实现不得把原始敏感信息写入 Job
            return self._fail_job(job_id, self._safe_error("动态音频任务失败", exc))

    def _get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.database.engine.connect() as connection:
            row = connection.execute(select(job).where(job.c.id == job_id)).first()
        record = _row_dict(row)
        if record is None:
            return None
        record["input_data"] = json_loads(record.get("input_json"), {})
        record["result_data"] = json_loads(record.get("result_json"), {})
        return record

    def _validate_input(self, record: Mapping[str, Any]) -> _AudioInput:
        if record.get("job_type") != _JOB_TYPE:
            raise DynamicAudioJobError(f"不支持的动态音频 Job 类型：{record.get('job_type')!r}")
        target_type = _text(record.get("target_type"))
        if target_type != _TARGET_TYPE:
            raise DynamicAudioJobError("动态音频 Job 的 target_type 必须为 shot_audio")
        provider_kind = _text(record.get("provider_kind"))
        if provider_kind != "tts":
            raise DynamicAudioJobError("动态音频 Job 的 provider_kind 必须为 tts")

        raw_input = record.get("input_data")
        if not isinstance(raw_input, Mapping):
            raw_input = json_loads(record.get("input_json"), {})
        input_data = dict(raw_input) if isinstance(raw_input, Mapping) else {}

        job_id = _text(record.get("id"))
        project_id = _text(record.get("project_id"))
        if not job_id:
            raise DynamicAudioJobError("动态音频 Job 缺少 Job ID")
        if not project_id:
            raise DynamicAudioJobError("动态音频 Job 缺少 project_id")

        workflow_run_id = self._consistent_text(
            "workflow_run_id",
            record.get("workflow_run_id"),
            _mapping_value(input_data, "workflow_run_id", "run_id"),
        )
        workflow_stage_id = self._consistent_text(
            "workflow_stage_id",
            record.get("workflow_stage_id"),
            _mapping_value(input_data, "workflow_stage_id", "stage_id"),
        )
        shot_id = self._consistent_text(
            "shot_id",
            record.get("target_id"),
            _mapping_value(input_data, "shot_id", "target_id"),
        )
        dialogue_line_id = _text(_mapping_value(input_data, "dialogue_line_id", "line_id"))
        line_order_value = _mapping_value(input_data, "line_order")
        if not dialogue_line_id:
            raise DynamicAudioJobError("动态音频 Job 缺少 dialogue_line_id")
        if isinstance(line_order_value, bool):
            raise DynamicAudioJobError("dialogue line_order 必须为正整数")
        try:
            line_order = int(line_order_value)
        except (TypeError, ValueError) as exc:
            raise DynamicAudioJobError("动态音频 Job 缺少有效的 line_order") from exc
        if line_order < 1 or str(line_order) != str(line_order_value).strip():
            # 允许 JSON 中的 1.0，但拒绝负数、小数和非数字字符串。
            if not isinstance(line_order_value, int) or line_order < 1:
                raise DynamicAudioJobError("dialogue line_order 必须为正整数")

        text_value = _mapping_value(input_data, "text", "dialogue_text", "dialogue", "content")
        snapshot_value = _mapping_value(input_data, "source_text_snapshot")
        source_text_snapshot = _snapshot_text(
            snapshot_value if snapshot_value is not None else text_value
        )
        text = _text(text_value if text_value is not None else source_text_snapshot)
        if not text:
            raise DynamicAudioJobError("动态音频 Job 缺少可生成音频的对白文本")
        if not source_text_snapshot.strip():
            source_text_snapshot = text

        speaker = _text(_mapping_value(input_data, "speaker", "speaker_name", "character", "name"))
        voice_id = _text(_mapping_value(input_data, "voice_id", "voice"))
        provider_id = self._consistent_text(
            "provider_id",
            record.get("provider_id"),
            _mapping_value(input_data, "provider_id"),
        )
        provider_model = _text(_mapping_value(input_data, "provider_model", "model"))
        input_signature = self._consistent_text(
            "input_signature",
            record.get("input_signature"),
            _mapping_value(input_data, "input_signature"),
        )
        if not input_signature:
            raise DynamicAudioJobError("动态音频 Job 缺少 input_signature")
        if not workflow_run_id or not workflow_stage_id or not shot_id:
            raise DynamicAudioJobError(
                "动态音频 Job 必须包含 workflow_run_id、workflow_stage_id 和 shot_id"
            )

        upstream_input_signature = _text(
            _mapping_value(input_data, "upstream_input_signature", "stage_input_signature")
        )

        params = _mapping_value(input_data, "params")
        if params is None:
            params = {}
        if not isinstance(params, Mapping):
            raise DynamicAudioJobError("动态音频 Job 的 params 必须为对象")

        return _AudioInput(
            job_id=job_id,
            project_id=project_id,
            workflow_run_id=workflow_run_id,
            workflow_stage_id=workflow_stage_id,
            shot_id=shot_id,
            dialogue_line_id=dialogue_line_id,
            line_order=line_order,
            text=text,
            source_text_snapshot=source_text_snapshot,
            speaker=speaker,
            voice_id=voice_id,
            provider_id=provider_id,
            provider_model=provider_model,
            input_signature=input_signature,
            upstream_input_signature=upstream_input_signature,
            params=copy.deepcopy(dict(params)),
        )

    @staticmethod
    def _consistent_text(name: str, *values: Any) -> str:
        non_empty = [_text(value) for value in values if _text(value)]
        if len(set(non_empty)) > 1:
            raise DynamicAudioJobError(f"动态音频 Job 的 {name} 字段不一致")
        return non_empty[0] if non_empty else ""

    def _validate_shot_reference(self, audio_input: _AudioInput) -> None:
        """在 Provider 调用前确认 Shot 仍属于当前项目。"""

        with self.database.engine.connect() as connection:
            record = _row_dict(
                connection.execute(
                    select(shot.c.id, shot.c.project_id).where(shot.c.id == audio_input.shot_id)
                ).first()
            )
        if record is None:
            raise DynamicAudioJobError(f"分镜不存在：{audio_input.shot_id}")
        if record.get("project_id") != audio_input.project_id:
            raise DynamicAudioJobError("动态音频 Job 的 shot 不属于 project")

    @staticmethod
    def _validate_dynamic_state_in_connection(
        connection: Any,
        audio_input: _AudioInput,
        *,
        current_job: Mapping[str, Any] | None = None,
    ) -> None:
        job_record = current_job or _row_dict(
            connection.execute(select(job).where(job.c.id == audio_input.job_id)).first()
        )
        if job_record is None:
            raise DynamicAudioJobError("动态音频任务不存在")
        if job_record.get("status") not in _ACTIVE_JOB_STATUSES:
            raise _JobNoLongerActive("动态音频任务已取消或不再处于活动状态")
        if (
            job_record.get("workflow_run_id") != audio_input.workflow_run_id
            or job_record.get("workflow_stage_id") != audio_input.workflow_stage_id
            or job_record.get("project_id") != audio_input.project_id
            or _text(job_record.get("input_signature")) != audio_input.input_signature
        ):
            raise DynamicAudioJobError("动态音频 Job 关联或 input_signature 已发生变化")

        run_record = _row_dict(
            connection.execute(
                select(workflow_run).where(workflow_run.c.id == audio_input.workflow_run_id)
            ).first()
        )
        stage_record = _row_dict(
            connection.execute(
                select(workflow_stage).where(
                    workflow_stage.c.id == audio_input.workflow_stage_id,
                    workflow_stage.c.workflow_run_id == audio_input.workflow_run_id,
                )
            ).first()
        )
        if run_record is None or stage_record is None:
            raise DynamicAudioJobError("动态音频 Job 关联的 workflow run/stage 不存在")
        if run_record.get("project_id") != audio_input.project_id:
            raise DynamicAudioJobError("动态音频 Job 的 run 不属于 project")
        if run_record.get("status") not in {"queued", "running"}:
            raise _JobNoLongerActive(
                f"动态音频 Job 的 workflow run 当前不可执行：{run_record.get('status')!r}"
            )
        if stage_record.get("status") not in {"queued", "running"}:
            raise _JobNoLongerActive(
                f"动态音频 Job 的 workflow stage 当前不可执行：{stage_record.get('status')!r}"
            )
        stage_signature = _text(stage_record.get("input_signature"))
        if stage_signature and audio_input.upstream_input_signature != stage_signature:
            raise DynamicAudioJobError(
                "动态音频 Job 的 upstream_input_signature 与 stage input_signature 不一致"
            )
        if stage_signature and not audio_input.upstream_input_signature:
            raise DynamicAudioJobError("动态音频 Job 缺少 stage upstream_input_signature")

    def _assert_dynamic_state(self, audio_input: _AudioInput) -> None:
        with self.database.engine.connect() as connection:
            self._validate_dynamic_state_in_connection(connection, audio_input)

    @staticmethod
    def _is_edge_input(audio_input: _AudioInput) -> bool:
        edge_provider_ids = {"edge-tts", "edge_tts", "edge"}
        return audio_input.voice_id.startswith(_EDGE_PREFIX) or (
            audio_input.provider_id.casefold() in edge_provider_ids
        )

    async def _resolve_provider(
        self,
        audio_input: _AudioInput,
    ) -> tuple[Any, str | None, str]:
        if self._is_edge_input(audio_input):
            if not audio_input.voice_id.startswith(_EDGE_PREFIX):
                raise DynamicAudioJobError("Edge TTS Job 的 voice_id 必须以 edge-tts: 开头")
            return None, None, audio_input.voice_id.removeprefix(_EDGE_PREFIX)

        resolved = self.provider_service.resolve(
            kind="tts",
            provider_id=audio_input.provider_id or None,
        )
        if inspect.isawaitable(resolved):
            resolved = await resolved
        provider: Any
        config: Any = {}
        if isinstance(resolved, tuple):
            if not resolved:
                raise DynamicAudioJobError("TTS Provider resolve 返回为空")
            provider = resolved[0]
            if len(resolved) > 1:
                config = resolved[1]
        else:
            provider = resolved

        resolved_provider_id = _text(_mapping_value(config, "id", "provider_id"))
        if not resolved_provider_id:
            resolved_provider_id = audio_input.provider_id or None
        provider_model = audio_input.provider_model or _text(
            _mapping_value(config, "model", "provider_model")
        )
        if not provider_model:
            provider_config = getattr(provider, "config", None)
            provider_model = _text(getattr(provider_config, "model", ""))
        if provider is None or not callable(getattr(provider, "synthesize", None)):
            raise DynamicAudioJobError("TTS Provider 不支持 synthesize")
        return provider, resolved_provider_id, provider_model

    async def _synthesize(
        self,
        provider: Any,
        audio_input: _AudioInput,
        *,
        provider_model: str,
    ) -> bytes:
        self._assert_dynamic_state(audio_input)
        if provider is None:
            return await self._synthesize_edge(audio_input)
        supports_voice = getattr(provider, "supports_voice", None)
        if callable(supports_voice) and audio_input.voice_id:
            supported = supports_voice(audio_input.voice_id)
            if inspect.isawaitable(supported):
                supported = await supported
            if supported is False:
                raise DynamicAudioJobError(f"TTS Provider 不支持音色：{audio_input.voice_id}")
        del provider_model  # model 已在 resolve 快照中保存，Provider 使用自己的配置。
        params = copy.deepcopy(audio_input.params)
        generated = provider.synthesize(
            text=audio_input.text,
            voice_id=audio_input.voice_id,
            params=params,
        )
        if inspect.isawaitable(generated):
            generated = await generated
        if not isinstance(generated, bytes | bytearray | memoryview):
            raise DynamicAudioJobError("TTS Provider 返回的数据不是音频字节")
        result = bytes(generated)
        if not result:
            raise DynamicAudioJobError("TTS Provider 返回空音频")
        return result

    async def _synthesize_edge(self, audio_input: _AudioInput) -> bytes:
        edge_voice = audio_input.voice_id.removeprefix(_EDGE_PREFIX).strip()
        if not edge_voice:
            raise DynamicAudioJobError("Edge TTS 音色不能为空")
        try:
            from app.providers.tts import edge_tts
        except ImportError as exc:
            raise DynamicAudioJobError("Edge TTS 不可用") from exc
        if not edge_tts.EDGE_TTS_AVAILABLE:
            raise DynamicAudioJobError("Edge TTS 未安装，请先安装 edge-tts")

        temporary_path: Path | None = None
        result = b""
        try:
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as temporary:
                temporary_path = Path(temporary.name)
            await edge_tts._generate_tts(audio_input.text, edge_voice, temporary_path)
            result = temporary_path.read_bytes()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise DynamicAudioJobError("Edge TTS 生成音频失败") from exc
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        if not result:
            raise DynamicAudioJobError("Edge TTS 返回空音频")
        return result

    def _find_reusable_audio(self, audio_input: _AudioInput) -> dict[str, Any] | None:
        with self.database.engine.connect() as connection:
            row = connection.execute(
                select(shot_audio)
                .where(
                    shot_audio.c.shot_id == audio_input.shot_id,
                    shot_audio.c.dialogue_line_id == audio_input.dialogue_line_id,
                    shot_audio.c.input_signature == audio_input.input_signature,
                    shot_audio.c.status == "completed",
                )
                .order_by(shot_audio.c.version_no.desc(), shot_audio.c.id.desc())
            ).first()
        record = _row_dict(row)
        if record is None:
            return None
        provenance = json_loads(record.get("provenance_json"), {})
        if not isinstance(provenance, Mapping):
            return None
        existing_run_id = _text(
            provenance.get("workflow_run_id") or provenance.get("run_id")
        )
        existing_stage_id = _text(
            provenance.get("workflow_stage_id") or provenance.get("stage_id")
        )
        if (
            existing_run_id != audio_input.workflow_run_id
            or existing_stage_id != audio_input.workflow_stage_id
        ):
            return None
        file_path = _text(record.get("file_path"))
        if not file_path or not self._valid_project_file(
            audio_input.project_id,
            file_path,
            require_non_empty=True,
        ):
            return None
        duration_value = record.get("duration_sec")
        if duration_value is None:
            return None
        try:
            duration = float(duration_value)
        except (TypeError, ValueError):
            return None
        if duration <= 0 or not math.isfinite(duration):
            return None
        return record

    def _valid_project_file(
        self,
        project_id: str,
        relative_path: str,
        *,
        require_non_empty: bool,
    ) -> bool:
        try:
            path = self.file_service.resolve_project_path(project_id, relative_path)
        except (FileServiceError, OSError, ValueError):
            return False
        if not path.is_file():
            return False
        if require_non_empty and path.stat().st_size <= 0:
            return False
        return True

    def _write_audio_file(self, audio_input: _AudioInput, data: bytes) -> tuple[str, Path]:
        suffix_value = _text(audio_input.params.get("response_format", "mp3")).lower()
        suffix = suffix_value.removeprefix(".")
        if suffix not in _AUDIO_SUFFIXES:
            suffix = "mp3"
        shot_hash = _hash_text(audio_input.shot_id)[:12]
        line_hash = _hash_text(audio_input.dialogue_line_id)[:12]
        signature_hash = _hash_text(audio_input.input_signature)[:12]
        for _ in range(8):
            filename = f"dialogue-{shot_hash}-{line_hash}-{signature_hash}-{uuid4().hex}.{suffix}"
            relative = (Path("分镜配音") / filename).as_posix()
            target = self.file_service.resolve_project_path(audio_input.project_id, relative)
            if target.exists():
                continue
            try:
                self.file_service.write_bytes_atomic(audio_input.project_id, relative, data)
            except FileExistsError:
                # 允许自定义 FileService 以独占模式拒绝竞态重名目标。
                continue
            if not target.is_file() or target.stat().st_size <= 0:
                self._remove_audio_file(audio_input.project_id, relative)
                raise DynamicAudioJobError("生成音频文件为空")
            return relative, target
        raise DynamicAudioJobError("无法为生成音频分配不冲突的文件名")

    def _remove_audio_file(self, project_id: str, relative_path: str) -> None:
        try:
            path = self.file_service.resolve_project_path(project_id, relative_path)
            if path.is_file():
                path.unlink()
        except (FileServiceError, OSError, ValueError):
            # 清理失败不能覆盖原始 Job 错误，也不能删除越界路径。
            return

    @staticmethod
    def _get_audio_duration(audio_path: Path) -> float:
        try:
            if not audio_path.is_file() or audio_path.stat().st_size <= 0:
                raise DynamicAudioJobError("无法读取生成音频时长：音频文件不存在或为空")
        except OSError as exc:
            raise DynamicAudioJobError("无法读取生成音频时长：音频文件不可读") from exc
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
                    str(audio_path),
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise DynamicAudioJobError("无法读取生成音频时长：ffprobe 执行失败") from exc
        value = result.stdout.strip()
        try:
            duration = float(value)
        except (TypeError, ValueError) as exc:
            raise DynamicAudioJobError("无法读取生成音频时长：ffprobe 返回值无效") from exc
        if duration <= 0 or not math.isfinite(duration):
            raise DynamicAudioJobError("无法读取生成音频时长：时长必须大于 0")
        return duration

    def _build_provenance(
        self,
        audio_input: _AudioInput,
        *,
        provider_id: str | None,
        provider_model: str,
    ) -> dict[str, Any]:
        text_hash = _hash_text(audio_input.text)
        source_text_hash = _hash_text(audio_input.source_text_snapshot)
        snapshot = {
            "workflow_run_id": audio_input.workflow_run_id,
            "workflow_stage_id": audio_input.workflow_stage_id,
            "run_id": audio_input.workflow_run_id,
            "stage_id": audio_input.workflow_stage_id,
            "provider_id": provider_id or "",
            "provider_model": provider_model,
            "model": provider_model,
            "voice_id": audio_input.voice_id,
            "voice": audio_input.voice_id,
            "text_hash": text_hash,
            "text_sha256": text_hash,
            "source_text_hash": source_text_hash,
            "input_signature": audio_input.input_signature,
            "upstream_input_signature": audio_input.upstream_input_signature,
        }
        return {
            "phase": "6a-audio",
            "job_id": audio_input.job_id,
            "workflow_run_id": audio_input.workflow_run_id,
            "workflow_stage_id": audio_input.workflow_stage_id,
            "run_id": audio_input.workflow_run_id,
            "stage_id": audio_input.workflow_stage_id,
            "shot_id": audio_input.shot_id,
            "dialogue_line_id": audio_input.dialogue_line_id,
            "line_order": audio_input.line_order,
            "speaker": audio_input.speaker,
            "voice_id": audio_input.voice_id,
            "provider_kind": "tts",
            "provider_id": provider_id or "",
            "provider_model": provider_model,
            "model": provider_model,
            "dialogue_text": audio_input.text,
            "source_text_snapshot": audio_input.source_text_snapshot,
            "text_hash": text_hash,
            "text_sha256": text_hash,
            "source_text_hash": source_text_hash,
            "input_signature": audio_input.input_signature,
            "upstream_input_signature": audio_input.upstream_input_signature,
            "input_snapshot": snapshot,
            "source": "generated",
        }

    def _commit_audio(
        self,
        audio_input: _AudioInput,
        *,
        file_path: str,
        duration_sec: float,
        provider_id: str | None,
        provider_model: str,
        provenance: Mapping[str, Any],
    ) -> _CommitOutcome:
        now = utc_now()
        with self.database.engine.begin() as connection:
            current_job = _row_dict(
                connection.execute(select(job).where(job.c.id == audio_input.job_id)).first()
            )
            if current_job is None:
                raise DynamicAudioJobError("动态音频任务不存在")
            if current_job["status"] == "completed":
                return _CommitOutcome(self._completed_result(current_job), False)
            if current_job["status"] not in _ACTIVE_JOB_STATUSES:
                return _CommitOutcome(self._status_result(current_job), False)
            self._validate_dynamic_state_in_connection(
                connection,
                audio_input,
                current_job=current_job,
            )

            shot_record = _row_dict(
                connection.execute(select(shot).where(shot.c.id == audio_input.shot_id)).first()
            )
            if shot_record is None:
                raise DynamicAudioJobError(f"分镜不存在：{audio_input.shot_id}")
            if shot_record.get("project_id") != audio_input.project_id:
                raise DynamicAudioJobError("动态音频 Job 的 shot 不属于 project")

            line_max_version = connection.scalar(
                select(func.max(shot_audio.c.version_no)).where(
                    shot_audio.c.shot_id == audio_input.shot_id,
                    shot_audio.c.dialogue_line_id == audio_input.dialogue_line_id,
                )
            )
            version_no = int(line_max_version or 0) + 1
            # 0017 之前的数据库仍有 (shot_id, version_no) 全局唯一约束。若该约束
            # 尚未由后续 schema lane 改成行级约束，使用不冲突的全局版本号作为兼容回退；
            # 行级版本数据库仍保持每个 dialogue_line_id 从 1 独立递增。
            if self._has_legacy_version_constraint():
                global_max_version = connection.scalar(
                    select(func.max(shot_audio.c.version_no)).where(
                        shot_audio.c.shot_id == audio_input.shot_id,
                    )
                )
                version_no = max(version_no, int(global_max_version or 0) + 1)
            audio_id = new_id("audio")
            final_provenance = dict(provenance)
            final_provenance.update(
                {
                    "audio_id": audio_id,
                    "version_no": version_no,
                    "file_path": file_path,
                    "duration_sec": duration_sec,
                }
            )
            connection.execute(
                update(shot_audio)
                .where(
                    shot_audio.c.shot_id == audio_input.shot_id,
                    shot_audio.c.dialogue_line_id == audio_input.dialogue_line_id,
                    shot_audio.c.is_current == 1,
                )
                .values(is_current=0)
            )
            connection.execute(
                insert(shot_audio).values(
                    id=audio_id,
                    shot_id=audio_input.shot_id,
                    dialogue_line_id=audio_input.dialogue_line_id,
                    line_order=audio_input.line_order,
                    dialogue_text=audio_input.text,
                    source_text_snapshot=audio_input.source_text_snapshot,
                    speaker_name=audio_input.speaker,
                    voice_id=audio_input.voice_id,
                    file_path=file_path,
                    duration_sec=duration_sec,
                    version_no=version_no,
                    provider_id=provider_id,
                    model=provider_model,
                    status="completed",
                    is_current=1,
                    source="generated",
                    input_signature=audio_input.input_signature,
                    provenance_json=json_dumps(final_provenance),
                    error="",
                    created_at=now,
                )
            )
            result = {
                "status": "completed",
                "audio_id": audio_id,
                "shot_id": audio_input.shot_id,
                "dialogue_line_id": audio_input.dialogue_line_id,
                "line_order": audio_input.line_order,
                "file_path": file_path,
                "duration_sec": duration_sec,
                "version_no": version_no,
                "provider_id": provider_id,
                "model": provider_model,
                "voice_id": audio_input.voice_id,
                "input_signature": audio_input.input_signature,
                "provenance": final_provenance,
            }
            updated = connection.execute(
                update(job)
                .where(job.c.id == audio_input.job_id, job.c.status.in_(_ACTIVE_JOB_STATUSES))
                .values(
                    status="completed",
                    progress_current=job.c.progress_total,
                    message="任务完成",
                    error="",
                    result_json=json_dumps(result),
                    finished_at=now,
                    updated_at=now,
                )
            )
            if updated.rowcount != 1:
                raise _JobNoLongerActive("动态音频任务状态已改变")
            return _CommitOutcome(result, True)

    def _has_legacy_version_constraint(self) -> bool:
        try:
            constraints = sa_inspect(self.database.engine).get_unique_constraints("shot_audio")
        except (OSError, RuntimeError):
            return True
        return any(
            tuple(item.get("column_names") or ()) == ("shot_id", "version_no")
            for item in constraints
        )

    def _complete_existing_job(
        self,
        audio_input: _AudioInput,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        job_id = audio_input.job_id
        with self.database.engine.begin() as connection:
            current = _row_dict(connection.execute(select(job).where(job.c.id == job_id)).first())
            if current is None:
                return self._failure_result("failed", "动态音频任务不存在")
            if current["status"] == "completed":
                return self._completed_result(current)
            if current["status"] not in _ACTIVE_JOB_STATUSES:
                return self._status_result(current)
            self._validate_dynamic_state_in_connection(
                connection,
                audio_input,
                current_job=current,
            )
            audio_id = _text(result.get("audio_id"))
            if audio_id:
                connection.execute(
                    update(shot_audio)
                    .where(
                        shot_audio.c.shot_id == audio_input.shot_id,
                        shot_audio.c.dialogue_line_id == audio_input.dialogue_line_id,
                        shot_audio.c.is_current == 1,
                        shot_audio.c.id != audio_id,
                    )
                    .values(is_current=0)
                )
                connection.execute(
                    update(shot_audio).where(shot_audio.c.id == audio_id).values(is_current=1)
                )
            now = utc_now()
            updated = connection.execute(
                update(job)
                .where(job.c.id == job_id, job.c.status.in_(_ACTIVE_JOB_STATUSES))
                .values(
                    status="completed",
                    progress_current=job.c.progress_total,
                    message="任务完成（复用已有音频）",
                    error="",
                    result_json=json_dumps(result),
                    finished_at=now,
                    updated_at=now,
                )
            )
            if updated.rowcount != 1:
                latest = _row_dict(
                    connection.execute(select(job).where(job.c.id == job_id)).first()
                )
                return self._status_result(latest or current)
        return result

    def _audio_result_from_row(self, record: Mapping[str, Any]) -> dict[str, Any]:
        provenance = json_loads(record.get("provenance_json"), {})
        return {
            "status": "completed",
            "audio_id": record.get("id"),
            "shot_id": record.get("shot_id"),
            "dialogue_line_id": record.get("dialogue_line_id"),
            "line_order": record.get("line_order"),
            "file_path": record.get("file_path"),
            "duration_sec": record.get("duration_sec"),
            "version_no": record.get("version_no"),
            "provider_id": record.get("provider_id"),
            "model": record.get("model", ""),
            "voice_id": record.get("voice_id", ""),
            "input_signature": record.get("input_signature", ""),
            "provenance": provenance if isinstance(provenance, Mapping) else {},
        }

    def _completed_result(self, record: Mapping[str, Any]) -> dict[str, Any]:
        result = _decode_result(record)
        if result:
            result.setdefault("status", "completed")
            return result
        return {"status": "completed"}

    def _status_result(self, record: Mapping[str, Any]) -> dict[str, Any]:
        status = _text(record.get("status")) or "failed"
        result = _decode_result(record)
        if result:
            result.setdefault("status", status)
            return result
        return {
            "status": status,
            "error": sanitize_error_message(record.get("error", "")),
        }

    def _fail_job(self, job_id: str, error: str) -> dict[str, Any]:
        safe_error = sanitize_error_message(error)
        with self.database.engine.begin() as connection:
            current = _row_dict(connection.execute(select(job).where(job.c.id == job_id)).first())
            if current is None:
                return self._failure_result("failed", safe_error)
            if current["status"] in _TERMINAL_JOB_STATUSES:
                return self._status_result(current)
            now = utc_now()
            connection.execute(
                update(job)
                .where(job.c.id == job_id, job.c.status.in_(_ACTIVE_JOB_STATUSES))
                .values(
                    status="failed",
                    error=safe_error,
                    message="任务失败",
                    finished_at=now,
                    updated_at=now,
                )
            )
        return self._failure_result("failed", safe_error)

    def _interrupt_job(self, job_id: str, error: str) -> dict[str, Any]:
        safe_error = sanitize_error_message(error)
        with self.database.engine.begin() as connection:
            current = _row_dict(connection.execute(select(job).where(job.c.id == job_id)).first())
            if current is None:
                return self._failure_result("interrupted", safe_error)
            if current["status"] in _TERMINAL_JOB_STATUSES:
                return self._status_result(current)
            now = utc_now()
            connection.execute(
                update(job)
                .where(job.c.id == job_id, job.c.status.in_(_ACTIVE_JOB_STATUSES))
                .values(
                    status="interrupted",
                    error=safe_error,
                    message="任务因中断而停止",
                    finished_at=now,
                    updated_at=now,
                )
            )
        return self._failure_result("interrupted", safe_error)

    @staticmethod
    def _failure_result(status: str, error: str) -> dict[str, Any]:
        return {"status": status, "error": sanitize_error_message(error)}

    @staticmethod
    def _safe_error(prefix: str, error: BaseException) -> str:
        message = sanitize_error_message(error)
        return f"{prefix}：{message}" if message else prefix


__all__ = ["DynamicAudioJobError", "DynamicAudioJobService"]
