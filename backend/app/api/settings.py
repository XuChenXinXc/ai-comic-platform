from fastapi import APIRouter, Depends
from pydantic import BaseModel

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
    get_imgbb_api_key_override,
    get_provider_allow_private_hosts_setting,
    set_camera_policy,
    set_default_style_prompt,
    set_imgbb_api_key,
    set_provider_allow_private_hosts,
)

router = APIRouter(prefix="/settings", tags=["settings"])


# 这两个模型只服务本地 Provider 开关；api/schemas.py 不在本次改动范围内，
# 因此就近定义在本路由模块，避免为单个开关扩大共享 schema 的改动面。
class ProviderAllowPrivateHostsSetting(BaseModel):
    enabled: bool
    source: str


class ProviderAllowPrivateHostsUpdate(BaseModel):
    enabled: bool


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

    if get_imgbb_api_key_override(service.database):
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
    set_imgbb_api_key(service.database, body.api_key.strip())
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


@router.get(
    "/provider-allow-private-hosts",
    response_model=ProviderAllowPrivateHostsSetting,
)
def read_provider_allow_private_hosts(
    service: SettingService = Depends(get_setting_service),
) -> dict:
    """读取「允许连接本地/内网 Provider」开关及其来源。"""
    return get_provider_allow_private_hosts_setting(service.database, service.settings)


@router.post(
    "/provider-allow-private-hosts",
    response_model=ProviderAllowPrivateHostsSetting,
)
def save_provider_allow_private_hosts(
    body: ProviderAllowPrivateHostsUpdate,
    service: SettingService = Depends(get_setting_service),
) -> dict:
    """保存开关。

    生效无需重启：ProviderService 在每次解析/保存/测试前会把数据库值同步到
    Provider 层；页面保存后紧接着的连接测试或生成请求即读到新值。
    """
    set_provider_allow_private_hosts(service.database, body.enabled)
    return get_provider_allow_private_hosts_setting(service.database, service.settings)

