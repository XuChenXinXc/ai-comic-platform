from fastapi import APIRouter, Depends

from app.api.schemas import ProjectStorageSelectionResult, ProjectStorageSetting
from app.dependencies import get_setting_service
from app.services.setting_service import SettingService

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("/project-storage", response_model=ProjectStorageSetting)
def get_project_storage(
    service: SettingService = Depends(get_setting_service),
) -> dict:
    return service.get_project_storage()


@router.post(
    "/project-storage/select",
    response_model=ProjectStorageSelectionResult,
)
def select_project_storage(
    service: SettingService = Depends(get_setting_service),
) -> dict:
    return service.select_project_storage()
