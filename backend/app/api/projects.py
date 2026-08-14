from typing import Literal

from fastapi import APIRouter, Depends, Query, Response, status

from app.api.schemas import ProjectCreate, ProjectUpdate
from app.dependencies import get_project_service
from app.services.project_service import ProjectService

router = APIRouter(prefix="/projects", tags=["projects"])


@router.post("", status_code=status.HTTP_201_CREATED)
def create_project(
    body: ProjectCreate,
    service: ProjectService = Depends(get_project_service),
) -> dict:
    return service.create(body.model_dump())


@router.get("")
def list_projects(
    kind: Literal["regular", "free"] | None = Query(default=None),
    service: ProjectService = Depends(get_project_service),
) -> list[dict]:
    return service.list(kind=kind)


@router.get("/{project_id}")
def get_project(
    project_id: str,
    service: ProjectService = Depends(get_project_service),
) -> dict:
    return service.get(project_id)


@router.put("/{project_id}")
def update_project(
    project_id: str,
    body: ProjectUpdate,
    service: ProjectService = Depends(get_project_service),
) -> dict:
    return service.update(project_id, body.model_dump(exclude_unset=True))


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(
    project_id: str,
    service: ProjectService = Depends(get_project_service),
) -> Response:
    service.delete(project_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
