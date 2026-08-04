from __future__ import annotations

import asyncio
import base64
import math
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

    def _options(self) -> dict[str, Any]:
        options = dict(self.config.params)
        model = self.config.model.strip().lower()
        if model in {
            "grok-imagine-1.5-video-apimart",
            "grok-imagine-1.5-video-ext",
        }:
            options.setdefault("retry_unsafe_methods", False)
            options.setdefault("submit_path", "videos/generations")
            options.setdefault("poll_path", "tasks/{task_id}")
            options.setdefault("task_id_path", "data.0.task_id")
            options.setdefault("submit_status_path", "data.0.status")
            options.setdefault("status_path", "data.status")
            options.setdefault("result_url_path", "data.result.videos.0.url.0")
            options.setdefault("error_path", "data.error")
            options.setdefault("input_image_field", "image_urls")
            options.setdefault("input_image_list", True)
            options.setdefault("input_image_mode", "upload")
            options.setdefault("upload_path", "uploads/images")
            options.setdefault("upload_file_field", "file")
            options.setdefault("upload_result_path", "url")
            options.setdefault("max_reference_files", 7)
            options.setdefault("min_duration_sec", 6)
            options.setdefault("max_duration_sec", 30)
            options.setdefault("duration_integer_only", True)
            options.setdefault("duration_round_up", True)
            options["default_params"] = {
                "size": "16:9",
                "quality": "720p",
                **dict(options.get("default_params", {})),
            }
        elif model in {
            "doubao-seedance-2.0",
            "doubao-seedance-2.0-fast",
            "doubao-seedance-2.0-mini",
        }:
            # doubao-seedance-2.0 系列：分镜填多少秒就传多少秒（4~15 秒整数），
            # 不做向上取整或最低时长抬升；生成音频默认开启。
            options.setdefault("retry_unsafe_methods", False)
            options.setdefault("submit_path", "videos/generations")
            options.setdefault("poll_path", "tasks/{task_id}")
            options.setdefault("task_id_path", "data.0.task_id")
            options.setdefault("submit_status_path", "data.0.status")
            options.setdefault("status_path", "data.status")
            options.setdefault("result_url_path", "data.result.videos.0.url.0")
            options.setdefault("error_path", "data.error")
            options.setdefault("input_image_field", "image_urls")
            options.setdefault("input_image_list", True)
            options.setdefault("input_image_mode", "upload")
            options.setdefault("upload_path", "uploads/images")
            options.setdefault("upload_file_field", "file")
            options.setdefault("upload_result_path", "url")
            options.setdefault("max_reference_files", 9)
            options.setdefault("min_duration_sec", 4)
            options.setdefault("max_duration_sec", 15)
            options.setdefault("duration_integer_only", True)
            options.setdefault("duration_round_up", False)
            options["default_params"] = {
                "size": "16:9",
                "resolution": "720p",
                "generate_audio": True,
                **dict(options.get("default_params", {})),
            }
        return options

    @property
    def supports_multiple_input_images(self) -> bool:
        return bool(self._options().get("input_image_list", False))

    async def prepare_input_image(self, local_file: Path) -> dict[str, Any]:
        if not local_file.is_file() or local_file.stat().st_size <= 0:
            raise ProviderError("视频输入关键帧不存在或为空")
        options = self._options()
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

    async def prepare_input_images(self, local_files: list[Path]) -> dict[str, Any]:
        if not local_files:
            raise ProviderError("视频输入参考图不能为空")
        prepared = [await self.prepare_input_image(local_file) for local_file in local_files]
        return {
            "values": [item["value"] for item in prepared],
            "mode": prepared[0]["mode"],
        }

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
        options = self._options()
        effective_duration: float | int = duration_sec
        round_up = bool(options.get("duration_round_up"))
        # 显式配置 duration_round_up=False 的模型（如 doubao-seedance）分镜多少秒
        # 就传多少秒，超范围由 _validate_duration 报错；缺省或 True 时保留旧行为：
        # 向上取整并把时长抬到供应商下限（grok 等模型）。
        suppress_floor = options.get("duration_round_up") is False
        if round_up:
            effective_duration = math.ceil(duration_sec)
        minimum_duration = options.get("min_duration_sec")
        if minimum_duration is not None and not suppress_floor:
            effective_duration = max(effective_duration, float(minimum_duration))
        self._validate_duration(effective_duration, options)
        if options.get("duration_integer_only"):
            effective_duration = int(effective_duration)
        payload = dict(options.get("payload", {}))
        payload.update(options.get("default_params", {}))
        payload.update(params)
        payload[str(options.get("prompt_field", "prompt"))] = prompt
        input_image_values = input_image.get("values")
        if not isinstance(input_image_values, list) or not input_image_values:
            input_image_values = [input_image.get("value")]
        input_image_values = [item for item in input_image_values if item]
        if not input_image_values:
            raise ProviderError("视频输入关键帧不能为空")
        maximum = options.get("max_reference_files")
        if maximum is not None and len(input_image_values) > int(maximum):
            raise ProviderError(f"视频参考图数量不能超过 {maximum}")
        input_image_value: Any = (
            input_image_values if options.get("input_image_list", False) else input_image_values[0]
        )
        payload[str(options.get("input_image_field", "input_image"))] = input_image_value
        payload[str(options.get("duration_field", "duration"))] = effective_duration
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
        status = self._status(
            response,
            default="completed" if mode == "sync" else "",
            options=options,
            path_key="submit_status_path",
        )
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
            # 内存 bytes 可被 requests 在每次重试时重新读取（文件句柄在重试时会读到 EOF 发空文件）
            content = local_file.read_bytes()
            response = self._request(
                str(options.get("upload_method", "POST")),
                self._url(upload_path),
                options=options,
                headers=headers,
                files={
                    field: (
                        local_file.name,
                        content,
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
    def _validate_duration(duration_sec: float, options: dict[str, Any]) -> None:
        minimum = options.get("min_duration_sec")
        maximum = options.get("max_duration_sec")
        if minimum is not None and duration_sec < float(minimum):
            raise ProviderError(f"视频时长不能小于 {minimum} 秒")
        if maximum is not None and duration_sec > float(maximum):
            raise ProviderError(f"视频时长不能大于 {maximum} 秒")
        if options.get("duration_integer_only") and not float(duration_sec).is_integer():
            raise ProviderError("当前视频 Provider 只接受整数秒时长")
