"""音频服务

处理配音生成、音频管理等功能。
"""

import logging
import subprocess
from pathlib import Path
from typing import Any

from sqlalchemy import delete, insert, select, update

from app.config import settings
from app.db import Database, db
from app.models.tables import (
    api_provider,
    audio_config,
    project,
    project_bgm,
    shot,
    shot_audio,
)
from app.providers.registry import provider_registry
from app.providers.tts.openai_tts import OpenAITTSProvider
from app.services.errors import bad_request, not_found
from app.services.file_service import FileServiceError, atomic_write_bytes, file_service
from app.services.job_service import JobService
from app.services.provider_service import ProviderService
from app.services.utils import json_dumps, json_loads, new_id, row_dict, utc_now

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
            }

        return row_dict(record)

    def update_project_audio_config(self, project_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        """更新项目音频配置"""
        now = utc_now()

        with self.database.engine.begin() as connection:
            # 检查项目是否存在
            proj = connection.execute(select(project.c.id).where(project.c.id == project_id)).first()
            if proj is None:
                raise not_found(f"项目不存在：{project_id}")

            # 检查 TTS Provider 是否存在且类型正确
            if "tts_provider_id" in updates and updates["tts_provider_id"]:
                provider_record = connection.execute(
                    select(api_provider.c.id, api_provider.c.kind).where(
                        api_provider.c.id == updates["tts_provider_id"]
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
                    .values(**updates, updated_at=now)
                )
            else:
                # 插入
                connection.execute(
                    insert(audio_config).values(project_id=project_id, updated_at=now, **updates)
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
        """为单个分镜生成配音"""
        with self.database.engine.begin() as connection:
            # 检查分镜是否存在
            shot_record = connection.execute(
                select(shot.c.id, shot.c.project_id).where(shot.c.id == shot_id)
            ).first()
            if shot_record is None:
                raise not_found(f"分镜不存在：{shot_id}")

            project_id = shot_record.project_id

            # 获取 TTS Provider
            if not tts_provider_id:
                config = self.get_project_audio_config(project_id)
                tts_provider_id = config.get("tts_provider_id")

            if not tts_provider_id:
                raise bad_request("未配置 TTS Provider")

            provider_record = connection.execute(
                select(api_provider).where(api_provider.c.id == tts_provider_id)
            ).first()
            if provider_record is None:
                raise not_found("TTS Provider 不存在")

            provider_dict = row_dict(provider_record)

            # 获取运行时配置（包含解密的 API Key）
            runtime_config = self.providers._runtime_config(
                self.providers._decode(provider_dict, redact_base_url=False, include_secret=True)
            )
            api_key = runtime_config.get("api_key", "")
            if not api_key:
                raise bad_request("TTS Provider 未配置 API Key")

            # 创建 Provider 实例
            from app.schemas import ProviderConfig

            provider_config = ProviderConfig(
                kind="tts",
                adapter=provider_dict["adapter"],
                base_url=provider_dict["base_url"],
                model=provider_dict["model"],
                params=json_loads(provider_dict["params_json"], {}),
                timeout_sec=provider_dict["timeout_sec"],
                max_retries=provider_dict["max_retries"],
            )

            if provider_dict["adapter"] != "openai_tts":
                raise bad_request(f"不支持的 TTS adapter：{provider_dict['adapter']}")

            tts_provider = OpenAITTSProvider(provider_config, api_key)

            # 调用 TTS API
            try:
                audio_data = await tts_provider.synthesize(
                    text=dialogue_text,
                    voice_id=voice_id,
                    params=provider_config.params,
                )
            except Exception as exc:
                logger.error(f"TTS 生成失败：{exc}")
                raise bad_request(f"TTS 生成失败：{exc}") from exc

            # 保存音频文件
            project_path = file_service.project_path(project_id)
            audio_dir = project_path / "分镜配音"
            audio_dir.mkdir(parents=True, exist_ok=True)

            # 获取当前最大版本号
            max_version = connection.execute(
                select(shot_audio.c.version_no)
                .where(shot_audio.c.shot_id == shot_id)
                .order_by(shot_audio.c.version_no.desc())
            ).first()
            version_no = (max_version[0] + 1) if max_version else 1

            audio_filename = f"shot-{shot_id}-v{version_no:03d}.mp3"
            audio_path = audio_dir / audio_filename

            atomic_write_bytes(audio_path, audio_data)

            # 获取音频时长
            duration_sec = self._get_audio_duration(audio_path)

            # 取消当前版本
            connection.execute(
                update(shot_audio).where(shot_audio.c.shot_id == shot_id).values(is_current=0)
            )

            # 插入数据库
            audio_id = new_id()
            now = utc_now()
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
                    provider_id=tts_provider_id,
                    model=provider_dict["model"],
                    status="completed",
                    is_current=1,
                    source="generated",
                    created_at=now,
                )
            )

        return self.get_shot_audio(shot_id)

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

    def list_project_bgms(self, project_id: str) -> list[dict[str, Any]]:
        """列出项目所有 BGM"""
        with self.database.engine.connect() as connection:
            records = connection.execute(
                select(project_bgm)
                .where(project_bgm.c.project_id == project_id)
                .order_by(project_bgm.c.created_at.desc())
            ).fetchall()

        return [row_dict(r) for r in records]

    def save_bgm(self, project_id: str, name: str, file_data: bytes, mood_tags: list[str]) -> dict[str, Any]:
        """保存 BGM 文件"""
        with self.database.engine.begin() as connection:
            # 检查项目是否存在
            proj = connection.execute(select(project.c.id).where(project.c.id == project_id)).first()
            if proj is None:
                raise not_found(f"项目不存在：{project_id}")

            # 保存文件
            project_path = file_service.project_path(project_id)
            bgm_dir = project_path / "BGM"
            bgm_dir.mkdir(parents=True, exist_ok=True)

            bgm_id = new_id()
            bgm_filename = f"bgm-{bgm_id}.mp3"
            bgm_path = bgm_dir / bgm_filename

            atomic_write_bytes(bgm_path, file_data)

            # 获取音频时长
            duration_sec = self._get_audio_duration(bgm_path)

            # 插入数据库
            now = utc_now()
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

            record = connection.execute(
                select(project_bgm).where(project_bgm.c.id == bgm_id)
            ).first()

        return row_dict(record)

    def delete_bgm(self, bgm_id: str) -> None:
        """删除 BGM"""
        with self.database.engine.begin() as connection:
            # 获取文件路径
            record = connection.execute(
                select(project_bgm.c.project_id, project_bgm.c.file_path).where(
                    project_bgm.c.id == bgm_id
                )
            ).first()

            if record is None:
                raise not_found(f"BGM 不存在：{bgm_id}")

            # 删除文件
            project_path = file_service.project_path(record.project_id)
            bgm_path = project_path / record.file_path
            bgm_path.unlink(missing_ok=True)

            # 删除数据库记录
            connection.execute(delete(project_bgm).where(project_bgm.c.id == bgm_id))

    @staticmethod
    def _get_audio_duration(audio_path: Path) -> float:
        """获取音频时长（使用 ffprobe）"""
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
            return float(result.stdout.strip())
        except (subprocess.CalledProcessError, ValueError, FileNotFoundError) as exc:
            logger.warning(f"无法获取音频时长：{exc}")
            return 0.0
