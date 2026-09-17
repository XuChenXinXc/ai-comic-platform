from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel

from app.config import settings
from app.schemas import ProviderConfig, ProviderPollResult, ProviderSubmission
from app.services.constants import (
    PROVIDER_REFERENCE_JPEG_QUALITY,
    PROVIDER_REFERENCE_MAX_BYTES,
    PROVIDER_REFERENCE_MAX_SIDE,
)
from app.services.file_service import (
    atomic_write_bytes,
    download_url_atomic,
    file_service,
)
from app.services.net_guard import NO_HOST, inspect_target_host
from app.services.utils import sanitize_error_message

# Provider 结果 Base64 解码后的字节上限（与平台媒体上限对齐，防止解码耗尽内存/磁盘）。
_MAX_BASE64_DECODED_BYTES = 100 * 1024 * 1024  # 100 MB
# 对应的编码后长度上限：4/3 换算 + data URL 头部冗余。
_MAX_BASE64_ENCODED_CHARS = (_MAX_BASE64_DECODED_BYTES * 4 // 3) + 64
# data URL 允许的媒体 MIME 前缀；防止把任意文本伪装成媒体落盘。
_ALLOWED_DATA_URL_MIMES = ("image/", "video/", "audio/", "application/octet-stream")
# 手动逐跳跟随重定向的最大跳数。
_MAX_REDIRECTS = 10


class ProviderError(RuntimeError):
    pass


class ProviderConfigurationError(ProviderError):
    pass


logger = logging.getLogger(__name__)

# B-L10：ImgBB 平台级密钥的运行时覆盖。main.py 启动时从加密的平台设置读取
# 并写入该变量，避免直接改写 lru_cache 单例 settings（后者是全局共享配置
# 对象，被改写后无法区分「环境变量值」与「数据库覆盖值」）。
_imgbb_api_key_override = ""


def set_imgbb_api_key_override(value: str) -> None:
    """写入运行时 ImgBB Key 覆盖；空字符串表示清除覆盖。"""
    global _imgbb_api_key_override
    _imgbb_api_key_override = str(value or "")


def get_imgbb_api_key() -> str:
    """ImgBB Key 读取优先级：运行时覆盖（数据库加密值）> settings 环境变量。"""
    return _imgbb_api_key_override or settings.imgbb_api_key


# P1-3：本地/内网 Provider 放行开关的运行时覆盖。判定发生在每次 Provider
# 构造时，而平台设置存在数据库里；ProviderService 在解析/保存/测试前把数据库
# 值同步到该变量，避免每次请求都查库。None 表示未同步，按环境变量判定，
# 因此 .env 的 APP_PROVIDER_ALLOW_PRIVATE_HOSTS=1 仍然生效。
_provider_allow_private_hosts_override: bool | None = None


def set_provider_allow_private_hosts_override(value: bool | None) -> None:
    """写入本地/内网 Provider 放行开关的运行时覆盖；None 表示回退环境变量。"""
    global _provider_allow_private_hosts_override
    _provider_allow_private_hosts_override = value


def provider_allow_private_hosts() -> bool:
    """是否允许 Provider 连接内网/回环地址。"""
    if _provider_allow_private_hosts_override is not None:
        return _provider_allow_private_hosts_override
    return settings.provider_allow_private_hosts


def compress_reference_image(
    local_file: Path,
    *,
    max_side: int = PROVIDER_REFERENCE_MAX_SIDE,
    quality: int = PROVIDER_REFERENCE_JPEG_QUALITY,
    max_bytes: int = PROVIDER_REFERENCE_MAX_BYTES,
) -> bytes:
    """等比压缩 Provider 输入图：保持宽高比，只缩小超过最长边的图，转 JPEG。"""
    try:
        with Image.open(local_file) as image:
            image = ImageOps.exif_transpose(image)
            width, height = image.size
            scale = min(1.0, max_side / max(width, height))
            if scale < 1.0:
                image = image.resize(
                    (max(1, round(width * scale)), max(1, round(height * scale))),
                    Image.Resampling.LANCZOS,
                )
            rgb = image.convert("RGB")
    except (UnidentifiedImageError, OSError) as exc:
        raise ProviderError(f"参考图无法解码：{local_file}") from exc
    data = b""
    for current_quality in range(max(quality, 50), 49, -10):
        buffer = io.BytesIO()
        rgb.save(buffer, format="JPEG", quality=current_quality)
        data = buffer.getvalue()
        if len(data) <= max_bytes:
            return data
    # 质量降到下限仍超限时，按比例继续缩小分辨率兜底，直到达标或到达最小边长。
    while len(data) > max_bytes and min(rgb.size) > 64:
        smaller_width = max(64, round(rgb.size[0] * 0.8))
        smaller_height = max(64, round(rgb.size[1] * 0.8))
        if (smaller_width, smaller_height) == rgb.size:
            break
        rgb = rgb.resize((smaller_width, smaller_height), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        rgb.save(buffer, format="JPEG", quality=max(quality, 50))
        data = buffer.getvalue()
    if len(data) > max_bytes:
        logger.warning(
            "参考图压缩后仍超过大小上限：%s（%d 字节 > %d 字节）",
            local_file,
            len(data),
            max_bytes,
        )
    return data


def reference_jpeg_part(local_file: Path) -> tuple[str, bytes, str]:
    """压缩参考图并产出 multipart files 三元组：文件名统一清洗非法字符并定型 .jpg。"""
    content = compress_reference_image(local_file)
    filename = re.sub(r"[^A-Za-z0-9_.-]", "_", local_file.name)
    return Path(filename).with_suffix(".jpg").name, content, "image/jpeg"


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
    # 与 provider_service._is_sensitive_name 保持一致：超长输入不能直接放行
    # （否则可绕过 base_url 敏感参数拦截），按敏感后缀判断。
    if len(name) > 200:
        lowered = name.casefold()
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


def _private_host_message(url: str) -> str | None:
    """内网/保留目标的统一文案；放行或非内网返回 None。"""
    verdict = inspect_target_host(url)
    if verdict is None:
        return None
    code, detail = verdict
    if code == NO_HOST:
        # 不回显 URL：它可能带敏感查询参数。
        return "Provider base_url 缺少有效主机名"
    return (
        f"Provider base_url 指向本地或内网地址，已拒绝：{detail}。"
        "如需连接本地 Provider（Ollama/ComfyUI/本地网关），"
        "请在平台设置页开启「允许连接本地/内网 Provider」，"
        "或设置 APP_PROVIDER_ALLOW_PRIVATE_HOSTS=1 后重启"
    )


def ensure_public_network_target(url: str) -> None:
    """重定向逐跳校验：目标为内网/保留地址时抛配置错误。

    与 validate_provider_base_url_network 共用判定与放行开关，
    供 request_with_redirect_guard 在每一跳调用。
    """
    if provider_allow_private_hosts():
        return
    message = _private_host_message(url)
    if message:
        raise ProviderConfigurationError(message)


def validate_provider_base_url_network(base_url: str) -> None:
    """拒绝 base_url 指向内网/回环/保留地址。

    Provider 配置可通过无鉴权的 API 写入，base_url 不加网络策略就等于把后端
    变成内网探测原语（2026-09-11 审查报告 H5 残留1 / 3.1）。

    与 validate_provider_base_url 分开：后者同时被 provider_service 用来判断
    base_url 是否需要脱敏，把网络策略混进去会让内网配置在 API 响应里被误脱敏。

    需要接本地 Provider（Ollama / ComfyUI / 本地网关）时，可在平台设置页开启
    「允许连接本地/内网 Provider」，或设置 APP_PROVIDER_ALLOW_PRIVATE_HOSTS=1。
    """
    if not base_url:
        return
    ensure_public_network_target(base_url)


def request_with_redirect_guard(
    method: str,
    url: str,
    *,
    timeout: float,
    verify: bool = True,
    **kwargs: Any,
) -> requests.Response:
    """发送请求并手动逐跳跟随重定向，每一跳都校验目标主机。

    requests 默认会跟随 30x 到任意目标，首跳校验挡不住
    「公网 base_url 302 到 169.254.169.254 / 内网」的 SSRF 变体。
    这里显式 allow_redirects=False，逐跳调用 ensure_public_network_target，
    并在跨主机时剥离 Authorization/Cookie，与 file_service 下载路径口径一致。

    通过 requests.request 发出（兼容既有对该入口的猴子补丁）。
    """
    return _follow_redirects(_default_sender, method, url, timeout=timeout, verify=verify, **kwargs)


def post_with_redirect_guard(
    url: str,
    *,
    timeout: float,
    verify: bool = True,
    **kwargs: Any,
) -> requests.Response:
    """POST 版本：首跳走 requests.post（TTS 等既有调用链依赖该入口）。

    重定向改 GET 时自动回退到通用入口，避免用 POST 发出 GET。
    """
    return _follow_redirects(_post_sender, "POST", url, timeout=timeout, verify=verify, **kwargs)


def _default_sender(method: str, url: str, **kwargs: Any) -> requests.Response:
    return requests.request(method, url, **kwargs)


def _post_sender(method: str, url: str, **kwargs: Any) -> requests.Response:
    if method == "POST":
        return requests.post(url, **kwargs)
    return requests.request(method, url, **kwargs)


def _follow_redirects(
    send: Any,
    method: str,
    url: str,
    *,
    timeout: float,
    verify: bool,
    **kwargs: Any,
) -> requests.Response:
    current_method = method.strip().upper()
    current_url = str(url)
    current_kwargs = dict(kwargs)
    # 重定向必须由本函数逐跳控制，忽略调用方传入的 allow_redirects。
    current_kwargs.pop("allow_redirects", None)
    headers = dict(current_kwargs.pop("headers", None) or {})
    for _ in range(_MAX_REDIRECTS):
        ensure_public_network_target(current_url)
        request_kwargs = dict(current_kwargs)
        if headers:
            request_kwargs["headers"] = headers
        response = send(
            current_method,
            current_url,
            timeout=timeout,
            verify=verify,
            allow_redirects=False,
            **request_kwargs,
        )
        if not (response.is_redirect or response.is_permanent_redirect):
            return response
        location = response.headers.get("Location")
        response.close()
        if not location:
            raise ProviderError("Provider 重定向缺少 Location 头")
        next_url = urljoin(current_url, location)
        if urlsplit(next_url).netloc.lower() != urlsplit(current_url).netloc.lower():
            headers = {
                key: value
                for key, value in headers.items()
                if key.lower() not in ("authorization", "cookie")
            }
            current_kwargs.pop("auth", None)
        # 对齐 requests 的重定向语义：301/302/303 对非 GET/HEAD 改为 GET
        # 并丢弃请求体，307/308 保留方法与请求体。
        if response.status_code in (301, 302, 303) and current_method not in ("GET", "HEAD"):
            current_method = "GET"
            for key in ("json", "data", "files"):
                current_kwargs.pop(key, None)
            headers = {
                key: value
                for key, value in headers.items()
                if key.lower() not in ("content-length", "content-type", "transfer-encoding")
            }
        current_url = next_url
    raise ProviderError("Provider 重定向次数过多")


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
        # 运行时同样校验网络策略：直接改库写入的内网 base_url 不能绕过
        # provider_service._validate_config（配置保存入口）生效。
        validate_provider_base_url_network(config.base_url)
        self.config = config

    @property
    def supports_vision(self) -> bool:
        """是否支持图片输入（视觉质检用）。默认为否，按 Provider 实现覆盖。"""
        return False

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
        # 非安全方法（POST 等）默认不重试；仅以下两类「服务端明确未受理」的
        # 瞬时错误可安全重试：
        # - 429：明确限流，请求未被上游执行；
        # - 202 + 错误体：中转渠道明确报告提交未被接受（见下方判断）。
        # 500/502/503/504 等网关模糊失败对 POST 不自动重试：网关超时/断连时
        # 上游可能已经建立计费任务，盲目重发会造成重复计费；这类失败直接
        # 返回给上层，交由 Job 层的 interrupted/失败恢复语义处理。
        # requests.Timeout 同理保持不自动重试。显式配置
        # retry_unsafe_methods=True 的渠道维持全量重试（由渠道自担重复
        # 提交风险）。GET/HEAD/OPTIONS 等安全方法幂等，维持现状照常重试。
        transient_statuses = {202, 429}
        for attempt in range(attempts):
            try:
                response = request_with_redirect_guard(
                    method,
                    url,
                    timeout=self.config.timeout_sec,
                    # verify_ssl 仅供自建/中转服务使用自签证书时关闭；
                    # 关闭后不校验 TLS 证书，存在中间人风险，不要对公网服务关闭。
                    verify=bool(options.get("verify_ssl", True)),
                    **kwargs,
                )
                response.raise_for_status()
                # 中转渠道偶发 202 + error JSON（如“上游服务暂时不可用”），
                # 按瞬时错误处理，允许重试而不是当成成功响应。
                # 注意只认响应体里明确携带的 error/message 文本；没有错误体的
                # 正常 202（异步接口的 Accepted 接受形态）必须放行，否则
                # multipart/JSON 提交会被整单重发、产生重复付费任务。
                if response.status_code == 202 and self._provider_error_text(response):
                    raise requests.HTTPError(response=response)
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
                # SSLError 走这里，不再单独放行：requests 默认 stream=False，
                # 响应读取期的 SSL 断连（UNEXPECTED_EOF_WHILE_READING 等）同样
                # 从 requests.request() 抛 SSLError，而该错误串在握手期和读取期
                # 都会出现，无法据此判断请求体是否已送达上游。此时上游可能已经
                # 建立计费任务，自动重发就是重复付费，因此与 5xx/Timeout 同口径：
                # 非安全方法不自动重试，交给 Job 层的 interrupted/失败恢复语义。
                # 确实需要吸收中转线路 TLS 抖动的渠道显式配 retry_unsafe_methods=True。
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
    def _provider_error_text(response: requests.Response | None) -> str:
        """仅当响应体明确携带 error/message 错误信息时才返回该文本，否则空串。"""
        if response is None:
            return ""
        try:
            payload = response.json()
        except requests.JSONDecodeError:
            return ""
        if not isinstance(payload, Mapping):
            return ""
        error = payload.get("error")
        provider_message = ""
        if isinstance(error, Mapping):
            provider_message = str(error.get("message") or error.get("detail") or "")
        elif isinstance(error, str):
            provider_message = error
        if not provider_message:
            provider_message = str(payload.get("message") or payload.get("detail") or "")
        return provider_message

    @classmethod
    def _http_failure_message(cls, response: requests.Response | None) -> str:
        if response is None:
            return ""
        provider_message = sanitize_error_message(cls._provider_error_text(response))
        prefix = f"Provider 请求失败（HTTP {response.status_code}）"
        if not provider_message:
            return prefix
        return f"{prefix}：{provider_message}"

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
            encoded = str(result.content_base64)
            if encoded.startswith("data:") and "," in encoded:
                header, encoded = encoded.split(",", 1)
                mime = header[5:].split(";", 1)[0].strip().lower()
                if mime and not any(mime.startswith(prefix) for prefix in _ALLOWED_DATA_URL_MIMES):
                    raise ProviderError(f"Provider 返回的 data URL 媒体类型不受支持：{mime}")
            # 编码长度预检：解码前就拒绝超限数据，避免恶意/异常结果耗尽内存。
            if len(encoded) > _MAX_BASE64_ENCODED_CHARS:
                raise ProviderError("Provider 返回的 Base64 文件超过大小上限")
            try:
                content = base64.b64decode(encoded, validate=True)
            except ValueError as exc:
                raise ProviderError("Provider 返回的 Base64 文件无效") from exc
            if len(content) > _MAX_BASE64_DECODED_BYTES:
                raise ProviderError("Provider 返回的 Base64 文件超过大小上限")
            return await asyncio.to_thread(atomic_write_bytes, target, content)
        local_file = result.data.get("local_file")
        if local_file:
            # 契约：只有本机 Provider（mock）会返回 local_file，路径由 Provider
            # 自行生成；远程 Provider 实现不得返回该字段，否则等同于任意文件读取。
            # B-L4 纵深防御：下载前校验该路径确实位于平台工作区内，
            # 阻止伪造的 Provider 结果把工作区外的任意本机文件复制进媒体目录。
            resolved_local = Path(str(local_file)).resolve()
            if not resolved_local.is_relative_to(file_service.workspace_dir):
                raise ProviderError("Provider 返回的本地文件超出平台工作区范围")
            from app.services.file_service import atomic_copy

            return await asyncio.to_thread(atomic_copy, resolved_local, target)
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
