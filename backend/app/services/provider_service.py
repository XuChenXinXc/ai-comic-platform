from __future__ import annotations

import os
import re
from typing import Any

import requests
from sqlalchemy import delete, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.db import Database, db
from app.models.tables import api_provider, job, project
from app.providers.base import (
    ProviderConfigurationError,
    redact_provider_base_url,
    resolve_provider_url,
    validate_api_key_env,
    validate_custom_headers,
    validate_provider_base_url,
)
from app.providers.registry import provider_registry
from app.services.errors import bad_request, conflict, not_found
from app.services.secret_service import SecretServiceError, decrypt_secret, encrypt_secret
from app.services.utils import (
    json_dumps,
    json_loads,
    new_id,
    row_dict,
    sanitize_error_message,
    utc_now,
)


class ProviderService:
    def __init__(self, database: Database | None = None) -> None:
        self.database = database or db

    @staticmethod
    def _decode(
        record: dict[str, Any],
        *,
        redact_base_url: bool = True,
        include_secret: bool = False,
    ) -> dict[str, Any]:
        result = dict(record)
        ciphertext = result.pop("api_key_ciphertext", None) or ""
        params = ProviderService._redact_params(json_loads(result["params_json"], {}))
        result["params_json"] = params
        result["params"] = params
        has_env_key = bool(result.get("api_key_env") and os.getenv(result["api_key_env"]))
        result["api_key_configured"] = bool(ciphertext) or has_env_key
        result["api_key_source"] = (
            "direct" if ciphertext else "env" if result.get("api_key_env") else "none"
        )
        if include_secret:
            result["_api_key_ciphertext"] = ciphertext
        if redact_base_url and result.get("base_url"):
            try:
                validate_provider_base_url(str(result["base_url"]))
            except ProviderConfigurationError:
                result["base_url"] = redact_provider_base_url(str(result["base_url"]))
        result["enabled"] = bool(result["enabled"])
        result["is_default"] = bool(result["is_default"])
        if result.get("last_test_ok") is not None:
            result["last_test_ok"] = bool(result["last_test_ok"])
        return result

    @staticmethod
    def _is_sensitive_name(name: Any) -> bool:
        normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(name))
        parts = {part for part in re.split(r"[^a-z0-9]+", normalized.lower()) if part}
        collapsed = re.sub(r"[^a-z0-9]", "", normalized.lower())
        return bool(
            parts
            & {
                "auth",
                "authorization",
                "credential",
                "credentials",
                "token",
                "key",
                "secret",
                "password",
                "passwd",
                "cookie",
                "sig",
                "signature",
            }
            or collapsed
            in {
                "apikey",
                "xapikey",
                "accesstoken",
                "accesskey",
                "authtoken",
                "bearertoken",
                "clientsecret",
                "privatekey",
                "proxyauthorization",
                "secretkey",
                "setcookie",
            }
        )

    @classmethod
    def _redact_params(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: "[REDACTED]" if cls._is_sensitive_name(key) else cls._redact_params(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [cls._redact_params(item) for item in value]
        return value

    @classmethod
    def _sensitive_param_path(cls, value: Any, path: str = "params") -> str | None:
        if isinstance(value, dict):
            for key, item in value.items():
                current = f"{path}.{key}"
                if cls._is_sensitive_name(key):
                    return current
                nested = cls._sensitive_param_path(item, current)
                if nested:
                    return nested
        elif isinstance(value, list):
            for index, item in enumerate(value):
                nested = cls._sensitive_param_path(item, f"{path}[{index}]")
                if nested:
                    return nested
        return None

    @classmethod
    def _validate_config(
        cls,
        config: dict[str, Any],
        *,
        reject_mock_runtime_paths: bool = True,
    ) -> None:
        try:
            validate_provider_base_url(str(config.get("base_url", "")))
            validate_api_key_env(str(config["kind"]), str(config.get("api_key_env", "")))
        except ProviderConfigurationError as exc:
            raise bad_request(str(exc)) from exc
        params = config.get("params", {})
        sensitive_path = cls._sensitive_param_path(params)
        if sensitive_path:
            raise bad_request(
                f"Provider params 不能保存 Authorization/token/key/secret/password/cookie："
                f"{sensitive_path}"
            )
        if reject_mock_runtime_paths and config.get("adapter") == "mock":
            for key in ("task_store_dir", "ffmpeg_path"):
                if key in params:
                    raise bad_request(f"Mock Provider 不允许通过 API 配置 {key}")

    @staticmethod
    def _has_active_job(connection: Any, provider_id: str) -> bool:
        return (
            connection.execute(
                select(job.c.id)
                .where(
                    job.c.provider_id == provider_id,
                    job.c.status.in_(("queued", "running", "waiting_provider", "downloading")),
                )
                .limit(1)
            ).first()
            is not None
        )

    def list(self, kind: str | None = None) -> list[dict[str, Any]]:
        statement = select(api_provider)
        if kind:
            statement = statement.where(api_provider.c.kind == kind)
        statement = statement.order_by(
            api_provider.c.kind, api_provider.c.is_default.desc(), api_provider.c.name
        )
        with self.database.engine.connect() as connection:
            return [self._decode(dict(row._mapping)) for row in connection.execute(statement)]

    def _get_config(
        self,
        provider_id: str,
        *,
        redact_base_url: bool,
        include_secret: bool = False,
    ) -> dict[str, Any]:
        with self.database.engine.connect() as connection:
            record = row_dict(
                connection.execute(
                    select(api_provider).where(api_provider.c.id == provider_id)
                ).first()
            )
        if record is None:
            raise not_found("Provider")
        return self._decode(
            record,
            redact_base_url=redact_base_url,
            include_secret=include_secret,
        )

    def get(self, provider_id: str) -> dict[str, Any]:
        return self._get_config(provider_id, redact_base_url=True)

    def create(self, values: dict[str, Any]) -> dict[str, Any]:
        values = dict(values)
        now = utc_now()
        params = values.pop("params", {})
        api_key = str(values.pop("api_key", "") or "")
        try:
            api_key_ciphertext = encrypt_secret(api_key) if api_key else ""
        except SecretServiceError as exc:
            raise bad_request(str(exc)) from exc
        record = {
            "id": new_id("provider"),
            **values,
            "api_key_ciphertext": api_key_ciphertext or None,
            "params_json": json_dumps(params),
            "created_at": now,
            "updated_at": now,
        }
        self._validate_config({**record, "params": params})
        try:
            provider_registry.create(
                self._runtime_config({**record, "params": params}, api_key=api_key)
            )
        except ProviderConfigurationError as exc:
            raise bad_request(str(exc)) from exc
        with self.database.engine.begin() as connection:
            if record["is_default"]:
                connection.execute(
                    update(api_provider)
                    .where(api_provider.c.kind == record["kind"])
                    .values(is_default=False)
                )
            connection.execute(insert(api_provider).values(**record))
        return self.get(record["id"])

    def update(self, provider_id: str, values: dict[str, Any]) -> dict[str, Any]:
        current = self._get_config(
            provider_id,
            redact_base_url=False,
            include_secret=True,
        )
        with self.database.engine.connect() as connection:
            if self._has_active_job(connection, provider_id):
                raise conflict("Provider 存在进行中的任务，不能更新或停用")
        changes = dict(values)
        api_key = changes.pop("api_key", None)
        if api_key:
            try:
                changes["api_key_ciphertext"] = encrypt_secret(str(api_key))
            except SecretServiceError as exc:
                raise bad_request(str(exc)) from exc
        if "params" in changes:
            changes["params_json"] = json_dumps(changes.pop("params"))
        candidate = {**current, **changes}
        candidate["params"] = json_loads(
            changes.get("params_json", json_dumps(current["params"])), {}
        )
        self._validate_config(candidate)
        try:
            provider_registry.create(self._runtime_config(candidate))
        except ProviderConfigurationError as exc:
            raise bad_request(str(exc)) from exc
        changes["updated_at"] = utc_now()
        with self.database.engine.begin() as connection:
            if self._has_active_job(connection, provider_id):
                raise conflict("Provider 存在进行中的任务，不能更新或停用")
            if changes.get("is_default"):
                connection.execute(
                    update(api_provider)
                    .where(
                        api_provider.c.kind == current["kind"],
                        api_provider.c.id != provider_id,
                    )
                    .values(is_default=False)
                )
            connection.execute(
                update(api_provider).where(api_provider.c.id == provider_id).values(**changes)
            )
        return self.get(provider_id)

    def delete(self, provider_id: str) -> None:
        self.get(provider_id)
        if provider_id in {"mock-text", "mock-image", "mock-video"}:
            raise conflict("内置 Mock Provider 不能删除，可以停用")
        try:
            with self.database.engine.begin() as connection:
                project_reference = connection.execute(
                    select(project.c.id)
                    .where(
                        or_(
                            project.c.default_text_provider_id == provider_id,
                            project.c.default_image_provider_id == provider_id,
                            project.c.default_video_provider_id == provider_id,
                        )
                    )
                    .limit(1)
                ).first()
                if project_reference:
                    raise conflict("Provider 已被项目、媒体或任务引用，不能删除")
                connection.execute(delete(api_provider).where(api_provider.c.id == provider_id))
        except IntegrityError as exc:
            raise conflict("Provider 已被项目、媒体或任务引用，不能删除") from exc

    def resolve(
        self,
        *,
        kind: str,
        provider_id: str | None = None,
    ) -> tuple[Any, dict[str, Any]]:
        if provider_id:
            config = self._get_config(
                provider_id,
                redact_base_url=False,
                include_secret=True,
            )
        else:
            with self.database.engine.connect() as connection:
                record = row_dict(
                    connection.execute(
                        select(api_provider).where(
                            api_provider.c.kind == kind,
                            api_provider.c.is_default.is_(True),
                        )
                    ).first()
                )
            if record is None:
                raise bad_request(f"未配置默认{kind} Provider")
            config = self._decode(
                record,
                redact_base_url=False,
                include_secret=True,
            )
        if config["kind"] != kind or not config["enabled"]:
            raise bad_request(f"{kind} Provider 不存在或未启用")
        self._validate_config(config, reject_mock_runtime_paths=False)
        try:
            provider = provider_registry.get(self._runtime_config(config))
        except ProviderConfigurationError as exc:
            raise bad_request(str(exc)) from exc
        resolved_config = dict(config)
        resolved_config.pop("_api_key_ciphertext", None)
        return provider, resolved_config

    def test(self, provider_id: str) -> dict[str, Any]:
        config = self._get_config(
            provider_id,
            redact_base_url=False,
            include_secret=True,
        )
        self._validate_config(config, reject_mock_runtime_paths=False)
        runtime_config = self._runtime_config(config)
        try:
            provider_registry.get(runtime_config)
        except ProviderConfigurationError as exc:
            raise bad_request(str(exc)) from exc
        try:
            ok, message = self._probe(runtime_config)
        except ProviderConfigurationError as exc:
            raise bad_request(str(exc)) from exc
        now = utc_now()
        with self.database.engine.begin() as connection:
            connection.execute(
                update(api_provider)
                .where(api_provider.c.id == provider_id)
                .values(
                    last_test_ok=ok,
                    last_test_message=message,
                    updated_at=now,
                )
            )
        return {"ok": ok, "message": message}

    @staticmethod
    def _probe(config: dict[str, Any]) -> tuple[bool, str]:
        if config["adapter"] == "mock":
            return True, "Mock Provider 可用"
        if not config["base_url"]:
            return False, "非 Mock Provider 必须配置 base_url"
        api_key = str(config.get("api_key") or "")
        if not api_key and config["api_key_env"]:
            validate_api_key_env(config["kind"], config["api_key_env"])
            api_key = os.getenv(config["api_key_env"], "")
        if not api_key:
            return False, "请配置 API Key"
        params = config["params"]
        if config["adapter"] == "openai_compatible":
            path = str(params.get("test_path", "models"))
        else:
            path = str(params.get("test_path", ""))
        url = resolve_provider_url(config["base_url"], path)
        headers = validate_custom_headers(params)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            response = requests.request(
                str(params.get("test_method", "GET")),
                url,
                headers=headers,
                timeout=min(config["timeout_sec"], 10),
                verify=bool(params.get("verify_ssl", True)),
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            return False, f"连接测试失败：{sanitize_error_message(exc)}"
        return True, f"连接测试通过（HTTP {response.status_code}）"

    @staticmethod
    def _runtime_config(
        config: dict[str, Any],
        *,
        api_key: str | None = None,
    ) -> dict[str, Any]:
        runtime = dict(config)
        ciphertext = runtime.pop("api_key_ciphertext", None)
        if ciphertext is None:
            ciphertext = runtime.pop("_api_key_ciphertext", None)
        if api_key is not None:
            runtime["api_key"] = api_key
        elif ciphertext:
            try:
                runtime["api_key"] = decrypt_secret(str(ciphertext))
            except SecretServiceError as exc:
                raise bad_request(
                    "Provider API Key 解密失败，请使用保存该配置的 Windows 用户运行平台"
                ) from exc
        else:
            runtime["api_key"] = ""
        return runtime
