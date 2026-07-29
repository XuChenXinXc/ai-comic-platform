from fastapi import APIRouter, Depends, Response, status

from app.api.schemas import (
    GenerateRequest,
    ShotCopyRequest,
    ShotCreate,
    ShotExtractRequest,
    ShotMergeRequest,
    ShotPromptBatchRequest,
    ShotReorderRequest,
    ShotSplitRequest,
    ShotUpdate,
)
from app.dependencies import get_shot_service
from app.services.shot_service import ShotService

router = APIRouter(tags=["shots"])


@router.post("/projects/{project_id}/shots/extract", status_code=status.HTTP_202_ACCEPTED)
def extract_shots(
    project_id: str,
    body: ShotExtractRequest,
    service: ShotService = Depends(get_shot_service),
) -> dict:
    return service.queue_extract(project_id, body.model_dump())


@router.get("/projects/{project_id}/shots")
def list_shots(
    project_id: str,
    service: ShotService = Depends(get_shot_service),
) -> list[dict]:
    return service.list(project_id)


@router.post("/projects/{project_id}/shots", status_code=status.HTTP_201_CREATED)
def create_shot(
    project_id: str,
    body: ShotCreate,
    service: ShotService = Depends(get_shot_service),
) -> dict:
    return service.create(project_id, body.model_dump())


@router.put("/shots/{shot_id}")
def update_shot(
    shot_id: str,
    body: ShotUpdate,
    service: ShotService = Depends(get_shot_service),
) -> dict:
    return service.update(shot_id, body.model_dump(exclude_unset=True))


@router.delete("/shots/{shot_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_shot(
    shot_id: str,
    service: ShotService = Depends(get_shot_service),
) -> Response:
    service.delete(shot_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/projects/{project_id}/shots/reorder")
def reorder_shots(
    project_id: str,
    body: ShotReorderRequest,
    service: ShotService = Depends(get_shot_service),
) -> list[dict]:
    return service.reorder(project_id, body.ids or [])


@router.post("/shots/{shot_id}/split")
def split_shot(
    shot_id: str,
    body: ShotSplitRequest,
    service: ShotService = Depends(get_shot_service),
) -> list[dict]:
    return service.split(shot_id, [part.model_dump(exclude_none=True) for part in body.parts])


@router.post("/shots/{shot_id}/copy", status_code=status.HTTP_201_CREATED)
def copy_shot(
    shot_id: str,
    body: ShotCopyRequest,
    service: ShotService = Depends(get_shot_service),
) -> dict:
    return service.copy(shot_id, body.after_shot_id)


@router.post("/shots/{shot_id}/merge")
def merge_shots(
    shot_id: str,
    body: ShotMergeRequest,
    service: ShotService = Depends(get_shot_service),
) -> dict:
    return service.merge(shot_id, body.ids or [])


@router.post("/projects/{project_id}/shots/merge")
def merge_project_shots(
    project_id: str,
    body: ShotMergeRequest,
    service: ShotService = Depends(get_shot_service),
) -> dict:
    return service.merge_many(project_id, body.ids or [])


@router.post("/shots/{shot_id}/image-prompt/generate", status_code=status.HTTP_202_ACCEPTED)
def generate_shot_image_prompt(
    shot_id: str,
    body: GenerateRequest,
    service: ShotService = Depends(get_shot_service),
) -> dict:
    return service.queue_prompt(shot_id, "image", body.model_dump())


@router.post("/shots/{shot_id}/video-prompt/generate", status_code=status.HTTP_202_ACCEPTED)
def generate_shot_video_prompt(
    shot_id: str,
    body: GenerateRequest,
    service: ShotService = Depends(get_shot_service),
) -> dict:
    return service.queue_prompt(shot_id, "video", body.model_dump())


@router.post(
    "/projects/{project_id}/shots/prompts/batch",
    status_code=status.HTTP_202_ACCEPTED,
)
def generate_shot_prompts_batch(
    project_id: str,
    body: ShotPromptBatchRequest,
    service: ShotService = Depends(get_shot_service),
) -> dict:
    return service.queue_prompt_batch(project_id, body.model_dump())
