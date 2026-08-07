"""字幕和合成相关 API"""

from typing import Any

from fastapi import APIRouter, Depends, Response

from app.api.schemas import ChapterComposeRequest, SubtitleSaveRequest
from app.dependencies import get_composition_service, get_subtitle_service
from app.services.composition_service import CompositionService
from app.services.subtitle_service import SubtitleService

router = APIRouter(prefix="/composition", tags=["composition"])


@router.post("/shots/{shot_id}/subtitles/generate")
def generate_subtitle(
    shot_id: str,
    service: SubtitleService = Depends(get_subtitle_service),
) -> list[dict[str, Any]]:
    """根据配音自动生成字幕时间轴"""
    return service.generate_subtitle_from_audio(shot_id)


@router.post("/shots/{shot_id}/subtitles")
def save_subtitles(
    shot_id: str,
    body: SubtitleSaveRequest,
    service: SubtitleService = Depends(get_subtitle_service),
) -> list[dict[str, Any]]:
    """保存分镜字幕"""
    return service.save_subtitles(shot_id, body.subtitles)


@router.get("/shots/{shot_id}/subtitles")
def get_subtitles(
    shot_id: str,
    service: SubtitleService = Depends(get_subtitle_service),
) -> list[dict[str, Any]]:
    """获取分镜字幕"""
    return service.get_shot_subtitles(shot_id)


@router.get("/subtitle-styles")
def get_subtitle_styles(
    service: SubtitleService = Depends(get_subtitle_service),
) -> dict[str, dict[str, str]]:
    """获取预设字幕样式"""
    return service.get_subtitle_styles()


@router.get("/chapters/{chapter_id}/subtitles/srt")
def export_chapter_srt(
    chapter_id: str,
    service: SubtitleService = Depends(get_subtitle_service),
) -> Response:
    """导出章节的 SRT 格式字幕文件"""
    srt_content = service.export_chapter_srt(chapter_id)
    return Response(
        content=srt_content,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=chapter-{chapter_id}.srt"},
    )


@router.post("/chapters/{chapter_id}/compose")
def compose_chapter(
    chapter_id: str,
    body: ChapterComposeRequest,
    service: CompositionService = Depends(get_composition_service),
) -> dict[str, Any]:
    """创建章节合成任务"""
    return service.compose_chapter(chapter_id, body.model_dump())


@router.get("/chapters/{chapter_id}/composition")
def get_chapter_composition(
    chapter_id: str,
    service: CompositionService = Depends(get_composition_service),
) -> dict[str, Any] | None:
    """获取章节合成记录"""
    return service.get_chapter_composition(chapter_id)
