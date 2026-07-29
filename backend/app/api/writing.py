from fastapi import APIRouter, Depends, status

from app.api.schemas import GenerateRequest, ScreenplaySceneGenerateRequest, TextContentUpdate
from app.dependencies import get_writing_service
from app.services.writing_service import WritingService

router = APIRouter(prefix="/projects/{project_id}", tags=["writing"])


@router.post("/analysis/generate", status_code=status.HTTP_202_ACCEPTED)
def generate_analysis(
    project_id: str,
    body: GenerateRequest,
    service: WritingService = Depends(get_writing_service),
) -> dict:
    return service.queue_analysis(project_id, body.model_dump())


@router.get("/analysis")
def get_analysis(
    project_id: str,
    service: WritingService = Depends(get_writing_service),
) -> dict:
    return service.get_analysis(project_id)


@router.put("/analysis")
def update_analysis(
    project_id: str,
    body: TextContentUpdate,
    service: WritingService = Depends(get_writing_service),
) -> dict:
    return service.update_analysis(project_id, body.model_dump())


@router.post("/screenplay/generate", status_code=status.HTTP_202_ACCEPTED)
def generate_screenplay(
    project_id: str,
    body: GenerateRequest,
    service: WritingService = Depends(get_writing_service),
) -> dict:
    return service.queue_screenplay(project_id, body.model_dump())


@router.post("/screenplay/scenes/generate", status_code=status.HTTP_202_ACCEPTED)
def generate_screenplay_scene(
    project_id: str,
    body: ScreenplaySceneGenerateRequest,
    service: WritingService = Depends(get_writing_service),
) -> dict:
    return service.queue_screenplay_scene(project_id, body.model_dump())


@router.post(
    "/screenplay/scenes/{scene_no}/generate",
    status_code=status.HTTP_202_ACCEPTED,
)
def generate_screenplay_scene_by_path(
    project_id: str,
    scene_no: int,
    body: GenerateRequest,
    service: WritingService = Depends(get_writing_service),
) -> dict:
    payload = body.model_dump()
    payload["scene_no"] = scene_no
    payload["instructions"] = ""
    return service.queue_screenplay_scene(project_id, payload)


@router.get("/screenplay")
def get_screenplay(
    project_id: str,
    service: WritingService = Depends(get_writing_service),
) -> dict:
    return service.get_screenplay(project_id)


@router.put("/screenplay")
def update_screenplay(
    project_id: str,
    body: TextContentUpdate,
    service: WritingService = Depends(get_writing_service),
) -> dict:
    return service.update_screenplay(project_id, body.model_dump())
