from fastapi import APIRouter, Depends, Query, Response, status

from app.api.schemas import (
    AssetBatchRequest,
    AssetCreate,
    AssetExtractRequest,
    AssetMergeRequest,
    AssetType,
    AssetUpdate,
    GenerateRequest,
    SceneViewUpdate,
)
from app.dependencies import get_asset_service, get_scene_view_service
from app.services.asset_service import AssetService
from app.services.scene_view_service import SceneViewService

router = APIRouter(tags=["assets"])


@router.post("/chapters/{chapter_id}/assets/extract", status_code=status.HTTP_202_ACCEPTED)
def extract_assets(
    chapter_id: str,
    body: AssetExtractRequest,
    service: AssetService = Depends(get_asset_service),
) -> dict:
    return service.queue_extract(chapter_id, body.model_dump())


@router.get("/projects/{project_id}/assets")
def list_assets(
    project_id: str,
    asset_type: AssetType | None = Query(default=None),
    service: AssetService = Depends(get_asset_service),
) -> list[dict]:
    return service.list(project_id, asset_type)


@router.post("/projects/{project_id}/assets", status_code=status.HTTP_201_CREATED)
def create_asset(
    project_id: str,
    body: AssetCreate,
    service: AssetService = Depends(get_asset_service),
) -> dict:
    return service.create(project_id, body.model_dump())


@router.put("/assets/{asset_id}")
def update_asset(
    asset_id: str,
    body: AssetUpdate,
    service: AssetService = Depends(get_asset_service),
) -> dict:
    return service.update(asset_id, body.model_dump(exclude_unset=True))


@router.delete("/assets/{asset_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_asset(
    asset_id: str,
    service: AssetService = Depends(get_asset_service),
) -> Response:
    service.delete(asset_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/assets/{asset_id}/merge")
def merge_assets(
    asset_id: str,
    body: AssetMergeRequest,
    service: AssetService = Depends(get_asset_service),
) -> dict:
    return service.merge(asset_id, body.ids)


@router.post("/projects/{project_id}/assets/merge")
def merge_project_assets(
    project_id: str,
    body: AssetMergeRequest,
    service: AssetService = Depends(get_asset_service),
) -> dict:
    return service.merge_many(project_id, body.ids)


@router.post("/assets/{asset_id}/prompt/generate", status_code=status.HTTP_202_ACCEPTED)
def generate_asset_prompt(
    asset_id: str,
    body: GenerateRequest,
    service: AssetService = Depends(get_asset_service),
) -> dict:
    return service.queue_prompt(asset_id, body.model_dump())


@router.post(
    "/projects/{project_id}/assets/prompts/batch",
    status_code=status.HTTP_202_ACCEPTED,
)
def generate_asset_prompts_batch(
    project_id: str,
    body: AssetBatchRequest,
    service: AssetService = Depends(get_asset_service),
) -> dict:
    return service.queue_prompt_batch(project_id, body.model_dump())


# ---------- Scene View（场景资产包） ----------


@router.get("/assets/{asset_id}/scene-views")
def list_scene_views(
    asset_id: str,
    service: SceneViewService = Depends(get_scene_view_service),
) -> list[dict]:
    return service.list(asset_id)


@router.post(
    "/assets/{asset_id}/scene-views/ensure",
    status_code=status.HTTP_201_CREATED,
)
def ensure_scene_views(
    asset_id: str,
    service: SceneViewService = Depends(get_scene_view_service),
) -> list[dict]:
    """幂等补齐默认视图（MASTER + front/reverse/left45/right45），已存在则直接返回。"""
    return service.ensure_defaults(asset_id)


@router.put("/scene-views/{view_id}")
def update_scene_view(
    view_id: str,
    body: SceneViewUpdate,
    service: SceneViewService = Depends(get_scene_view_service),
) -> dict:
    return service.update(view_id, body.model_dump())


@router.delete("/scene-views/{view_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_scene_view(
    view_id: str,
    service: SceneViewService = Depends(get_scene_view_service),
) -> Response:
    service.delete(view_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/assets/{asset_id}/scene-views/{view_key}/generate",
    status_code=status.HTTP_202_ACCEPTED,
)
def generate_scene_view_image(
    asset_id: str,
    view_key: str,
    body: GenerateRequest,
    service: SceneViewService = Depends(get_scene_view_service),
) -> dict:
    """生成某个 Scene View 的图片：MASTER 无参考图；其余视图以 MASTER 图为参考。"""
    return service.generate(asset_id, view_key, body.model_dump())
