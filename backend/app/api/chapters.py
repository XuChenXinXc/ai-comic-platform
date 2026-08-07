from fastapi import APIRouter, Depends, Response, status

from app.api.schemas import ChapterCreate, ChapterReorderRequest, ChapterUpdate
from app.dependencies import get_chapter_service
from app.services.chapter_service import ChapterService

router = APIRouter(tags=["chapters"])


@router.get("/projects/{project_id}/chapters")
def list_chapters(
    project_id: str,
    service: ChapterService = Depends(get_chapter_service),
) -> list[dict]:
    return service.list(project_id)


@router.post("/projects/{project_id}/chapters", status_code=status.HTTP_201_CREATED)
def create_chapter(
    project_id: str,
    body: ChapterCreate,
    service: ChapterService = Depends(get_chapter_service),
) -> dict:
    return service.create(project_id, body.model_dump())


@router.get("/chapters/{chapter_id}")
def get_chapter(
    chapter_id: str,
    service: ChapterService = Depends(get_chapter_service),
) -> dict:
    return service.get(chapter_id)


@router.put("/chapters/{chapter_id}")
def update_chapter(
    chapter_id: str,
    body: ChapterUpdate,
    service: ChapterService = Depends(get_chapter_service),
) -> dict:
    return service.update(chapter_id, body.model_dump(exclude_unset=True))


@router.get("/chapters/{chapter_id}/delete-preview")
def preview_chapter_delete(
    chapter_id: str,
    service: ChapterService = Depends(get_chapter_service),
) -> dict:
    return service.delete_preview(chapter_id)


@router.delete("/chapters/{chapter_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_chapter(
    chapter_id: str,
    service: ChapterService = Depends(get_chapter_service),
) -> Response:
    service.delete(chapter_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/projects/{project_id}/chapters/reorder")
def reorder_chapters(
    project_id: str,
    body: ChapterReorderRequest,
    service: ChapterService = Depends(get_chapter_service),
) -> list[dict]:
    return service.reorder(project_id, body.ids)
