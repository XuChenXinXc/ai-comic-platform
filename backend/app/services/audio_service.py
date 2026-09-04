"""音频服务

处理配音生成、音频管理等功能。
"""

import logging
from pathlib import Path
from typing import Any

from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError

from app.db import Database, db
from app.models.tables import (
    api_provider,
    audio_config,
    project,
    project_bgm,
    shot,
    shot_audio,
)
from app.services.errors import bad_request, conflict, not_found
from app.services.ffmpeg_utils import get_video_duration, has_audio_stream
from app.services.file_service import FileServiceError, atomic_write_bytes, file_service
from app.services.job_service import JobService
from app.services.provider_service import ProviderService
from app.services.utils import (
    json_dumps,
    json_loads,
    new_id,
    row_dict,
    sanitize_error_message,
    utc_now,
)

logger = logging.getLogger(__name__)


class AudioService:
    def __init__(
        self,
        database: Database | None = None,
        jobs: JobService | None = None,
    ) -> None:
        self.database = database or db
        self.jobs = jobs or JobService(self.database)
        self.providers = ProviderService(self.database)

    def get_project_audio_config(self, project_id: str) -> dict[str, Any]:
        """获取项目音频配置"""
        with self.database.engine.connect() as connection:
            record = connection.execute(
                select(audio_config).where(audio_config.c.project_id == project_id)
            ).first()

        if record is None:
            # 返回默认配置
            return {
                "project_id": project_id,
                "tts_provider_id": None,
                "bgm_volume": 0.3,
                "dialogue_volume": 1.0,
                "sound_effect_volume": 0.5,
                "voice_mapping": {},
            }

        result = row_dict(record)
        if result is None:
            # row_dict 仅在传入 None 时返回 None，此处 record 已非空，仅为类型收窄
            raise not_found(f"音频配置不存在：{project_id}")
        # 音色映射以结构化字段返回；解析失败回退空映射，不阻断配置读取
        voice_mapping = json_loads(result.pop("voice_mapping_json", ""), {})
        if not isinstance(voice_mapping, dict):
            voice_mapping = {}
        result["voice_mapping"] = voice_mapping
        return result

    def update_project_audio_config(
        self, project_id: str, updates: dict[str, Any]
    ) -> dict[str, Any]:
        """更新项目音频配置"""
        now = utc_now()
        # 列白名单：只允许更新已知配置字段，防止客户端写入任意列
        # （如 project_id/updated_at）或非法类型值。
        allowed_columns = {
            "tts_provider_id",
            "bgm_volume",
            "dialogue_volume",
            "sound_effect_volume",
            "voice_mapping",
        }
        filtered = {key: value for key, value in updates.items() if key in allowed_columns}
        if not filtered:
            raise bad_request("没有可更新的音频配置字段")

        # 音量必须在 [0, 1]：CHECK 约束违规会以未捕获 IntegrityError 500 暴露，
        # 这里前置校验并给出明确 400。
        for volume_key in ("bgm_volume", "dialogue_volume", "sound_effect_volume"):
            if volume_key not in filtered:
                continue
            raw = filtered[volume_key]
            if isinstance(raw, bool) or not isinstance(raw, int | float):
                raise bad_request(f"{volume_key} 必须是 0 到 1 之间的数字")
            volume = float(raw)
            if not 0 <= volume <= 1:
                raise bad_request(f"{volume_key} 必须在 0 到 1 之间")
            filtered[volume_key] = volume
        # tts_provider_id 空串归一化为 None：写入空串会触发 FK 约束 500。
        if filtered.get("tts_provider_id") == "":
            filtered["tts_provider_id"] = None

        # 音色映射校验后转换为 voice_mapping_json 列存储
        if "voice_mapping" in filtered:
            mapping = filtered.pop("voice_mapping")
            if not isinstance(mapping, dict):
                raise bad_request("voice_mapping 必须是角色名到音色 ID 的映射对象")
            if len(mapping) > 200:
                raise bad_request("voice_mapping 数量超过上限（最多 200 条）")
            for role_name, voice_id in mapping.items():
                if not isinstance(role_name, str) or not role_name.strip():
                    raise bad_request("voice_mapping 的键必须是非空角色名")
                if not isinstance(voice_id, str) or not voice_id.strip():
                    raise bad_request("voice_mapping 的值必须是非空音色 ID")
            filtered["voice_mapping_json"] = json_dumps(mapping)

        with self.database.engine.begin() as connection:
            # 检查项目是否存在
            proj = connection.execute(
                select(project.c.id).where(project.c.id == project_id)
            ).first()
            if proj is None:
                raise not_found(f"项目不存在：{project_id}")

            # 检查 TTS Provider 是否存在且类型正确
            if "tts_provider_id" in filtered and filtered["tts_provider_id"]:
                provider_record = connection.execute(
                    select(api_provider.c.id, api_provider.c.kind).where(
                        api_provider.c.id == filtered["tts_provider_id"]
                    )
                ).first()
                if provider_record is None:
                    raise bad_request("TTS Provider 不存在")
                if provider_record.kind != "tts":
                    raise bad_request("指定的 Provider 不是 TTS 类型")

            # 检查配置是否已存在
            existing = connection.execute(
                select(audio_config.c.project_id).where(audio_config.c.project_id == project_id)
            ).first()

            if existing:
                # 更新
                connection.execute(
                    update(audio_config)
                    .where(audio_config.c.project_id == project_id)
                    .values(**filtered, updated_at=now)
                )
            else:
                # 插入
                connection.execute(
                    insert(audio_config).values(project_id=project_id, updated_at=now, **filtered)
                )

        return self.get_project_audio_config(project_id)

    def extract_dialogues_from_screenplay(self, chapter_id: str) -> list[dict[str, Any]]:
        """从剧本提取所有对话"""
        from app.services.writing_service import WritingService

        writing = WritingService(self.database)
        screenplay_record = writing.get_screenplay(chapter_id)

        if not screenplay_record or screenplay_record["status"] != "confirmed":
            raise bad_request("剧本未确认，无法提取对话")

        content = json_loads(screenplay_record["content_json"], {})
        scenes = content.get("scenes", [])

        dialogues = []
        for scene in scenes:
            for shot_data in scene.get("shots", []):
                shot_no = shot_data.get("shot_no")
                dialogue_list = shot_data.get("dialogue", [])

                for dialogue_item in dialogue_list:
                    speaker = dialogue_item.get("speaker", "")
                    text = dialogue_item.get("text", "")
                    if text.strip():
                        dialogues.append(
                            {
                                "shot_no": shot_no,
                                "speaker": speaker,
                                "text": text,
                            }
                        )

        return dialogues

    async def generate_shot_audio(
        self,
        shot_id: str,
        dialogue_text: str,
        voice_id: str,
        *,
        tts_provider_id: str | None = None,
    ) -> dict[str, Any]:
        """为单个分镜生成配音

        支持两种模式：
        1. Edge TTS：voice_id 以 "edge-tts:" 开头，如 "edge-tts:zh-CN-XiaoxiaoNeural"
        2. Provider TTS：使用配置的 TTS Provider
        """
        # 第一步：事务外完成 TTS 网络调用与文件生成，
        # 避免 SQLite 写事务跨越网络往返导致 database is locked。
        with self.database.engine.connect() as connection:
            shot_record = connection.execute(
                select(shot.c.id, shot.c.project_id).where(shot.c.id == shot_id)
            ).first()
            if shot_record is None:
                raise not_found(f"分镜不存在：{shot_id}")

            project_id = shot_record.project_id

        # 检查是否使用 Edge TTS
        use_edge_tts = voice_id.startswith("edge-tts:")
        edge_voice = ""
        provider_runtime: dict[str, Any] = {}
        if use_edge_tts:
            edge_voice = voice_id.replace("edge-tts:", "")
            from app.providers.tts.edge_tts import COMMON_VOICES

            if edge_voice not in {voice["name"] for voice in COMMON_VOICES}:
                raise bad_request(f"不支持的 Edge TTS 音色：{edge_voice}")
        else:
            if not tts_provider_id:
                config = self.get_project_audio_config(project_id)
                tts_provider_id = config.get("tts_provider_id")

            if not tts_provider_id:
                raise bad_request("未配置 TTS Provider")

        # 在事务外执行 TTS 生成（网络调用 / Edge TTS）
        if use_edge_tts:
            audio_data = await self._generate_with_edge_tts(dialogue_text, edge_voice)
            provider_id_to_save = None  # Edge TTS 不需要 Provider ID
        else:
            # 通过公开接口解析 TTS Provider（含 kind 校验与 API Key 解密），
            # 不再绕过私有 _runtime_config/_decode 组合。
            resolved_provider_id = str(tts_provider_id)
            tts_provider, provider_runtime = self.providers.resolve_tts_provider(
                resolved_provider_id
            )
            if provider_runtime["adapter"] not in {"openai_tts", "http_tts"}:
                raise bad_request(f"不支持的 TTS adapter：{provider_runtime['adapter']}")
            if not provider_runtime.get("api_key"):
                raise bad_request("TTS Provider 未配置 API Key")

            try:
                audio_data = await tts_provider.synthesize(
                    text=dialogue_text,
                    voice_id=voice_id,
                    params=provider_runtime["params"],
                )
            except Exception as exc:
                logger.error("TTS 生成失败：%s", sanitize_error_message(exc))
                raise bad_request(
                    f"TTS 生成失败：{sanitize_error_message(exc)}"
                ) from exc

            provider_id_to_save = resolved_provider_id

        model_to_save = edge_voice if use_edge_tts else str(provider_runtime.get("model") or "")

        # 第二步：事务外写文件并探测时长（文件 IO 与 ffprobe 不进入写事务）
        project_path = file_service.project_path(project_id)
        audio_dir = project_path / "分镜配音"
        audio_dir.mkdir(parents=True, exist_ok=True)

        # 短事务读取当前最大版本号（仅读取，不做网络/文件 IO）
        with self.database.engine.connect() as connection:
            max_version = connection.execute(
                select(shot_audio.c.version_no)
                .where(shot_audio.c.shot_id == shot_id)
                .order_by(shot_audio.c.version_no.desc())
            ).first()
        version_no = (max_version[0] + 1) if max_version else 1
        # 文件名以本次生成唯一 ID 结尾：并发请求可能读到相同 version_no，
        # 若文件名只由版本号决定，冲突方清理文件时会误删成功方已入库引用的文件。
        audio_id = new_id("audio")
        audio_filename = f"shot-{shot_id}-v{version_no:03d}-{audio_id}.mp3"
        audio_path = audio_dir / audio_filename

        try:
            atomic_write_bytes(audio_path, audio_data)
            # 获取音频时长；失败时抛明确错误，不写入 0 时长
            duration_sec = await self._get_audio_duration(audio_path)
        except Exception:
            # 失败时清理本次写入的文件，避免残留孤儿文件
            audio_path.unlink(missing_ok=True)
            raise

        # 第三步：短事务内只做版本切换与行插入
        now = utc_now()
        try:
            with self.database.engine.begin() as connection:
                # 取消当前版本
                connection.execute(
                    update(shot_audio)
                    .where(shot_audio.c.shot_id == shot_id)
                    .values(is_current=0)
                )
                # 插入数据库
                connection.execute(
                    insert(shot_audio).values(
                        id=audio_id,
                        shot_id=shot_id,
                        dialogue_text=dialogue_text,
                        speaker_name="",
                        voice_id=voice_id,
                        file_path=str(audio_path.relative_to(project_path)),
                        duration_sec=duration_sec,
                        version_no=version_no,
                        provider_id=provider_id_to_save,  # Edge TTS 时为 None
                        model=model_to_save,
                        status="completed",
                        is_current=1,
                        source="generated",
                        created_at=now,
                    )
                )
        except IntegrityError:
            # 并发生成导致版本号被抢占：文件名含本次生成唯一 ID，
            # 清理的是本次自己的文件，不影响已成功入库的并发请求。
            audio_path.unlink(missing_ok=True)
            raise conflict("配音版本号冲突，请重试") from None

        return self.get_shot_audio(shot_id)

    async def _generate_with_edge_tts(self, text: str, voice: str) -> bytes:
        """使用 Edge TTS 生成音频"""
        import tempfile

        from app.providers.tts import edge_tts

        if not edge_tts.EDGE_TTS_AVAILABLE:
            raise bad_request("Edge TTS 未安装，请运行: pip install edge-tts")

        # 使用临时文件
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as temp_file:
            temp_path = Path(temp_file.name)

        try:
            # 生成音频
            await edge_tts._generate_tts(text, voice, temp_path)

            # 读取音频数据
            audio_data = temp_path.read_bytes()

            return audio_data
        finally:
            # 清理临时文件
            temp_path.unlink(missing_ok=True)

    def get_shot_audio(self, shot_id: str) -> dict[str, Any] | None:
        """获取分镜的当前配音"""
        with self.database.engine.connect() as connection:
            record = connection.execute(
                select(shot_audio).where(
                    shot_audio.c.shot_id == shot_id, shot_audio.c.is_current == 1
                )
            ).first()

        if record is None:
            return None

        return row_dict(record)

    def list_shot_audios(self, shot_id: str) -> list[dict[str, Any]]:
        """列出分镜所有配音版本"""
        with self.database.engine.connect() as connection:
            records = connection.execute(
                select(shot_audio)
                .where(shot_audio.c.shot_id == shot_id)
                .order_by(shot_audio.c.version_no.desc())
            ).fetchall()

        return [row_dict(r) for r in records]

    def list_chapter_audios(self, chapter_id: str) -> list[dict[str, Any]]:
        """列出章节内所有分镜的当前配音。"""
        with self.database.engine.connect() as connection:
            rows = connection.execute(
                select(
                    shot.c.id.label("shot_id"),
                    shot.c.shot_no,
                    shot_audio.c.id.label("audio_id"),
                    shot_audio.c.dialogue_text,
                    shot_audio.c.voice_id,
                    shot_audio.c.file_path,
                    shot_audio.c.duration_sec,
                    shot_audio.c.status,
                )
                .outerjoin(
                    shot_audio,
                    (shot_audio.c.shot_id == shot.c.id)
                    & (shot_audio.c.is_current == 1),
                )
                .where(shot.c.chapter_id == chapter_id)
                .order_by(shot.c.shot_no)
            ).fetchall()

        return [
            {
                "shot_id": row.shot_id,
                "shot_no": row.shot_no,
                "audio": (
                    {
                        "id": row.audio_id,
                        "dialogue_text": row.dialogue_text,
                        "voice_id": row.voice_id,
                        "file_path": row.file_path,
                        "duration_sec": row.duration_sec,
                        "status": row.status,
                    }
                    if row.audio_id
                    else None
                ),
            }
            for row in rows
        ]

    def delete_shot_audio(self, audio_id: str) -> None:
        """删除一个配音版本；删除当前版本后自动提升最新版本为当前。"""
        project_id = ""
        with self.database.engine.begin() as connection:
            record = connection.execute(
                select(shot_audio).where(shot_audio.c.id == audio_id)
            ).first()
            if record is None:
                raise not_found(f"配音不存在：{audio_id}")
            shot_id = record.shot_id
            file_path = record.file_path
            was_current = record.is_current == 1
            shot_row = connection.execute(
                select(shot.c.project_id).where(shot.c.id == shot_id)
            ).first()
            if shot_row is not None:
                project_id = shot_row.project_id
            connection.execute(delete(shot_audio).where(shot_audio.c.id == audio_id))
            if was_current:
                latest = connection.execute(
                    select(shot_audio.c.id)
                    .where(shot_audio.c.shot_id == shot_id)
                    .order_by(shot_audio.c.version_no.desc())
                    .limit(1)
                ).first()
                if latest is not None:
                    connection.execute(
                        update(shot_audio)
                        .where(shot_audio.c.id == latest.id)
                        .values(is_current=1)
                    )

        if file_path and project_id:
            try:
                target = file_service.resolve_project_path(project_id, file_path)
                target.unlink(missing_ok=True)
            except (FileServiceError, OSError):
                logger.warning("删除配音文件失败：%s", file_path)

    def list_project_bgms(self, project_id: str) -> list[dict[str, Any]]:
        """列出项目所有 BGM"""
        with self.database.engine.connect() as connection:
            proj = connection.execute(
                select(project.c.id).where(project.c.id == project_id)
            ).first()
            if proj is None:
                raise not_found(f"项目不存在：{project_id}")
            records = connection.execute(
                select(project_bgm)
                .where(project_bgm.c.project_id == project_id)
                .order_by(project_bgm.c.created_at.desc())
            ).fetchall()

        return [row_dict(r) for r in records]

    async def save_bgm(
        self,
        project_id: str,
        name: str,
        file_data: bytes,
        mood_tags: list[str],
    ) -> dict[str, Any]:
        """保存 BGM 文件

        文件写入与时长探测在事务外完成；数据库短事务内只做行插入，
        避免文件 IO / ffprobe 占用写事务。
        """
        # 短事务检查项目是否存在
        with self.database.engine.connect() as connection:
            proj = connection.execute(
                select(project.c.id).where(project.c.id == project_id)
            ).first()
            if proj is None:
                raise not_found(f"项目不存在：{project_id}")

        # 事务外保存文件并探测时长
        project_path = file_service.project_path(project_id)
        bgm_dir = project_path / "BGM"
        bgm_dir.mkdir(parents=True, exist_ok=True)

        bgm_id = new_id("bgm")
        bgm_filename = f"bgm-{bgm_id}.mp3"
        bgm_path = bgm_dir / bgm_filename

        try:
            atomic_write_bytes(bgm_path, file_data)
            # 获取音频时长；失败时抛明确错误，不写入 0 时长
            duration_sec = await self._get_audio_duration(bgm_path)
            # 校验确实包含音频流：视频/图片文件也能被 ffprobe 读到时长，
            # 不校验会把视频文件存成 .mp3 当 BGM 使用。
            if not await has_audio_stream(bgm_path):
                raise bad_request("上传的 BGM 文件不包含音频流，请上传音频文件")
        except Exception:
            # 失败时清理本次写入的文件，避免残留孤儿文件
            bgm_path.unlink(missing_ok=True)
            raise

        # 短事务插入数据库；插入失败同样清理本次写入的文件，不留孤儿。
        now = utc_now()
        try:
            with self.database.engine.begin() as connection:
                connection.execute(
                    insert(project_bgm).values(
                        id=bgm_id,
                        project_id=project_id,
                        name=name,
                        file_path=str(bgm_path.relative_to(project_path)),
                        duration_sec=duration_sec,
                        mood_tags=json_dumps(mood_tags),
                        created_at=now,
                    )
                )
        except Exception:
            bgm_path.unlink(missing_ok=True)
            raise

        return self._get_bgm_record(bgm_id)

    def _get_bgm_record(self, bgm_id: str) -> dict[str, Any]:
        """读取单条 BGM 记录"""
        with self.database.engine.connect() as connection:
            record = connection.execute(
                select(project_bgm).where(project_bgm.c.id == bgm_id)
            ).first()
        if record is None:
            raise not_found(f"BGM 不存在：{bgm_id}")
        result = row_dict(record)
        if result is None:
            raise not_found(f"BGM 不存在：{bgm_id}")
        return result

    def delete_bgm(self, bgm_id: str) -> None:
        """删除 BGM"""
        file_path_to_remove: str | None = None
        with self.database.engine.begin() as connection:
            # 获取文件路径
            record = connection.execute(
                select(project_bgm.c.project_id, project_bgm.c.file_path).where(
                    project_bgm.c.id == bgm_id
                )
            ).first()

            if record is None:
                raise not_found(f"BGM 不存在：{bgm_id}")

            # 先删数据库记录：若随后删文件失败，DB 行已删除不会造成
            # 「DB 回滚但文件已删」的反向不一致。
            connection.execute(delete(project_bgm).where(project_bgm.c.id == bgm_id))
            file_path_to_remove = str(record.file_path or "")
            project_id_for_removal = str(record.project_id or "")

        # 事务提交后再尽力删除文件；失败仅记日志，不影响删除结果。
        # 路径必须经 resolve_project_path 校验，防止越出项目目录删除任意文件。
        if file_path_to_remove:
            try:
                bgm_path = file_service.resolve_project_path(
                    project_id_for_removal, file_path_to_remove
                )
                bgm_path.unlink(missing_ok=True)
            except (FileServiceError, OSError) as exc:
                logger.warning("BGM 删除后清理文件失败：%s", exc)

    @staticmethod
    async def _get_audio_duration(audio_path: Path) -> float:
        """获取音频时长（ffprobe 异步封装，音视频共用 format=duration 探测）。

        失败时抛出明确错误，不再静默返回 0 入库。
        """
        try:
            # get_video_duration 探测 format=duration，对音频文件同样适用
            return await get_video_duration(audio_path)
        except ValueError as exc:
            raise bad_request(f"音频时长探测失败：{exc}") from exc
