from fastapi import APIRouter, Depends, Response, status

from app.api.schemas import (
    ContinuityCheckRequest,
    GenerateRequest,
    QualityFixRequest,
    RefinePromptsRequest,
    ShotCopyRequest,
    ShotCreate,
    ShotExtractRequest,
    ShotMergeRequest,
    ShotPromptBatchRequest,
    ShotReorderRequest,
    ShotSplitRequest,
    ShotUpdate,
    VideoPromptOptimizeAllRequest,
    VideoPromptOptimizeRequest,
)
from app.dependencies import get_shot_service
from app.services.shot_service import ShotService

router = APIRouter(tags=["shots"])


@router.post("/chapters/{chapter_id}/shots/extract", status_code=status.HTTP_202_ACCEPTED)
def extract_shots(
    chapter_id: str,
    body: ShotExtractRequest,
    service: ShotService = Depends(get_shot_service),
) -> dict:
    return service.queue_extract(chapter_id, body.model_dump())


@router.get("/chapters/{chapter_id}/shots")
def list_shots(
    chapter_id: str,
    service: ShotService = Depends(get_shot_service),
) -> list[dict]:
    return service.list(chapter_id)


@router.post("/chapters/{chapter_id}/shots", status_code=status.HTTP_201_CREATED)
def create_shot(
    chapter_id: str,
    body: ShotCreate,
    service: ShotService = Depends(get_shot_service),
) -> dict:
    return service.create(chapter_id, body.model_dump())


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


@router.post("/chapters/{chapter_id}/shots/reorder")
def reorder_shots(
    chapter_id: str,
    body: ShotReorderRequest,
    service: ShotService = Depends(get_shot_service),
) -> list[dict]:
    return service.reorder(chapter_id, body.ids or [])


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


@router.post("/chapters/{chapter_id}/shots/merge")
def merge_chapter_shots(
    chapter_id: str,
    body: ShotMergeRequest,
    service: ShotService = Depends(get_shot_service),
) -> dict:
    return service.merge_many(chapter_id, body.ids or [])


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


@router.post("/shots/{shot_id}/video-prompt/optimize")
async def optimize_shot_video_prompt(
    shot_id: str,
    body: VideoPromptOptimizeRequest,
    service: ShotService = Depends(get_shot_service),
) -> dict:
    return await service.optimize_video_prompt(
        shot_id,
        prompt=body.prompt,
        provider_id=body.provider_id,
        params=body.params,
    )


@router.post(
    "/projects/{project_id}/video-prompts/optimize-all",
    status_code=status.HTTP_202_ACCEPTED,
)
def optimize_project_video_prompts(
    project_id: str,
    body: VideoPromptOptimizeAllRequest,
    service: ShotService = Depends(get_shot_service),
) -> dict:
    return service.queue_video_prompt_optimize_batch(project_id, body.model_dump())


@router.post(
    "/chapters/{chapter_id}/shots/prompts/batch",
    status_code=status.HTTP_202_ACCEPTED,
)
def generate_shot_prompts_batch(
    chapter_id: str,
    body: ShotPromptBatchRequest,
    service: ShotService = Depends(get_shot_service),
) -> dict:
    return service.queue_prompt_batch(chapter_id, body.model_dump())


@router.post(
    "/projects/{project_id}/shots/continuity/check",
    status_code=status.HTTP_202_ACCEPTED,
)
def check_shot_continuity(
    project_id: str,
    body: ContinuityCheckRequest,
    service: ShotService = Depends(get_shot_service),
) -> dict:
    return service.queue_continuity_check(project_id, body.model_dump())


@router.post(
    "/projects/{project_id}/shots/prompts/refine",
    status_code=status.HTTP_202_ACCEPTED,
)
def refine_shot_prompts(
    project_id: str,
    body: RefinePromptsRequest,
    service: ShotService = Depends(get_shot_service),
) -> dict:
    return service.queue_refine_prompts(project_id, body.model_dump())


@router.get("/projects/{project_id}/shots/quality-report")
def shot_quality_report(
    project_id: str,
    service: ShotService = Depends(get_shot_service),
) -> dict:
    return service.quality_report(project_id)


@router.post(
    "/projects/{project_id}/shots/quality-fix",
    status_code=status.HTTP_202_ACCEPTED,
)
def fix_shot_quality(
    project_id: str,
    body: QualityFixRequest,
    service: ShotService = Depends(get_shot_service),
) -> dict:
    return service.queue_quality_fix(project_id, body.model_dump())
