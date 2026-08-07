from __future__ import annotations

import asyncio
import base64
import os
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from pydantic import BaseModel

from app.schemas import ProviderConfig, ProviderPollResult, ProviderSubmission
from app.services.file_service import atomic_write_bytes, download_url_atomic
from app.services.utils import sanitize_error_message


class ProviderError(RuntimeError):
    pass


class ProviderConfigurationError(ProviderError):
    pass


_SENSITIVE_QUERY_PARTS = {
    "auth",
    "authorization",
    "cookie",
    "credential",
    "credentials",
    "key",
    "password",
    "secret",
    "sig",
    "signature",
    "token",
}
_SENSITIVE_QUERY_ALIASES = {
    "apikey",
    "xapikey",
    "accesskey",
    "accesstoken",
    "authtoken",
    "bearertoken",
    "clientsecret",
    "privatekey",
    "secretkey",
}


def _is_sensitive_query_name(name: str) -> bool:
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    parts = {part for part in re.split(r"[^a-z0-9]+", normalized.lower()) if part}
    collapsed = re.sub(r"[^a-z0-9]", "", normalized.lower())
    return bool(parts & _SENSITIVE_QUERY_PARTS or collapsed in _SENSITIVE_QUERY_ALIASES)


def validate_api_key_env(kind: str, api_key_env: str) -> None:
    if not api_key_env:
        return
    prefix = kind.upper()
    pattern = rf"{re.escape(prefix)}_(?:[A-Z0-9]+_)*API_KEY"
    if re.fullmatch(pattern, api_key_env) is None:
        raise ProviderConfigurationError(
            f"{kind} Provider 的 api_key_env 必须使用 {prefix}_API_KEY "
            f"或 {prefix}_*_API_KEY 命名，例如 {prefix}_OPENAI_API_KEY"
        )


def _url_origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ProviderConfigurationError("Provider URL 必须是有效的 HTTP(S) 地址")
    if parsed.username is not None or parsed.password is not None:
        raise ProviderConfigurationError("Provider URL 不能包含用户名或密码")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ProviderConfigurationError("Provider URL 端口无效") from exc
    return (
        parsed.scheme.lower(),
        parsed.hostname.lower(),
        port or (443 if parsed.scheme.lower() == "https" else 80),
    )


def validate_provider_base_url(base_url: str) -> None:
    if not base_url:
        return
    _url_origin(base_url)
    for name, _ in parse_qsl(urlsplit(base_url).query, keep_blank_values=True):
        if _is_sensitive_query_name(name):
            raise ProviderConfigurationError(f"Provider base_url 不能包含敏感查询参数：{name}")


def redact_provider_base_url(base_url: str) -> str:
    if not base_url:
        return ""
    parsed = urlsplit(base_url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return "[已隐藏]"
    try:
        port = parsed.port
    except ValueError:
        return "[已隐藏]"
    hostname = parsed.hostname
    netloc = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None:
        netloc = f"{netloc}:{port}"
    query = urlencode(
        [
            (name, "[已隐藏]" if _is_sensitive_query_name(name) else value)
            for name, value in parse_qsl(parsed.query, keep_blank_values=True)
        ],
        doseq=True,
        safe="[]",
    )
    return urlunsplit((parsed.scheme, netloc, parsed.path, query, ""))


def resolve_provider_url(base_url: str, path: str) -> str:
    if not base_url:
        raise ProviderConfigurationError("Provider 未配置 base_url")
    base_origin = _url_origin(base_url)
    resolved = urljoin(f"{base_url.rstrip('/')}/", path.lstrip("/"))
    if _url_origin(resolved) != base_origin:
        raise ProviderConfigurationError("Provider 请求路径必须与 base_url 同源")
    return resolved


def is_same_origin(base_url: str, url: str) -> bool:
    try:
        return _url_origin(base_url) == _url_origin(url)
    except ProviderConfigurationError:
        return False


def validate_custom_headers(options: Mapping[str, Any]) -> dict[str, str]:
    raw_headers = options.get("headers", {})
    if not isinstance(raw_headers, Mapping):
        raise ProviderConfigurationError("Provider headers 必须是对象")
    headers = {str(key): str(value) for key, value in raw_headers.items()}
    sensitive_parts = {
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
    for name in headers:
        normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
        parts = {part for part in re.split(r"[^a-z0-9]+", normalized.lower()) if part}
        collapsed = re.sub(r"[^a-z0-9]", "", normalized.lower())
        if parts & sensitive_parts or collapsed in {
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
        }:
            raise ProviderConfigurationError(f"Provider headers 不能包含敏感字段：{name}")
    return headers


@runtime_checkable
class TextProvider(Protocol):
    async def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: type[BaseModel],
        params: dict[str, Any],
    ) -> BaseModel: ...


@runtime_checkable
class ImageProvider(Protocol):
    async def submit(
        self,
        *,
        prompt: str,
        negative_prompt: str,
        reference_files: list[Path],
        params: dict[str, Any],
    ) -> ProviderSubmission: ...

    async def poll(self, task_id: str) -> ProviderPollResult: ...

    async def download(self, result: ProviderPollResult, target: Path) -> Path: ...


@runtime_checkable
class VideoProvider(Protocol):
    async def prepare_input_image(self, local_file: Path) -> dict[str, Any]: ...

    async def submit(
        self,
        *,
        prompt: str,
        input_image: dict[str, Any],
        duration_sec: float,
        params: dict[str, Any],
    ) -> ProviderSubmission: ...

    async def poll(self, task_id: str) -> ProviderPollResult: ...

    async def download(self, result: ProviderPollResult, target: Path) -> Path: ...


@runtime_checkable
class TTSProvider(Protocol):
    async def synthesize(
        self,
        *,
        text: str,
        voice_id: str,
        params: dict[str, Any],
    ) -> bytes: ...

    def supports_voice(self, voice_id: str) -> bool: ...


def extract_path(value: Any, path: str, default: Any = None) -> Any:
    if not path:
        return value
    current = value
    for part in path.split("."):
        if isinstance(current, Mapping):
            if part not in current:
                return default
            current = current[part]
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            if index >= len(current):
                return default
            current = current[index]
        else:
            return default
    return current


def normalize_provider_status(
    raw_status: Any,
    *,
    completed_values: list[str] | None = None,
    failed_values: list[str] | None = None,
) -> str:
    status = str(raw_status or "").lower()
    completed = {
        item.lower() for item in (completed_values or ["completed", "succeeded", "success"])
    }
    failed = {item.lower() for item in (failed_values or ["failed", "error", "cancelled"])}
    if status in completed:
        return "completed"
    if status in failed:
        return "failed"
    return "waiting_provider"


class HttpProviderBase:
    def __init__(self, config: ProviderConfig) -> None:
        validate_provider_base_url(config.base_url)
        self.config = config

    def _api_key(self) -> str:
        if self.config.api_key:
            return self.config.api_key
        if not self.config.api_key_env:
            return ""
        validate_api_key_env(self.config.kind, self.config.api_key_env)
        value = os.getenv(self.config.api_key_env, "")
        if not value:
            raise ProviderConfigurationError(f"环境变量 {self.config.api_key_env} 未配置")
        return value

    def _headers(self, options: Mapping[str, Any]) -> dict[str, str]:
        headers = validate_custom_headers(options)
        api_key = self._api_key()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    def _url(self, path: str) -> str:
        return resolve_provider_url(self.config.base_url, path)

    def _request(
        self,
        method: str,
        url: str,
        *,
        options: Mapping[str, Any],
        **kwargs: Any,
    ) -> requests.Response:
        last_error: Exception | None = None
        last_http_failure = ""
        method_name = method.strip().upper()
        retry_unsafe_methods = bool(options.get("retry_unsafe_methods", False))
        attempts = self.config.max_retries + 1
        safe_method = method_name in {"GET", "HEAD", "OPTIONS"} or retry_unsafe_methods
        # 非安全方法（POST 等）默认不重试；但瞬时性错误（429/5xx 过载、限流）除外，
        # 这类错误重试不会造成重复提交语义问题，能显著提升上游短暂过载时的可用性。
        transient_statuses = {429, 500, 502, 503, 504}
        for attempt in range(attempts):
            try:
                response = requests.request(
                    method,
                    url,
                    timeout=self.config.timeout_sec,
                    verify=bool(options.get("verify_ssl", True)),
                    **kwargs,
                )
                response.raise_for_status()
                return response
            except requests.HTTPError as exc:
                last_error = exc
                last_http_failure = self._http_failure_message(exc.response)
                status_code = exc.response.status_code if exc.response is not None else 0
                if not safe_method and status_code not in transient_statuses:
                    break
                if attempt >= attempts - 1:
                    break
                time.sleep(min(2**attempt * 0.25, 2))
            except requests.RequestException as exc:
                last_error = exc
                if not safe_method:
                    break
                if attempt >= attempts - 1:
                    break
                time.sleep(min(2**attempt * 0.25, 2))
        if last_http_failure:
            raise ProviderError(last_http_failure) from last_error
        raise ProviderError(f"Provider 请求失败：{last_error}") from last_error

    @staticmethod
    def _http_failure_message(response: requests.Response | None) -> str:
        if response is None:
            return ""
        provider_message = ""
        try:
            payload = response.json()
        except requests.JSONDecodeError:
            payload = None
        if isinstance(payload, Mapping):
            error = payload.get("error")
            if isinstance(error, Mapping):
                provider_message = str(error.get("message") or error.get("detail") or "")
            elif isinstance(error, str):
                provider_message = error
            if not provider_message:
                provider_message = str(payload.get("message") or payload.get("detail") or "")
        prefix = f"Provider 请求失败（HTTP {response.status_code}）"
        if not provider_message:
            return prefix
        return f"{prefix}：{sanitize_error_message(provider_message)}"

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        options: Mapping[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        def request() -> dict[str, Any]:
            response = self._request(method, self._url(path), options=options, **kwargs)
            try:
                payload = response.json()
            except requests.JSONDecodeError as exc:
                raise ProviderError("Provider 返回的不是 JSON") from exc
            if not isinstance(payload, dict):
                raise ProviderError("Provider JSON 响应必须是对象")
            return payload

        return await asyncio.to_thread(request)

    async def _download_poll_result(
        self,
        result: ProviderPollResult,
        target: Path,
    ) -> Path:
        if result.content_base64:
            encoded = result.content_base64
            if encoded.startswith("data:") and "," in encoded:
                encoded = encoded.split(",", 1)[1]
            try:
                content = base64.b64decode(encoded, validate=True)
            except ValueError as exc:
                raise ProviderError("Provider 返回的 Base64 文件无效") from exc
            return await asyncio.to_thread(atomic_write_bytes, target, content)
        local_file = result.data.get("local_file")
        if local_file:
            from app.services.file_service import atomic_copy

            return await asyncio.to_thread(atomic_copy, local_file, target)
        if not result.result_url:
            raise ProviderError("Provider 结果不包含下载地址或文件内容")
        if result.result_url.startswith(("http://", "https://")):
            download_url = result.result_url
            _url_origin(download_url)
        else:
            download_url = self._url(result.result_url)
        validate_custom_headers(self.config.params)
        headers = (
            self._headers(self.config.params)
            if is_same_origin(self.config.base_url, download_url)
            else {}
        )
        return await asyncio.to_thread(
            download_url_atomic,
            download_url,
            target,
            headers=headers,
            timeout=self.config.timeout_sec,
        )
