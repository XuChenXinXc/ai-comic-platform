from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any

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

    async def submit(
        self,
        *,
        prompt: str,
        negative_prompt: str,
        reference_files: list[Path],
        params: dict[str, Any],
    ) -> ProviderSubmission:
        options = self.config.params
        self._validate_request(negative_prompt, reference_files, params, options)
        payload = dict(options.get("payload", {}))
        payload.update(options.get("default_params", {}))
        payload.update(params)
        payload[str(options.get("prompt_field", "prompt"))] = prompt
        if negative_prompt or options.get("send_empty_negative_prompt", False):
            payload[str(options.get("negative_prompt_field", "negative_prompt"))] = negative_prompt
        if reference_files:
            payload[str(options.get("references_field", "reference_images"))] = [
                self._encode_reference(path, options) for path in reference_files
            ]
        if self.config.model:
            payload.setdefault(str(options.get("model_field", "model")), self.config.model)
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
        status = self._status(response, default="completed" if mode == "sync" else "")
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

    async def poll(self, task_id: str) -> ProviderPollResult:
        options = self.config.params
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
            status=self._status(response),
            result_url=result["result_url"],
            content_base64=result["content_base64"],
            data={"raw": response},
            error=str(extract_path(response, str(options.get("error_path", "error")), "") or ""),
        )

    async def download(self, result: ProviderPollResult, target: Path) -> Path:
        return await self._download_poll_result(result, target)

    def _status(self, response: dict[str, Any], default: str = "") -> str:
        options = self.config.params
        raw = extract_path(response, str(options.get("status_path", "status")), default)
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
