from __future__ import annotations

import asyncio
import base64
import mimetypes
from pathlib import Path
from typing import Any

import requests

from app.schemas import ProviderConfig, ProviderPollResult, ProviderSubmission

from ..base import (
    HttpProviderBase,
    ProviderError,
    extract_path,
    normalize_provider_status,
)


class HttpVideoProvider(HttpProviderBase):
    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)

    async def prepare_input_image(self, local_file: Path) -> dict[str, Any]:
        if not local_file.is_file() or local_file.stat().st_size <= 0:
            raise ProviderError("视频输入关键帧不存在或为空")
        options = self.config.params
        mode = str(options.get("input_image_mode", "base64"))
        if mode == "base64":
            encoded = await asyncio.to_thread(local_file.read_bytes)
            value = base64.b64encode(encoded).decode("ascii")
            if options.get("input_image_data_url", True):
                media_type = mimetypes.guess_type(local_file.name)[0] or "image/png"
                value = f"data:{media_type};base64,{value}"
            return {"value": value, "mode": "base64"}
        if mode == "upload":
            return await self._upload_input_image(local_file, options)
        raise ProviderError(f"不支持的视频关键帧输入模式：{mode}")

    async def submit(
        self,
        *,
        prompt: str,
        input_image: dict[str, Any],
        duration_sec: float,
        params: dict[str, Any],
    ) -> ProviderSubmission:
        if duration_sec <= 0:
            raise ProviderError("视频时长必须大于 0")
        options = self.config.params
        self._validate_duration(duration_sec, options)
        payload = dict(options.get("payload", {}))
        payload.update(options.get("default_params", {}))
        payload.update(params)
        payload[str(options.get("prompt_field", "prompt"))] = prompt
        payload[str(options.get("input_image_field", "input_image"))] = input_image.get("value")
        payload[str(options.get("duration_field", "duration"))] = duration_sec
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
        mode = str(options.get("mode", "async"))
        status = self._status(response, default="completed" if mode == "sync" else "")
        result = self._result_values(response, options)
        if mode == "async" and not task_id:
            raise ProviderError("异步视频接口响应缺少 task_id")
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

    async def _upload_input_image(
        self,
        local_file: Path,
        options: dict[str, Any],
    ) -> dict[str, Any]:
        upload_path = str(options.get("upload_path", ""))
        if not upload_path:
            raise ProviderError("input_image_mode=upload 时必须配置 upload_path")

        def upload() -> dict[str, Any]:
            headers = self._headers(options)
            field = str(options.get("upload_file_field", "file"))
            with local_file.open("rb") as source:
                response = self._request(
                    str(options.get("upload_method", "POST")),
                    self._url(upload_path),
                    options=options,
                    headers=headers,
                    files={
                        field: (
                            local_file.name,
                            source,
                            mimetypes.guess_type(local_file.name)[0] or "image/png",
                        )
                    },
                    data=dict(options.get("upload_data", {})),
                )
            try:
                payload = response.json()
            except requests.JSONDecodeError as exc:
                raise ProviderError("关键帧上传接口返回的不是 JSON") from exc
            value = extract_path(
                payload,
                str(options.get("upload_result_path", "file_id")),
            )
            if not value:
                raise ProviderError("关键帧上传响应缺少结果标识")
            return {"value": value, "mode": "upload", "raw": payload}

        return await asyncio.to_thread(upload)

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
    def _validate_duration(duration_sec: float, options: dict[str, Any]) -> None:
        minimum = options.get("min_duration_sec")
        maximum = options.get("max_duration_sec")
        if minimum is not None and duration_sec < float(minimum):
            raise ProviderError(f"视频时长不能小于 {minimum} 秒")
        if maximum is not None and duration_sec > float(maximum):
            raise ProviderError(f"视频时长不能大于 {maximum} 秒")
        if options.get("duration_integer_only") and not float(duration_sec).is_integer():
            raise ProviderError("当前视频 Provider 只接受整数秒时长")
