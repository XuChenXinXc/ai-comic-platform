from __future__ import annotations

import os
import re
from typing import Any

import requests
from sqlalchemy import delete, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.db import Database, db
from app.models.tables import (
    api_provider,
    asset_image,
    audio_config,
    job,
    keyframe_version,
    project,
    shot_audio,
    video_version,
)
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
        # api_key_source 与 api_key_configured 保持一致：配置了 env 但环境变量
        # 未设置时不算「已配置」，避免前端误以为密钥可用。
        result["api_key_source"] = (
            "direct"
            if ciphertext
            else "env"
            if has_env_key and result.get("api_key_env")
            else "none"
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
        name_str = str(name)
        # 超长输入不能直接放行（否则可绕过脱敏/写入拦截）：
        # 长度超过 200 时按敏感后缀判断，仍视为敏感并 redact/拒绝。
        if len(name_str) > 200:
            lowered = name_str.casefold()
            return any(
                lowered.endswith(suffix)
                for suffix in (
                    "key",
                    "token",
                    "secret",
                    "password",
                    "passwd",
                    "auth",
                    "cookie",
                    "signature",
                    "credential",
                    "credentials",
                )
            )
        normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name_str)
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
        # 显式传 null 的字段视为「保持不变」：这些列 NOT NULL，
        # 裸写 None 会触发 IntegrityError 500。
        changes = {key: value for key, value in changes.items() if value is not None}
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
                if self._has_active_job(connection, provider_id):
                    raise conflict("Provider 存在进行中的任务，不能删除或停用")
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
                    raise conflict("Provider 已被项目引用，不能删除")
                # 历史引用（已终态的媒体/任务）允许删除：把引用列置 NULL，
                # 而不是依赖外键 IntegrityError 永久阻断删除。
                for reference_column in (
                    asset_image.c.provider_id,
                    keyframe_version.c.provider_id,
                    video_version.c.provider_id,
                    shot_audio.c.provider_id,
                ):
                    connection.execute(
                        update(reference_column.table)
                        .where(reference_column == provider_id)
                        .values(**{reference_column.name: None})
                    )
                connection.execute(
                    update(audio_config)
                    .where(audio_config.c.tts_provider_id == provider_id)
                    .values(tts_provider_id=None)
                )
                # 历史任务同样允许删除：置空引用，与媒体表口径一致。
                # 此前遗漏 job.provider_id，导致任何跑过任务的 Provider
                # 都会撞外键 IntegrityError 被误报为「已被任务引用」。
                connection.execute(
                    update(job)
                    .where(job.c.provider_id == provider_id)
                    .values(provider_id=None)
                )
                connection.execute(delete(api_provider).where(api_provider.c.id == provider_id))
        except IntegrityError as exc:
            raise conflict("Provider 已被媒体或任务引用，不能删除") from exc

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

    def resolve_tts_provider(self, provider_id: str) -> tuple[Any, dict[str, Any]]:
        """按 ID 解析 TTS Provider（公开接口，供音频服务使用）。

        内部复用 resolve() 的同款校验：存在性、kind 必须为 tts、启用状态、
        配置校验与 API Key 解密，避免调用方绕过校验直接访问私有方法。
        """
        return self.resolve(kind="tts", provider_id=provider_id)

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
                    "Provider API Key 解密失败，请在当前平台重新填写 API Key"
                ) from exc
        else:
            runtime["api_key"] = ""
        # api_key_env 回退：只配环境变量名而未保存明文/密文时，运行时从环境取值，
        # 与 _probe（连接测试）口径一致。此前该回退只在 _probe 中存在，
        # 导致仅配 api_key_env 的 TTS Provider 实际运行时拿到空 Key。
        if not runtime.get("api_key") and runtime.get("api_key_env"):
            validate_api_key_env(str(runtime.get("kind", "")), str(runtime["api_key_env"]))
            runtime["api_key"] = os.getenv(runtime["api_key_env"], "")
        return runtime
