from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import insert, select, update

from app.api.schemas import GenerateRequest, ProviderCreate, ProviderUpdate
from app.db import Database
from app.jobs.repository import JobRepository
from app.models.tables import api_provider, job
from app.services.errors import ServiceError
from app.services.file_service import FileService
from app.services.project_service import ProjectService
from app.services.provider_service import ProviderService
from app.services.utils import json_dumps, new_id, utc_now


@pytest.fixture
def provider_database(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{(tmp_path / 'provider-security.db').as_posix()}")
    database.initialize(seed_mock_providers=True)
    yield database
    database.dispose()


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


def test_provider_blank_update_preserves_direct_api_key(
    provider_database: Database,
) -> None:
    service = ProviderService(provider_database)
    provider = _create_provider(
        service,
        name="保留密钥 Provider",
        api_key_env="",
        api_key="direct-provider-secret",
    )

    updated = service.update(provider["id"], {"name": "更新名称", "api_key": ""})

    assert updated["api_key_configured"] is True
    assert updated["api_key_source"] == "direct"


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
