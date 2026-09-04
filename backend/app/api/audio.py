"""音频相关 API

包括配音生成、BGM 管理等。
"""

from typing import Any

from fastapi import APIRouter, Depends, File, Form, Response, UploadFile, status

from app.api.schemas import ShotAudioGenerateRequest
from app.dependencies import get_audio_service
from app.services.audio_service import AudioService
from app.services.constants import MAX_BGM_SIZE
from app.services.errors import bad_request

router = APIRouter(prefix="/audio", tags=["audio"])


@router.get("/edge-tts/voices")
def get_edge_tts_voices() -> list[dict[str, Any]]:
    """获取 Edge TTS 可用的声音列表"""
    from app.providers.tts.edge_tts import COMMON_VOICES
    return COMMON_VOICES


@router.get("/projects/{project_id}/config")
def get_audio_config(
    project_id: str,
    service: AudioService = Depends(get_audio_service),
) -> dict[str, Any]:
    """获取项目音频配置"""
    return service.get_project_audio_config(project_id)


@router.put("/projects/{project_id}/config")
def update_audio_config(
    project_id: str,
    updates: dict[str, Any],
    service: AudioService = Depends(get_audio_service),
) -> dict[str, Any]:
    """更新项目音频配置"""
    return service.update_project_audio_config(project_id, updates)


@router.get("/chapters/{chapter_id}/dialogues")
def extract_dialogues(
    chapter_id: str,
    service: AudioService = Depends(get_audio_service),
) -> list[dict[str, Any]]:
    """从剧本提取对话列表"""
    return service.extract_dialogues_from_screenplay(chapter_id)


@router.post("/shots/{shot_id}/audio")
async def generate_shot_audio(
    shot_id: str,
    body: ShotAudioGenerateRequest,
    service: AudioService = Depends(get_audio_service),
) -> dict[str, Any]:
    """为单个分镜生成配音"""
    return await service.generate_shot_audio(
        shot_id,
        body.dialogue_text,
        body.voice_id,
        tts_provider_id=body.tts_provider_id,
    )


@router.get("/shots/{shot_id}/audio")
def get_shot_audio(
    shot_id: str,
    service: AudioService = Depends(get_audio_service),
) -> dict[str, Any] | None:
    """获取分镜当前配音"""
    return service.get_shot_audio(shot_id)


@router.get("/shots/{shot_id}/audio/versions")
def list_shot_audio_versions(
    shot_id: str,
    service: AudioService = Depends(get_audio_service),
) -> list[dict[str, Any]]:
    """列出分镜所有配音版本"""
    return service.list_shot_audios(shot_id)


@router.get("/chapters/{chapter_id}/audios")
def list_chapter_audios(
    chapter_id: str,
    service: AudioService = Depends(get_audio_service),
) -> list[dict[str, Any]]:
    """列出章节内所有分镜的当前配音"""
    return service.list_chapter_audios(chapter_id)


@router.delete("/shot-audios/{audio_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_shot_audio(
    audio_id: str,
    service: AudioService = Depends(get_audio_service),
) -> Response:
    """删除一个配音版本"""
    service.delete_shot_audio(audio_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/projects/{project_id}/bgm")
def list_bgms(
    project_id: str,
    service: AudioService = Depends(get_audio_service),
) -> list[dict[str, Any]]:
    """列出项目所有 BGM"""
    return service.list_project_bgms(project_id)


@router.post("/projects/{project_id}/bgm")
async def upload_bgm(
    project_id: str,
    file: UploadFile = File(...),
    name: str = Form(...),
    mood_tags: str = Form(default="[]"),
    service: AudioService = Depends(get_audio_service),
) -> dict[str, Any]:
    """上传 BGM 文件"""
    from app.services.utils import json_loads

    # 有界读取：多读 1 字节即可判定超限，避免先把整个文件读进内存再检查大小。
    file_data = await file.read(MAX_BGM_SIZE + 1)
    if len(file_data) > MAX_BGM_SIZE:
        raise bad_request(f"文件大小超过限制（最大 {MAX_BGM_SIZE // (1024 * 1024)}MB）")

    # 验证文件类型
    if file.content_type and not file.content_type.startswith("audio/"):
        raise bad_request(f"不支持的文件类型：{file.content_type}")

    mood_tags_list = json_loads(mood_tags, [])

    return await service.save_bgm(project_id, name, file_data, mood_tags_list)


@router.delete("/bgm/{bgm_id}")
def delete_bgm(
    bgm_id: str,
    service: AudioService = Depends(get_audio_service),
) -> dict[str, str]:
    """删除 BGM"""
    service.delete_bgm(bgm_id)
    return {"message": "BGM 已删除"}
