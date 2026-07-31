from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

from app.schemas import ProviderConfig, ProviderPollResult, ProviderSubmission

from ..base import (
    HttpProviderBase,
    ProviderError,
    extract_path,
    normalize_provider_status,
)


class HttpImageProvider(HttpProviderBase):
    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)

    def _options(self) -> dict[str, Any]:
        options = dict(self.config.params)
        if self.config.model.strip().lower() == "gpt-image-2":
            options.setdefault("retry_unsafe_methods", False)
            options.setdefault("send_negative_prompt", False)
            options.setdefault("max_reference_files", 16)
            if self._uses_apib_protocol(options):
                options.setdefault("submit_path", "images/generations")
                options.setdefault("poll_path", "tasks/{task_id}")
                options.setdefault("mode", "async")
                options.setdefault("task_id_path", "data.0.task_id")
                options.setdefault("submit_status_path", "data.0.status")
                options.setdefault("status_path", "data.status")
                options.setdefault("result_url_path", "data.result.images.0.url.0")
                options.setdefault("error_path", "data.error")
                options.setdefault("references_field", "image_urls")
                options.setdefault("reference_data_url", True)
                options["default_params"] = {
                    "resolution": "4k",
                    "quality": "high",
                    "n": 1,
                    **dict(options.get("default_params", {})),
                }
            else:
                options.setdefault("submit_path", "images/generations")
                options.setdefault("edit_path", "images/edits")
                options.setdefault("reference_mode", "multipart")
                options.setdefault("reference_file_field", "image[]")
                options.setdefault("mode", "sync")
                options.setdefault("result_url_path", "data.0.url")
                options.setdefault("result_base64_path", "data.0.b64_json")
                options["default_params"] = {
                    "quality": "high",
                    **dict(options.get("default_params", {})),
                }
        return options

    def _uses_apib_protocol(self, options: dict[str, Any]) -> bool:
        protocol = str(options.get("protocol", "")).strip().lower()
        if protocol:
            return protocol in {"apib", "apimart"}
        hostname = (urlsplit(self.config.base_url).hostname or "").lower()
        return hostname in {"api.apib.ai", "api.apimart.ai"}

    async def submit(
        self,
        *,
        prompt: str,
        negative_prompt: str,
        reference_files: list[Path],
        params: dict[str, Any],
    ) -> ProviderSubmission:
        options = self._options()
        self._validate_request(negative_prompt, reference_files, params, options)
        payload = dict(options.get("payload", {}))
        payload.update(options.get("default_params", {}))
        payload.update(params)
        payload[str(options.get("prompt_field", "prompt"))] = prompt
        if options.get("send_negative_prompt", True) and (
            negative_prompt or options.get("send_empty_negative_prompt", False)
        ):
            payload[str(options.get("negative_prompt_field", "negative_prompt"))] = negative_prompt
        multipart_references = bool(
            reference_files and str(options.get("reference_mode", "")).lower() == "multipart"
        )
        if (
            not multipart_references
            and self.config.model.strip().lower() == "gpt-image-2"
            and not self._uses_apib_protocol(options)
        ):
            payload.setdefault("response_format", "url")
        if multipart_references:
            payload.setdefault("n", 1)
        if reference_files and not multipart_references:
            payload[str(options.get("references_field", "reference_images"))] = [
                self._encode_reference(path, options) for path in reference_files
            ]
        if self.config.model:
            payload.setdefault(str(options.get("model_field", "model")), self.config.model)
        if multipart_references:
            response = await self._request_multipart_json(
                payload,
                reference_files,
                options,
            )
        else:
            headers = self._headers(options)
            headers.setdefault("Content-Type", "application/json")
            response = await self._request_json(
                str(options.get("submit_method", "POST")),
                str(options.get("submit_path", "")),
                options=options,
                headers=headers,
                json=payload,
            )
        task_id = str(extract_path(response, str(options.get("task_id_path", "task_id")), "") or "")
        mode = str(options.get("mode", "sync"))
        status = self._status(
            response,
            default="completed" if mode == "sync" else "",
            options=options,
            path_key="submit_status_path",
        )
        result = self._result_values(response, options)
        if mode == "async" and not task_id:
            raise ProviderError("异步图片接口响应缺少 task_id")
        return ProviderSubmission(
            task_id=task_id,
            status=status,
            result={**result, "raw": response},
            message=str(
                extract_path(
                    response,
                    str(options.get("message_path", "message")),
                    "",
                )
                or ""
            ),
        )

    async def _request_multipart_json(
        self,
        payload: dict[str, Any],
        reference_files: list[Path],
        options: dict[str, Any],
    ) -> dict[str, Any]:
        def request() -> dict[str, Any]:
            files = []
            field = str(options.get("reference_file_field", "image[]"))
            for path in reference_files:
                if not path.is_file() or path.stat().st_size <= 0:
                    raise ProviderError(f"参考图不存在或为空：{path}")
                media_type = mimetypes.guess_type(path.name)[0] or "image/png"
                files.append((field, (path.name, path.read_bytes(), media_type)))
            headers = {
                name: value
                for name, value in self._headers(options).items()
                if name.lower() != "content-type"
            }
            response = self._request(
                str(options.get("submit_method", "POST")),
                self._url(str(options.get("edit_path", "images/edits"))),
                options=options,
                headers=headers,
                data={
                    str(name): self._multipart_value(value)
                    for name, value in payload.items()
                    if value is not None
                },
                files=files,
            )
            try:
                result = response.json()
            except requests.JSONDecodeError as exc:
                raise ProviderError("Provider 返回的不是 JSON") from exc
            if not isinstance(result, dict):
                raise ProviderError("Provider JSON 响应必须是对象")
            return result

        return await asyncio.to_thread(request)

    @staticmethod
    def _multipart_value(value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    async def poll(self, task_id: str) -> ProviderPollResult:
        options = self._options()
        path = str(options.get("poll_path", "tasks/{task_id}")).format(task_id=task_id)
        method = str(options.get("poll_method", "GET"))
        kwargs: dict[str, Any] = {"headers": self._headers(options)}
        if method.upper() != "GET":
            payload = dict(options.get("poll_payload", {}))
            payload[str(options.get("poll_task_id_field", "task_id"))] = task_id
            kwargs["json"] = payload
        response = await self._request_json(
            method,
            path,
            options=options,
            **kwargs,
        )
        result = self._result_values(response, options)
        return ProviderPollResult(
            task_id=task_id,
            status=self._status(response, options=options),
            result_url=result["result_url"],
            content_base64=result["content_base64"],
            data={"raw": response},
            error=str(extract_path(response, str(options.get("error_path", "error")), "") or ""),
        )

    async def download(self, result: ProviderPollResult, target: Path) -> Path:
        return await self._download_poll_result(result, target)

    def _status(
        self,
        response: dict[str, Any],
        default: str = "",
        *,
        options: dict[str, Any] | None = None,
        path_key: str = "status_path",
    ) -> str:
        options = options or self._options()
        status_path = options.get(path_key, options.get("status_path", "status"))
        raw = extract_path(response, str(status_path), default)
        if not raw and default:
            return default
        return normalize_provider_status(
            raw,
            completed_values=list(options.get("completed_values", [])) or None,
            failed_values=list(options.get("failed_values", [])) or None,
        )

    @staticmethod
    def _result_values(
        response: dict[str, Any],
        options: dict[str, Any],
    ) -> dict[str, str]:
        return {
            "result_url": str(
                extract_path(
                    response,
                    str(options.get("result_url_path", "result.url")),
                    "",
                )
                or ""
            ),
            "content_base64": str(
                extract_path(
                    response,
                    str(options.get("result_base64_path", "result.base64")),
                    "",
                )
                or ""
            ),
        }

    @staticmethod
    def _encode_reference(path: Path, options: dict[str, Any]) -> str:
        if not path.is_file() or path.stat().st_size <= 0:
            raise ProviderError(f"参考图不存在或为空：{path}")
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        if options.get("reference_data_url", False):
            media_type = mimetypes.guess_type(path.name)[0] or "image/png"
            return f"data:{media_type};base64,{encoded}"
        return encoded

    @staticmethod
    def _validate_request(
        negative_prompt: str,
        reference_files: list[Path],
        params: dict[str, Any],
        options: dict[str, Any],
    ) -> None:
        maximum = options.get("max_reference_files")
        if maximum is not None and len(reference_files) > int(maximum):
            raise ProviderError(f"参考图数量不能超过 {maximum}")
        if negative_prompt and options.get("supports_negative_prompt") is False:
            raise ProviderError("当前图片 Provider 不支持负向提示词")
        supported_sizes = options.get("supported_sizes")
        size = params.get(str(options.get("size_field", "size")))
        if supported_sizes and size and size not in supported_sizes:
            raise ProviderError(f"当前图片 Provider 不支持尺寸：{size}")
