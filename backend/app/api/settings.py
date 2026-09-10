from fastapi import APIRouter, Depends

from app.api.schemas import (
    CameraPolicySetting,
    CameraPolicyUpdate,
    DefaultStylePromptSetting,
    DefaultStylePromptUpdate,
    ImgbbKeyStatus,
    ProjectStorageSelectionResult,
    ProjectStorageSetting,
    SetImgbbKeyRequest,
)
from app.dependencies import get_setting_service
from app.services.camera_library import SUPPORTED_POLICIES
from app.services.setting_service import (
    SettingService,
    get_camera_policy,
    get_default_style_prompt,
    get_imagbb_api_key_override,
    set_camera_policy,
    set_default_style_prompt,
    set_imagbb_api_key,
)

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


@router.get("/imgbb-key", response_model=ImgbbKeyStatus)
def get_imgbb_key_status(
    service: SettingService = Depends(get_setting_service),
) -> dict:
    """查询 ImgBB Key 状态：从不返回明文，只返回是否配置与来源。"""
    from app.config import settings

    if get_imagbb_api_key_override(service.database):
        return {"configured": True, "source": "database"}
    if settings.imgbb_api_key:
        return {"configured": True, "source": "environment"}
    return {"configured": False, "source": "default"}


@router.post("/imgbb-key", response_model=ImgbbKeyStatus)
def set_imgbb_key(
    body: SetImgbbKeyRequest,
    service: SettingService = Depends(get_setting_service),
) -> dict:
    """保存 ImgBB Key（DPAPI/Keychain 加密）。空字符串表示清除。"""
    set_imagbb_api_key(service.database, body.api_key.strip())
    return get_imgbb_key_status(service=service)  # type: ignore[arg-type]


@router.get("/camera-policy", response_model=CameraPolicySetting)
def get_camera_policy_setting(
    service: SettingService = Depends(get_setting_service),
) -> dict:
    """读取平台运镜策略（克制 / 标准 / 丰富）。"""
    return {"policy": get_camera_policy(service.database), "options": list(SUPPORTED_POLICIES)}


@router.get("/default-style-prompt", response_model=DefaultStylePromptSetting)
def get_default_style_prompt_setting(
    service: SettingService = Depends(get_setting_service),
) -> dict:
    """读取平台默认画风（新建项目未填写统一画风时回填该值）。"""
    return {"style_prompt": get_default_style_prompt(service.database)}


@router.post("/default-style-prompt", response_model=DefaultStylePromptSetting)
def set_default_style_prompt_setting(
    body: DefaultStylePromptUpdate,
    service: SettingService = Depends(get_setting_service),
) -> dict:
    """保存平台默认画风；空字符串表示清除。"""
    style_prompt = set_default_style_prompt(service.database, body.style_prompt)
    return {"style_prompt": style_prompt}


@router.post("/camera-policy", response_model=CameraPolicySetting)
def set_camera_policy_setting(
    body: CameraPolicyUpdate,
    service: SettingService = Depends(get_setting_service),
) -> dict:
    """保存平台运镜策略。"""
    policy = set_camera_policy(service.database, body.policy)
    return {"policy": policy, "options": list(SUPPORTED_POLICIES)}
