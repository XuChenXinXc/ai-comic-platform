from fastapi import APIRouter, Depends, status

from app.api.schemas import GenerateRequest, ScreenplaySceneGenerateRequest, TextContentUpdate
from app.dependencies import get_writing_service
from app.services.writing_service import WritingService

router = APIRouter(prefix="/chapters/{chapter_id}", tags=["writing"])


@router.post("/analysis/generate", status_code=status.HTTP_202_ACCEPTED)
def generate_analysis(
    chapter_id: str,
    body: GenerateRequest,
    service: WritingService = Depends(get_writing_service),
) -> dict:
    return service.queue_analysis(chapter_id, body.model_dump())


@router.get("/analysis")
def get_analysis(
    chapter_id: str,
    service: WritingService = Depends(get_writing_service),
) -> dict:
    return service.get_analysis(chapter_id)


@router.put("/analysis")
def update_analysis(
    chapter_id: str,
    body: TextContentUpdate,
    service: WritingService = Depends(get_writing_service),
) -> dict:
    return service.update_analysis(chapter_id, body.model_dump())


@router.post("/screenplay/generate", status_code=status.HTTP_202_ACCEPTED)
def generate_screenplay(
    chapter_id: str,
    body: GenerateRequest,
    service: WritingService = Depends(get_writing_service),
) -> dict:
    return service.queue_screenplay(chapter_id, body.model_dump())


@router.post("/screenplay/scenes/generate", status_code=status.HTTP_202_ACCEPTED)
def generate_screenplay_scene(
    chapter_id: str,
    body: ScreenplaySceneGenerateRequest,
    service: WritingService = Depends(get_writing_service),
) -> dict:
    return service.queue_screenplay_scene(chapter_id, body.model_dump())


@router.post(
    "/screenplay/scenes/{scene_no}/generate",
    status_code=status.HTTP_202_ACCEPTED,
)
def generate_screenplay_scene_by_path(
    chapter_id: str,
    scene_no: int,
    body: GenerateRequest,
    service: WritingService = Depends(get_writing_service),
) -> dict:
    payload = body.model_dump()
    payload["scene_no"] = scene_no
    payload["instructions"] = ""
    return service.queue_screenplay_scene(chapter_id, payload)


@router.get("/screenplay")
def get_screenplay(
    chapter_id: str,
    service: WritingService = Depends(get_writing_service),
) -> dict:
    return service.get_screenplay(chapter_id)


@router.put("/screenplay")
def update_screenplay(
    chapter_id: str,
    body: TextContentUpdate,
    service: WritingService = Depends(get_writing_service),
) -> dict:
    return service.update_screenplay(chapter_id, body.model_dump())
