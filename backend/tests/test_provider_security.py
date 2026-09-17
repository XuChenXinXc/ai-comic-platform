from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import insert, select, update

from app.api.schemas import GenerateRequest, ProviderCreate, ProviderUpdate
from app.config import settings
from app.db import Database
from app.jobs.repository import JobRepository
from app.models.tables import api_provider, job
from app.providers import base as provider_base_module
from app.providers.base import (
    ProviderConfigurationError,
    ProviderError,
    request_with_redirect_guard,
    set_provider_allow_private_hosts_override,
)
from app.providers.registry import provider_registry
from app.schemas import ProviderConfig
from app.services import provider_service as provider_service_module
from app.services.errors import ServiceError
from app.services.file_service import FileService
from app.services.project_service import ProjectService
from app.services.provider_service import ProviderService
from app.services.setting_service import set_provider_allow_private_hosts
from app.services.utils import json_dumps, new_id, utc_now


@pytest.fixture
def provider_database(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{(tmp_path / 'provider-security.db').as_posix()}")
    database.initialize(seed_mock_providers=True)
    yield database
    database.dispose()


@pytest.fixture(autouse=True)
def _reset_allow_private_hosts_override() -> Iterator[None]:
    """Provider 层开关是进程内缓存，逐测试复位避免跨用例串味。"""
    set_provider_allow_private_hosts_override(None)
    yield
    set_provider_allow_private_hosts_override(None)


def _create_provider(service: ProviderService, **overrides: Any) -> dict[str, Any]:
    values = {
        "kind": "text",
        "name": "安全测试 Provider",
        "adapter": "mock",
        "base_url": "",
        "api_key_env": "",
        "model": "mock-text",
        "params": {},
        "timeout_sec": 120,
        "max_retries": 0,
        "enabled": True,
        "is_default": False,
    }
    values.update(overrides)
    return service.create(values)


@pytest.mark.parametrize(
    ("schema", "payload"),
    [
        (GenerateRequest, {"params": {"headers": {"Authorization": "Bearer secret"}}}),
        (
            ProviderCreate,
            {
                "kind": "text",
                "name": "含令牌 Provider",
                "adapter": "mock",
                "params": {"access_token": "secret"},
            },
        ),
        (
            GenerateRequest,
            {"params": {"signature": "secret"}},
        ),
        (ProviderUpdate, {"params": {"nested": {"client_secret": "secret"}}}),
    ],
)
def test_api_schema_with_sensitive_params_rejects_before_persistence(
    schema: type[Any],
    payload: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError, match="敏感"):
        schema.model_validate(payload)


@pytest.mark.parametrize(
    "params",
    [
        {"headers": {"Authorization": "Bearer plaintext"}},
        {"access_token": "plaintext"},
        {"nested": {"client_secret": "plaintext"}},
        {"apikey": "plaintext"},
        {"cookie": "session=plaintext"},
        {"password": "plaintext"},
        {"credentials": "plaintext"},
        {"signature": "plaintext"},
    ],
)
def test_provider_create_with_sensitive_params_returns_400_and_does_not_persist(
    provider_database: Database,
    params: dict[str, Any],
) -> None:
    service = ProviderService(provider_database)

    with pytest.raises(ServiceError) as exc_info:
        _create_provider(service, name="含敏感参数", params=params)

    assert exc_info.value.status_code == 400
    with provider_database.engine.connect() as connection:
        names = connection.scalars(select(api_provider.c.name)).all()
    assert "含敏感参数" not in names


def test_provider_create_with_wrong_kind_api_key_env_returns_400(
    provider_database: Database,
) -> None:
    service = ProviderService(provider_database)

    with pytest.raises(ServiceError) as exc_info:
        _create_provider(service, api_key_env="IMAGE_VENDOR_API_KEY")

    assert exc_info.value.status_code == 400
    assert "TEXT" in exc_info.value.detail


@pytest.mark.parametrize("api_key_env", ["TEXT_API_KEY", "TEXT_OPENAI_API_KEY"])
def test_provider_create_with_matching_kind_api_key_env_succeeds(
    provider_database: Database,
    api_key_env: str,
) -> None:
    service = ProviderService(provider_database)

    provider = _create_provider(
        service,
        name=f"合法密钥变量 {api_key_env}",
        api_key_env=api_key_env,
    )

    assert provider["api_key_env"] == api_key_env


def test_runtime_config_falls_back_to_api_key_env(
    provider_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """仅配 api_key_env 的 Provider 运行时从环境变量取 Key。

    回归：env 回退此前只存在于连接测试 _probe，实际运行（TTS 等）
    拿到空 Key，导致「连接测试通过但生成 401/400」。
    """
    monkeypatch.setenv("TTS_API_KEY", "env-secret-value")
    runtime = ProviderService._runtime_config(
        {
            "kind": "tts",
            "adapter": "http_tts",
            "base_url": "https://example.invalid/tts",
            "api_key_env": "TTS_API_KEY",
            "api_key": "",
            "params": {},
        }
    )
    assert runtime["api_key"] == "env-secret-value"


def test_provider_create_with_direct_api_key_persists_only_configuration_status(
    provider_database: Database,
) -> None:
    service = ProviderService(provider_database)

    provider = _create_provider(
        service,
        name="直接密钥 Provider",
        api_key_env="",
        api_key="direct-provider-secret",
    )

    assert provider["api_key_configured"] is True
    assert provider["api_key_source"] == "direct"
    assert "api_key" not in provider
    assert "direct-provider-secret" not in str(provider)
    with provider_database.engine.connect() as connection:
        ciphertext = connection.execute(
            select(api_provider.c.api_key_ciphertext).where(api_provider.c.id == provider["id"])
        ).scalar_one()
    assert ciphertext
    assert ciphertext != "direct-provider-secret"


def test_provider_update_without_api_key_field_preserves_direct_api_key(
    provider_database: Database,
) -> None:
    """未传 api_key 字段 = 保持不变（编辑其它字段不应误删密钥）。"""
    service = ProviderService(provider_database)
    provider = _create_provider(
        service,
        name="保留密钥 Provider",
        api_key_env="",
        api_key="direct-provider-secret",
    )

    updated = service.update(provider["id"], {"name": "更新名称"})

    assert updated["api_key_configured"] is True
    assert updated["api_key_source"] == "direct"


def test_provider_update_with_blank_api_key_clears_direct_api_key(
    provider_database: Database,
) -> None:
    """显式传空串 = 清空已保存的 API Key，密文列与运行时都不再持有它。"""
    service = ProviderService(provider_database)
    provider = _create_provider(
        service,
        name="清空密钥 Provider",
        api_key_env="",
        api_key="direct-provider-secret",
    )

    updated = service.update(provider["id"], {"api_key": ""})

    assert updated["api_key_configured"] is False
    assert updated["api_key_source"] == "none"
    with provider_database.engine.connect() as connection:
        ciphertext = connection.execute(
            select(api_provider.c.api_key_ciphertext).where(
                api_provider.c.id == provider["id"]
            )
        ).scalar_one()
    assert ciphertext is None


def test_provider_resolve_uses_direct_api_key_without_returning_it(
    provider_database: Database,
) -> None:
    service = ProviderService(provider_database)
    provider = _create_provider(
        service,
        name="运行时直接密钥 Provider",
        api_key_env="",
        api_key="direct-provider-secret",
    )

    runtime_provider, config = service.resolve(kind="text", provider_id=provider["id"])

    assert runtime_provider.config.api_key == "direct-provider-secret"
    assert "api_key" not in config
    assert "_api_key_ciphertext" not in config
    assert "direct-provider-secret" not in str(config)


def test_provider_resolve_without_id_uses_default_provider(
    provider_database: Database,
) -> None:
    service = ProviderService(provider_database)

    _runtime_provider, config = service.resolve(kind="text")

    assert config["id"] == "mock-text"


@pytest.mark.parametrize(
    "base_url",
    [
        "https://alice:plaintext@example.com/v1",
        "https://example.com/v1?api_key=plaintext",
        "https://example.com/v1?apikey=plaintext",
        "https://example.com/v1?access_token=plaintext",
        "https://example.com/v1?clientsecret=plaintext",
        "https://example.com/v1?signature=plaintext",
    ],
)
def test_provider_create_with_sensitive_base_url_returns_400_and_does_not_persist(
    provider_database: Database,
    base_url: str,
) -> None:
    service = ProviderService(provider_database)

    with pytest.raises(ServiceError) as exc_info:
        _create_provider(service, name="URL 含敏感凭据", base_url=base_url)

    assert exc_info.value.status_code == 400
    with provider_database.engine.connect() as connection:
        names = connection.scalars(select(api_provider.c.name)).all()
    assert "URL 含敏感凭据" not in names


def test_provider_update_with_sensitive_base_url_returns_400_and_preserves_original(
    provider_database: Database,
) -> None:
    service = ProviderService(provider_database)
    provider = _create_provider(service, base_url="https://example.com/v1")

    with pytest.raises(ServiceError) as exc_info:
        service.update(
            provider["id"],
            {"base_url": "https://alice:plaintext@example.com/v1"},
        )

    assert exc_info.value.status_code == 400
    assert service.get(provider["id"])["base_url"] == "https://example.com/v1"


@pytest.mark.parametrize("forbidden_key", ["task_store_dir", "ffmpeg_path"])
def test_mock_provider_api_config_with_runtime_path_override_returns_400(
    provider_database: Database,
    forbidden_key: str,
) -> None:
    service = ProviderService(provider_database)

    with pytest.raises(ServiceError) as exc_info:
        _create_provider(
            service,
            kind="video",
            adapter="mock",
            model="mock-video",
            params={forbidden_key: "C:/outside"},
        )

    assert exc_info.value.status_code == 400
    assert forbidden_key in exc_info.value.detail


def test_provider_list_with_legacy_sensitive_params_redacts_plaintext(
    provider_database: Database,
) -> None:
    now = utc_now()
    provider_id = new_id("provider")
    with provider_database.engine.begin() as connection:
        connection.execute(
            insert(api_provider).values(
                id=provider_id,
                kind="text",
                name="遗留 Provider",
                adapter="mock",
                params_json=json_dumps(
                    {
                        "headers": {"Authorization": "Bearer legacy-secret"},
                        "nested": {
                            "access_token": "legacy-token",
                            "signature": "legacy-signature",
                        },
                    }
                ),
                created_at=now,
                updated_at=now,
            )
        )

    record = ProviderService(provider_database).get(provider_id)

    serialized = json_dumps(record["params"])
    assert "legacy-secret" not in serialized
    assert "legacy-token" not in serialized
    assert "legacy-signature" not in serialized


def test_provider_get_with_legacy_sensitive_base_url_redacts_and_resolve_rejects(
    provider_database: Database,
) -> None:
    service = ProviderService(provider_database)
    with provider_database.engine.begin() as connection:
        connection.execute(
            update(api_provider)
            .where(api_provider.c.id == "mock-text")
            .values(base_url="https://example.com/v1?apikey=legacy-secret")
        )

    record = service.get("mock-text")

    assert "legacy-secret" not in record["base_url"]
    assert "apikey" in record["base_url"]
    with pytest.raises(ServiceError) as exc_info:
        service.resolve(kind="text", provider_id="mock-text")
    assert exc_info.value.status_code == 400


@pytest.mark.parametrize(
    "status",
    ["queued", "running", "waiting_provider", "downloading"],
)
def test_provider_update_with_active_job_returns_409(
    provider_database: Database,
    tmp_path: Path,
    status: str,
) -> None:
    service = ProviderService(provider_database)
    provider = _create_provider(service)
    project = ProjectService(provider_database, FileService(tmp_path)).create(
        {"name": "任务引用项目", "source_text": ""}
    )
    repository = JobRepository(provider_database)
    record = repository.create_job(
        project_id=project["id"],
        job_type="provider_security_test",
        provider_kind="text",
        provider_id=provider["id"],
    )
    with provider_database.engine.begin() as connection:
        connection.execute(update(job).where(job.c.id == record["id"]).values(status=status))

    with pytest.raises(ServiceError) as exc_info:
        service.update(provider["id"], {"enabled": False})

    assert exc_info.value.status_code == 409
    assert service.get(provider["id"])["enabled"] is True


# ==================== base_url 内网拦截（H5 残留1 / 报告 3.1）====================


def _provider_config(base_url: str, *, kind: str = "text", adapter: str = "") -> ProviderConfig:
    return ProviderConfig(
        id=f"provider-{kind}-net",
        kind=kind,  # type: ignore[arg-type]
        adapter=adapter or ("openai_compatible" if kind == "text" else "openai_tts"),
        base_url=base_url,
        api_key="test-key",
        model="test-model",
    )


@pytest.mark.parametrize(
    "base_url",
    [
        "http://127.0.0.1:11434/v1",
        "http://localhost:8000/v1",
        "http://2130706433/v1",
        "http://169.254.169.254/latest/meta-data/",
        "http://192.168.1.20:8080/v1",
    ],
)
def test_provider_create_with_private_base_url_returns_400(
    provider_database: Database,
    base_url: str,
) -> None:
    """内网 base_url 必须在落库前被拒：平台 API 无鉴权，它就是内网探测原语。"""
    service = ProviderService(provider_database)

    with pytest.raises(ServiceError) as exc_info:
        _create_provider(service, name="内网 Provider", base_url=base_url)

    assert exc_info.value.status_code == 400
    assert "内网" in exc_info.value.detail
    with provider_database.engine.connect() as connection:
        names = connection.scalars(select(api_provider.c.name)).all()
    assert "内网 Provider" not in names


def test_provider_create_with_private_base_url_allowed_by_escape_hatch(
    provider_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本地 Provider（Ollama/ComfyUI）用 APP_PROVIDER_ALLOW_PRIVATE_HOSTS 放行。"""
    monkeypatch.setattr(settings, "provider_allow_private_hosts", True)
    service = ProviderService(provider_database)

    provider = _create_provider(
        service,
        name="本地 Ollama",
        base_url="http://127.0.0.1:11434/v1",
    )

    assert provider["base_url"] == "http://127.0.0.1:11434/v1"


def test_provider_create_with_private_base_url_allowed_by_platform_setting(
    provider_database: Database,
) -> None:
    """平台设置页开启开关后，本地 Provider 无需改 .env 重启即可保存并使用。"""
    set_provider_allow_private_hosts(provider_database, True)
    service = ProviderService(provider_database)

    provider = _create_provider(
        service,
        name="本地 Ollama（平台开关）",
        base_url="http://127.0.0.1:11434/v1",
    )

    assert provider["base_url"] == "http://127.0.0.1:11434/v1"


@pytest.mark.parametrize(
    ("kind", "base_url"),
    [
        ("text", "http://127.0.0.1:9999/v1"),
        ("tts", "http://192.168.1.20:8080/v1"),
    ],
)
def test_provider_construction_rejects_private_base_url(kind: str, base_url: str) -> None:
    """运行时守卫：绕过 API 直接改库写入的内网 base_url 仍不能发出请求。"""
    with pytest.raises(ProviderConfigurationError, match="内网地址"):
        provider_registry.create(_provider_config(base_url, kind=kind))


def test_provider_construction_allows_private_base_url_with_escape_hatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "provider_allow_private_hosts", True)

    assert provider_registry.create(_provider_config("http://127.0.0.1:9999/v1")) is not None


def test_provider_construction_allows_public_base_url() -> None:
    """不能误伤：保留测试域名与公网地址必须照常构造。"""
    assert provider_registry.create(_provider_config("https://api.example.invalid/v1")) is not None


def test_provider_probe_rejects_private_target_before_sending_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """连接测试的目标由配置控制，校验必须发生在发出请求之前。"""

    class _NoRequest:
        @staticmethod
        def request(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("内网目标不应发出请求")

    # 请求最终由 app.providers.base.request_with_redirect_guard 发出，这里同时
    # 拦两个入口：即便预检被误删，也不会真的把请求打到 127.0.0.1。
    monkeypatch.setattr(provider_base_module, "requests", _NoRequest)
    monkeypatch.setattr(provider_service_module, "requests", _NoRequest)

    ok, message = ProviderService._probe(
        {
            "kind": "text",
            "adapter": "openai_compatible",
            "base_url": "http://127.0.0.1:9999/v1",
            "api_key": "test-key",
            "api_key_env": "",
            "params": {},
            "timeout_sec": 5,
        }
    )

    assert ok is False
    assert "内网地址" in message


# ==================== 重定向逐跳校验与连接测试加固（P1-2）====================

_PUBLIC_PROBE_CONFIG: dict[str, Any] = {
    "kind": "text",
    "adapter": "openai_compatible",
    "base_url": "https://93.184.216.34/v1",
    "api_key": "test-key",
    "api_key_env": "",
    "params": {},
    "timeout_sec": 5,
}


class _FakeRedirectResponse:
    def __init__(self, location: str) -> None:
        self.status_code = 302
        self.is_redirect = True
        self.is_permanent_redirect = False
        self.headers = {"Location": location}
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_redirect_guard_rejects_private_redirect_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """公网首跳返回 302 到元数据地址时，必须在第二跳前拦截，不能自动跟随。"""
    requested: list[str] = []

    class _FakeRequests:
        @staticmethod
        def request(_method: str, url: str, **_kwargs: Any) -> Any:
            requested.append(url)
            return _FakeRedirectResponse("http://169.254.169.254/latest/meta-data/")

    monkeypatch.setattr(provider_base_module, "requests", _FakeRequests)

    with pytest.raises(ProviderError, match="内网地址"):
        request_with_redirect_guard(
            "GET",
            "https://93.184.216.34/v1/models",
            timeout=5,
        )

    assert requested == ["https://93.184.216.34/v1/models"]


def test_redirect_guard_strips_authorization_on_cross_host_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """跨主机重定向必须剥离鉴权头，避免把 Key 发给重定向目标。"""
    calls: list[dict[str, Any]] = []

    class _FinalResponse:
        status_code = 200
        is_redirect = False
        is_permanent_redirect = False
        headers: dict[str, str] = {}

    class _FakeRequests:
        @staticmethod
        def request(_method: str, url: str, **kwargs: Any) -> Any:
            calls.append({"url": url, "headers": dict(kwargs.get("headers") or {})})
            if len(calls) == 1:
                return _FakeRedirectResponse("https://api.other-provider.example/v1/models")
            return _FinalResponse()

    monkeypatch.setattr(provider_base_module, "requests", _FakeRequests)

    response = request_with_redirect_guard(
        "GET",
        "https://93.184.216.34/v1/models",
        timeout=5,
        headers={"Authorization": "Bearer secret", "Accept": "application/json"},
    )

    assert response.status_code == 200
    assert calls[0]["headers"]["Authorization"] == "Bearer secret"
    assert "Authorization" not in calls[1]["headers"]
    assert calls[1]["headers"]["Accept"] == "application/json"


def test_provider_probe_rejects_non_idempotent_test_method() -> None:
    config = {**_PUBLIC_PROBE_CONFIG, "params": {"test_method": "DELETE"}}

    ok, message = ProviderService._probe(config)

    assert ok is False
    assert "GET/HEAD" in message


def test_provider_probe_forces_get_and_verifies_ssl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """连接测试不接受 Provider 配置的 verify_ssl=false，也不能用任意方法。"""
    captured: dict[str, Any] = {}

    class _Response:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

    def fake_guard(method: str, url: str, **kwargs: Any) -> Any:
        captured["method"] = method
        captured.update(kwargs)
        return _Response()

    monkeypatch.setattr(provider_service_module, "request_with_redirect_guard", fake_guard)
    config = {
        **_PUBLIC_PROBE_CONFIG,
        "params": {"test_method": "get", "verify_ssl": False},
    }

    ok, message = ProviderService._probe(config)

    assert ok is True
    assert captured["method"] == "GET"
    assert captured["verify"] is True
    assert "连接测试通过" in message


# ==================== 默认 Provider 唯一性（P1-6）====================


def test_provider_create_with_default_resets_other_defaults(
    provider_database: Database,
) -> None:
    service = ProviderService(provider_database)
    _create_provider(service, name="默认一", is_default=True)

    second = _create_provider(service, name="默认二", is_default=True)

    with provider_database.engine.connect() as connection:
        defaults = connection.scalars(
            select(api_provider.c.id).where(
                api_provider.c.kind == "text",
                api_provider.c.is_default.is_(True),
            )
        ).all()
    assert defaults == [second["id"]]


def test_provider_update_with_default_resets_other_defaults(
    provider_database: Database,
) -> None:
    service = ProviderService(provider_database)
    _create_provider(service, name="默认一", is_default=True)
    other = _create_provider(service, name="普通二", is_default=False)

    service.update(other["id"], {"is_default": True})

    with provider_database.engine.connect() as connection:
        defaults = connection.scalars(
            select(api_provider.c.id).where(
                api_provider.c.kind == "text",
                api_provider.c.is_default.is_(True),
            )
        ).all()
    assert defaults == [other["id"]]
