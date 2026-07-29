from fastapi import APIRouter, Depends, File, Response, UploadFile, status
from fastapi.responses import FileResponse

from app.api.schemas import AssetBatchRequest, GenerateRequest, MediaBatchRequest
from app.dependencies import get_media_service
from app.services.media_service import MediaService

router = APIRouter(tags=["media"])


async def _read_upload(file: UploadFile) -> tuple[str, str, bytes]:
    data = await file.read()
    return file.filename or "upload", file.content_type or "", data


@router.get("/assets/{asset_id}/images")
def list_asset_images(
    asset_id: str,
    service: MediaService = Depends(get_media_service),
) -> list[dict]:
    return service.list_asset_images(asset_id)


@router.post("/assets/{asset_id}/images/generate", status_code=status.HTTP_202_ACCEPTED)
def generate_asset_image(
    asset_id: str,
    body: GenerateRequest,
    service: MediaService = Depends(get_media_service),
) -> dict:
    return service.queue_asset_image(asset_id, body.model_dump())


@router.post(
    "/projects/{project_id}/assets/images/batch",
    status_code=status.HTTP_202_ACCEPTED,
)
def generate_asset_images_batch(
    project_id: str,
    body: AssetBatchRequest,
    service: MediaService = Depends(get_media_service),
) -> dict:
    return service.queue_asset_image_batch(project_id, body.model_dump())


@router.post("/assets/{asset_id}/images/upload", status_code=status.HTTP_201_CREATED)
async def upload_asset_image(
    asset_id: str,
    file: UploadFile = File(...),
    service: MediaService = Depends(get_media_service),
) -> dict:
    return service.upload_asset_image(asset_id, *(await _read_upload(file)))


@router.put("/asset-images/{image_id}/current")
def set_current_asset_image(
    image_id: str,
    service: MediaService = Depends(get_media_service),
) -> dict:
    return service.set_current("asset_image", image_id)


@router.delete("/asset-images/{image_id}/current")
def unset_current_asset_image(
    image_id: str,
    service: MediaService = Depends(get_media_service),
) -> dict:
    return service.unset_current("asset_image", image_id)


@router.delete("/asset-images/{image_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_asset_image(
    image_id: str,
    service: MediaService = Depends(get_media_service),
) -> Response:
    service.delete_version("asset_image", image_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/shots/{shot_id}/keyframes")
def list_keyframes(
    shot_id: str,
    service: MediaService = Depends(get_media_service),
) -> list[dict]:
    return service.list_keyframes(shot_id)


@router.post("/shots/{shot_id}/keyframes/generate", status_code=status.HTTP_202_ACCEPTED)
def generate_keyframe(
    shot_id: str,
    body: GenerateRequest,
    service: MediaService = Depends(get_media_service),
) -> dict:
    return service.queue_keyframe(shot_id, body.model_dump())


@router.post(
    "/projects/{project_id}/keyframes/batch",
    status_code=status.HTTP_202_ACCEPTED,
)
def generate_keyframes_batch(
    project_id: str,
    body: MediaBatchRequest,
    service: MediaService = Depends(get_media_service),
) -> dict:
    return service.queue_keyframe_batch(project_id, body.model_dump())


@router.post("/shots/{shot_id}/keyframes/upload", status_code=status.HTTP_201_CREATED)
async def upload_keyframe(
    shot_id: str,
    file: UploadFile = File(...),
    service: MediaService = Depends(get_media_service),
) -> dict:
    return service.upload_keyframe(shot_id, *(await _read_upload(file)))


@router.put("/keyframes/{keyframe_id}/current")
def set_current_keyframe(
    keyframe_id: str,
    service: MediaService = Depends(get_media_service),
) -> dict:
    return service.set_current("keyframe", keyframe_id)


@router.delete("/keyframes/{keyframe_id}/current")
def unset_current_keyframe(
    keyframe_id: str,
    service: MediaService = Depends(get_media_service),
) -> dict:
    return service.unset_current("keyframe", keyframe_id)


@router.delete("/keyframes/{keyframe_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_keyframe(
    keyframe_id: str,
    service: MediaService = Depends(get_media_service),
) -> Response:
    service.delete_version("keyframe", keyframe_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/shots/{shot_id}/videos")
def list_videos(
    shot_id: str,
    service: MediaService = Depends(get_media_service),
) -> list[dict]:
    return service.list_videos(shot_id)


@router.post("/shots/{shot_id}/videos/generate", status_code=status.HTTP_202_ACCEPTED)
def generate_video(
    shot_id: str,
    body: GenerateRequest,
    service: MediaService = Depends(get_media_service),
) -> dict:
    return service.queue_video(shot_id, body.model_dump())


@router.post(
    "/projects/{project_id}/videos/batch",
    status_code=status.HTTP_202_ACCEPTED,
)
def generate_videos_batch(
    project_id: str,
    body: MediaBatchRequest,
    service: MediaService = Depends(get_media_service),
) -> dict:
    return service.queue_video_batch(project_id, body.model_dump())


@router.post("/shots/{shot_id}/videos/upload", status_code=status.HTTP_201_CREATED)
async def upload_video(
    shot_id: str,
    file: UploadFile = File(...),
    service: MediaService = Depends(get_media_service),
) -> dict:
    return service.upload_video(shot_id, *(await _read_upload(file)))


@router.put("/videos/{video_id}/current")
def set_current_video(
    video_id: str,
    service: MediaService = Depends(get_media_service),
) -> dict:
    return service.set_current("video", video_id)


@router.delete("/videos/{video_id}/current")
def unset_current_video(
    video_id: str,
    service: MediaService = Depends(get_media_service),
) -> dict:
    return service.unset_current("video", video_id)


@router.delete("/videos/{video_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_video(
    video_id: str,
    service: MediaService = Depends(get_media_service),
) -> Response:
    service.delete_version("video", video_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/videos/{video_id}/download", response_class=FileResponse)
def download_video(
    video_id: str,
    service: MediaService = Depends(get_media_service),
) -> FileResponse:
    path = service.resolve_video_download(video_id)
    return FileResponse(path, media_type="video/mp4", filename=path.name)


@router.get("/projects/{project_id}/videos/export", response_class=FileResponse)
def export_current_videos(
    project_id: str,
    service: MediaService = Depends(get_media_service),
) -> FileResponse:
    path = service.export_current_videos(project_id)
    return FileResponse(path, media_type="application/zip", filename=path.name)
