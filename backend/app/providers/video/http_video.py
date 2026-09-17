from __future__ import annotations

import asyncio
import base64
import json
import math
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

from app.schemas import ProviderConfig, ProviderPollResult, ProviderSubmission

from ..base import (
    HttpProviderBase,
    ProviderConfigurationError,
    ProviderError,
    compress_reference_image,
    extract_path,
    get_imgbb_api_key,
    normalize_provider_status,
    reference_jpeg_part,
)

# H3（zhongbiao ref2va）视频接口的响应结构以 H3Studio 官方前端消费逻辑为准：
# 提交/轮询返回 { job: {...} } 包装，任务字段为 job.id / job.status / job.error /
# job.resultUrl；状态值为 queued / running / processing / succeeded / failed /
# cancelled / timed_out。页面 params 里显式配置 task_id_path/status_path/
# result_url_path/error_path 即可完全覆盖默认行为。
_H3_TASK_ID_CANDIDATE_PATHS = [
    "job.id",
    "job_id",
    "data.job_id",
    "task_id",
    "data.task_id",
    "id",
    "data.id",
]
_H3_STATUS_CANDIDATE_PATHS = ["job.status", "status", "data.status", "state", "data.state"]
_H3_RESULT_URL_CANDIDATE_PATHS = [
    # 结果直链优先绝对 URL；官方相对路径形如 /api/videos/{id}/content，
    # 直接拼接到 base_url 会产生 /api/api 双前缀，因此不进候选，
    # 由协议固定的 download_path 端点回填（同源自动附带 Bearer 鉴权）。
    "url",
    "data.url",
    "video_url",
    "data.video_url",
    "result.url",
    "output.url",
]
_H3_ERROR_CANDIDATE_PATHS = ["job.error", "error.message", "fail_reason", "failure_reason"]

# 协议控制字段：只用于驱动 Provider 行为，不进入提交负载（JSON body 与 multipart 表单共用）。
_VIDEO_PROTOCOL_CONTROL_FIELDS = [
    "protocol",
    "submit_path",
    "submit_method",
    "submit_transport",
    "poll_path",
    "poll_method",
    "poll_payload",
    "poll_task_id_field",
    "task_id_path",
    "task_id_candidate_paths",
    "submit_status_path",
    "status_path",
    "status_candidate_paths",
    "result_url_path",
    "result_url_candidate_paths",
    "result_base64_path",
    "error_path",
    "error_candidate_paths",
    "message_path",
    "prompt_field",
    "model_field",
    "duration_field",
    "input_image_field",
    "input_image_list",
    "input_image_mode",
    "input_image_data_url",
    "max_reference_files",
    "min_duration_sec",
    "max_duration_sec",
    "duration_integer_only",
    "duration_round_up",
    "force_duration_sec",
    "billing_duration_field",
    "billing_duration_value",
    "completed_values",
    "failed_values",
    "default_params",
    "payload",
    "headers",
    "verify_ssl",
    "retry_unsafe_methods",
    "test_path",
    "test_method",
    "upload_path",
    "upload_method",
    "upload_file_field",
    "upload_result_path",
    "upload_data",
    "ignored_payload_fields",
]


class HttpVideoProvider(HttpProviderBase):
    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)

    def _options(self) -> dict[str, Any]:
        options = dict(self.config.params)
        model = self.config.model.strip().lower()
        # 未识别的视频模型也要有通用 OpenAI 兼容提交路径，避免 submit_path
        # 为空时把请求直接打到 base_url（网关返回 Invalid URL (POST /v1)）。
        options.setdefault("submit_path", "videos/generations")
        # 显式声明的协议必须优先于按模型名的兜底匹配：
        # 用户在页面上写了 protocol=h3 时，哪怕 model 名撞上其他厂商命名也按 H3 走。
        if self._uses_h3_video_protocol(options):
            # H3（zhongbiao ref2va）视频接口：POST {base_url}/videos 用
            # multipart/form-data 直接携带参考文件，无独立上传步骤；轮询
            # GET videos/{task_id}；完成后从响应里提取不到结果直链时，走同源相对
            # 路径 videos/{task_id}/content 下载（自动附带 Bearer 鉴权）。
            # mode/resolution/steps/aspectRatio 等固定参数在 Provider params 的
            # default_params 里配置；官方示例未给出时长边界，分镜多少秒就传多少秒
            # （整数），需要约束时用 min/max_duration_sec。
            options["submit_path"] = "videos"
            options.setdefault("poll_path", "videos/{task_id}")
            # 完成后取结果的下载端点：与 poll_path 同款 {task_id} 模板，页面可覆盖。
            # 仅配置了该选项的协议才会做「无结果直链 → 下载端点」的兜底。
            options.setdefault("download_path", "videos/{task_id}/content")
            options.setdefault("retry_unsafe_methods", False)
            # 线格式由协议决定，不允许页面改 submit_transport/input_image_mode
            # 组合出「JSON 提交但值是本地路径」「multipart 但值是 dataURL」等非法组合
            options["submit_transport"] = "multipart"
            options["input_image_list"] = True
            options["input_image_mode"] = "attach_file"
            options.setdefault("input_image_field", "referenceImages")
            # 与其余视频协议一致的数量上限，防止绑定大量素材时一次性发出巨型表单
            options.setdefault("max_reference_files", 9)
            options.setdefault("duration_integer_only", True)
            options.setdefault("duration_round_up", False)
            # 官方提交示例没有 model 字段，默认不随表单发送（可显式配置 model_field 开启）
            options.setdefault("model_field", "")
            # H3 只有异步 JOB 轮询一种形态：强制 mode=async，同时避免页面顶层误配的
            # "mode" 键改变 sync/async 判定（业务层 mode 字段走 default_params 下发）
            options["mode"] = "async"
            options.setdefault("task_id_candidate_paths", _H3_TASK_ID_CANDIDATE_PATHS)
            options.setdefault("status_candidate_paths", _H3_STATUS_CANDIDATE_PATHS)
            options.setdefault("result_url_candidate_paths", _H3_RESULT_URL_CANDIDATE_PATHS)
            options.setdefault("error_candidate_paths", _H3_ERROR_CANDIDATE_PATHS)
            # 官方状态包含 timed_out（计费失败终态），缺省会永远停在等待轮询
            options.setdefault(
                "failed_values",
                ["failed", "error", "cancelled", "timed_out"],
            )
            options.setdefault("ignored_payload_fields", _VIDEO_PROTOCOL_CONTROL_FIELDS)
        elif model in {
            "grok-imagine-1.5-video-apimart",
            "grok-imagine-1.5-video-ext",
        }:
            options.setdefault("retry_unsafe_methods", False)
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
            # 与其他视频协议共用同一份协议控制字段清单，防止 params 里的
            # 控制键（payload/headers/verify_ssl 等）漏进发往上游的 JSON 载荷。
            options.setdefault("ignored_payload_fields", _VIDEO_PROTOCOL_CONTROL_FIELDS)
            options["default_params"] = {
                "size": "16:9",
                "quality": "720p",
                **dict(options.get("default_params", {})),
            }
        elif model in {
            "doubao-seedance-2.0",
            "doubao-seedance-2.0-fast",
            "doubao-seedance-2.0-mini",
            "seedance-2.0",
            "seedance-2.0-fast",
            "seedance-2.0-mini",
        }:
            # APIB Seedance 2.0 系列：分镜填多少秒就传多少秒（4~15 秒整数），
            # 不做向上取整或最低时长抬升；生成音频默认开启。
            options.setdefault("retry_unsafe_methods", False)
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
            # Seedance 2.0 支持音频参考（≤3 段）驱动口型：当前配音音频经素材
            # 上传接口换 URL 后放入 audio_urls 数组；字段与路径均可被页面 params 覆盖。
            options.setdefault("audio_input_field", "audio_urls")
            options.setdefault("audio_upload_path", "uploads/audios")
            options.setdefault("audio_upload_file_field", "file")
            options.setdefault("audio_upload_result_path", "url")
            options.setdefault("max_audio_files", 3)
            # 与其他视频协议共用同一份协议控制字段清单，防止 params 里的
            # 控制键（payload/headers/verify_ssl 等）漏进发往上游的 JSON 载荷。
            options.setdefault("ignored_payload_fields", _VIDEO_PROTOCOL_CONTROL_FIELDS)
            options["default_params"] = {
                "size": "16:9",
                "resolution": "720p",
                "generate_audio": True,
                **dict(options.get("default_params", {})),
            }
        elif self._uses_tianyue_video_protocol(options):
            # 天悦官方视频接口：POST /v1/videos、GET /v1/videos/{task_id}。
            # 按次计费模型固定 duration=1，实际生成时长写到 video_duration；
            # 参考图最多 9 张，使用 images 数组；火山上游只认公网 URL，
            # 本地图先上传 ImgBB 换成 HTTPS URL。
            options["submit_path"] = "videos"
            options.setdefault("poll_path", "videos/{task_id}")
            options.setdefault("mode", "async")
            options.setdefault("task_id_path", "task_id")
            options.setdefault("submit_status_path", "status")
            options.setdefault("status_path", "status")
            options.setdefault("result_url_path", "url")
            options.setdefault("error_path", "error.message")
            options.setdefault("input_image_field", "images")
            options.setdefault("input_image_list", True)
            options.setdefault("input_image_mode", "public_url")
            options.setdefault("max_reference_files", 9)
            options.setdefault("min_duration_sec", 15)
            options.setdefault("max_duration_sec", 15)
            options.setdefault("duration_integer_only", True)
            options.setdefault("duration_round_up", True)
            options.setdefault("force_duration_sec", 15)
            options.setdefault("duration_field", "video_duration")
            options.setdefault("billing_duration_field", "duration")
            options.setdefault("billing_duration_value", 1)
            # 与其他视频协议共用中央清单；额外忽略 "mode"——它既是天悦的页面级
            # 参数又驱动本 Provider 的 sync/async 判定，绝不能透传给上游。
            options.setdefault(
                "ignored_payload_fields",
                [*_VIDEO_PROTOCOL_CONTROL_FIELDS, "mode"],
            )
            options["default_params"] = {
                "duration": 1,
                "aspect_ratio": "16:9",
                **dict(options.get("default_params", {})),
            }
        return options

    def _uses_tianyue_video_protocol(self, options: dict[str, Any]) -> bool:
        protocol = str(options.get("protocol", "")).strip().lower()
        if protocol:
            return protocol == "tianyue"
        hostname = (urlsplit(self.config.base_url).hostname or "").lower()
        return hostname in {"api.tianyue.xyz", "tianyue.xyz"}

    def _uses_h3_video_protocol(self, options: dict[str, Any]) -> bool:
        protocol = str(options.get("protocol", "")).strip().lower()
        if protocol:
            return protocol == "h3"
        hostname = (urlsplit(self.config.base_url).hostname or "").lower()
        return hostname == "h3.zhongbiao.cloud"

    @property
    def supports_multiple_input_images(self) -> bool:
        return bool(self._options().get("input_image_list", False))

    @property
    def supports_audio_input(self) -> bool:
        """是否支持音频参考输入（Seedance 2.0 等协议用 audio_urls 驱动口型）。"""
        return bool(self._options().get("audio_input_field"))

    @property
    def prefers_public_reference_urls(self) -> bool:
        """天悦 image_urls 优先使用公网 URL，缺失时由调用方回退 base64。"""
        return self._uses_tianyue_video_protocol(self._options())

    async def prepare_input_image(self, local_file: Path) -> dict[str, Any]:
        if not local_file.is_file() or local_file.stat().st_size <= 0:
            raise ProviderError("视频输入关键帧不存在或为空")
        options = self._options()
        mode = str(options.get("input_image_mode", "base64"))
        if mode == "base64":
            compressed = await asyncio.to_thread(compress_reference_image, local_file)
            value = base64.b64encode(compressed).decode("ascii")
            if options.get("input_image_data_url", True):
                value = f"data:image/jpeg;base64,{value}"
            return {"value": value, "mode": "base64"}
        if mode == "upload":
            return await self._upload_input_image(local_file, options)
        if mode == "public_url":
            return await self._upload_input_image_public(local_file)
        if mode == "attach_file":
            # H3 等 multipart 接口：参考文件本体在 submit 时并入表单，这里只透传本地路径
            return {"value": local_file, "mode": "attach_file"}
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
        audio_files: list[Path] | None = None,
    ) -> ProviderSubmission:
        if duration_sec <= 0:
            raise ProviderError("视频时长必须大于 0")
        options = self._options()
        forced_duration = options.get("force_duration_sec")
        if forced_duration is None:
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
        else:
            # 固定时长模型（如天悦 mini-15s）：忽略分镜秒数，全部按固定时长提交。
            effective_duration = float(forced_duration)
        self._validate_duration(effective_duration, options)
        # 上游部分接口的 duration 字段是 Go int；数据库取出的整数秒可能仍是 6.0，
        # JSON 序列化后会被上游区分为浮点数，因此整数值统一按 int 发送。
        if options.get("duration_integer_only") or float(effective_duration).is_integer():
            effective_duration = int(effective_duration)
        payload = dict(options.get("payload", {}))
        payload.update(options.get("default_params", {}))
        filtered_params = dict(params)
        for field in options.get("ignored_payload_fields", []):
            filtered_params.pop(str(field), None)
        payload.update(filtered_params)
        billing_field = options.get("billing_duration_field")
        if billing_field is not None:
            payload[str(billing_field)] = options.get("billing_duration_value")
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
        # H3 等 multipart 接口通过空串 model_field 关闭 model 字段发送
        model_field = str(options.get("model_field", "model"))
        if self.config.model and model_field:
            payload.setdefault(model_field, self.config.model)
        payload[str(options.get("duration_field", "duration"))] = effective_duration
        # 音频输入（对白口型同步）：支持音频参考的协议（Seedance 2.0）把当前配音
        # 音频上传为 audio_urls 数组；模型据音频驱动口型，而不是凭空表演说话。
        audio_field = str(options.get("audio_input_field", ""))
        if audio_files and audio_field:
            maximum_audio = options.get("max_audio_files")
            if maximum_audio is not None and len(audio_files) > int(maximum_audio):
                audio_files = audio_files[: int(maximum_audio)]
            payload[audio_field] = [
                (await self._upload_audio(path, options))["value"] for path in audio_files
            ]
        headers = self._headers(options)
        transport = str(options.get("submit_transport", "json"))
        if transport == "multipart":
            # multipart 提交：标量参数进 data、参考文件进 files；Content-Type 由
            # requests 自动生成（含 boundary），不能手动设置。页面 params.headers
            # 里误贴的 Content-Type 会压掉自动 boundary 头毁掉整个表单上传，
            # 与图片适配器一致剥离（其余自定义头保留）。
            form_headers = {
                key: value for key, value in headers.items() if key.lower() != "content-type"
            }
            response = await self._request_json(
                str(options.get("submit_method", "POST")),
                str(options.get("submit_path", "")),
                options=options,
                headers=form_headers,
                data=self._form_fields(payload),
                files=await self._build_reference_files(input_image_values, options),
            )
        else:
            input_image_value: Any = (
                input_image_values
                if options.get("input_image_list", False)
                else input_image_values[0]
            )
            payload[str(options.get("input_image_field", "input_image"))] = input_image_value
            headers.setdefault("Content-Type", "application/json")
            response = await self._request_json(
                str(options.get("submit_method", "POST")),
                str(options.get("submit_path", "")),
                options=options,
                headers=headers,
                json=payload,
            )
        task_id = str(
            self._extract_value(
                response,
                options,
                str(options.get("task_id_path", "task_id")),
                "task_id_candidate_paths",
            )
        )
        if str(options.get("submit_transport", "")) == "multipart" and task_id:
            # 任务 ID 会拼进轮询/下载 URL：提交期就校验一次，避免脏 ID 被持久化
            # 后在 poll/download 阶段才以更隐晦的方式报错。
            self._safe_task_segment(task_id)
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
        download_template = str(options.get("download_path", "")).strip("/")
        if status == "completed" and download_template and not result["result_url"]:
            # 提交即终态的任务不会再走 poll 步骤，必须在此补上下载端点，
            # 否则媒体版本进入下载阶段时拿不到任何下载地址。
            result["result_url"] = self._content_download_url(options, task_id)
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
        poll_template = str(options.get("poll_path", "tasks/{task_id}"))
        # B-L5：任务 ID 来自服务端字符串，拼进 URL 前统一做字符白名单过滤，
        # 杜绝 "../"、查询串等被带进同源请求路径；JSON 协议路径与 multipart
        # 路径同样处理，不再只覆盖 multipart 协议。
        path = poll_template.format(task_id=self._safe_task_segment(task_id))
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
        status = self._status(response, options=options)
        result = self._result_values(response, options)
        download_template = str(options.get("download_path", "")).strip("/")
        if status == "completed" and download_template and not result["result_url"]:
            # 完成后未返回结果直链时回退到配置的下载端点（绝对地址，同源附带 Bearer）。
            # 仅在终态填充，等待中的轮询不产生伪下载地址。
            result["result_url"] = self._content_download_url(options, task_id)
        error_raw = self._stringify_error(
            self._extract_value(
                response,
                options,
                str(options.get("error_path", "error")),
                "error_candidate_paths",
            )
        )
        return ProviderPollResult(
            task_id=task_id,
            status=status,
            result_url=result["result_url"],
            content_base64=result["content_base64"],
            data={"raw": response},
            error=error_raw,
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
            response = self._request(
                str(options.get("upload_method", "POST")),
                self._url(upload_path),
                options=options,
                headers=headers,
                files={field: reference_jpeg_part(local_file)},
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

    async def _upload_audio(
        self,
        local_file: Path,
        options: dict[str, Any],
    ) -> dict[str, Any]:
        """把配音音频文件上传到 Provider 素材接口，返回音频引用标识。

        音频不做图片式压缩，按原始文件上传；返回字段路径由
        audio_upload_result_path 控制（默认与图片一致的 url）。
        """
        upload_path = str(options.get("audio_upload_path", ""))
        if not upload_path:
            raise ProviderError("音频输入必须配置 audio_upload_path")
        if not local_file.is_file() or local_file.stat().st_size <= 0:
            raise ProviderError(f"配音音频文件不存在或为空：{local_file}")

        def upload() -> dict[str, Any]:
            headers = self._headers(options)
            field = str(options.get("audio_upload_file_field", "file"))
            filename = Path(re.sub(r"[^A-Za-z0-9_.-]", "_", local_file.name))
            with local_file.open("rb") as file_handle:
                content = file_handle.read()
            response = self._request(
                str(options.get("upload_method", "POST")),
                self._url(upload_path),
                options=options,
                headers=headers,
                files={field: (filename.name, content, "audio/mpeg")},
                data=dict(options.get("upload_data", {})),
            )
            try:
                payload = response.json()
            except requests.JSONDecodeError as exc:
                raise ProviderError("配音上传接口返回的不是 JSON") from exc
            value = extract_path(
                payload,
                str(options.get("audio_upload_result_path", "url")),
            )
            if not value:
                raise ProviderError("配音上传响应缺少音频标识")
            return {"value": value, "mode": "upload", "raw": payload}

        return await asyncio.to_thread(upload)

    async def _upload_input_image_public(self, local_file: Path) -> dict[str, Any]:
        """把本地参考图上传到 ImgBB，返回公网 HTTPS URL（天悦火山接口需要）。"""
        # B-L10：ImgBB Key 优先读运行时覆盖（数据库加密值），不改写 settings 单例。
        imgbb_api_key = get_imgbb_api_key()
        if not imgbb_api_key:
            raise ProviderConfigurationError(
                "未配置 ImgBB API Key（APP_IMGBB_API_KEY），无法生成公网参考图 URL"
            )

        def upload() -> dict[str, Any]:
            content = compress_reference_image(local_file)
            encoded = base64.b64encode(content).decode("ascii")
            try:
                response = requests.post(
                    "https://api.imgbb.com/1/upload",
                    params={"key": imgbb_api_key, "expiration": 600},
                    files={"image": (None, encoded)},
                    timeout=60,
                )
                response.raise_for_status()
            except requests.HTTPError as exc:
                clean_msg = re.sub(r"[?&]key=[^&\s'\"]+", "?key=[REDACTED]", str(exc))
                raise ProviderError(f"ImgBB 上传 HTTP 错误：{clean_msg}") from None
            try:
                payload = response.json()
            except requests.JSONDecodeError as exc:
                raise ProviderError("ImgBB 返回的不是 JSON") from exc
            url = extract_path(payload, "data.url")
            if not payload.get("success") or not url:
                detail = str(
                    extract_path(payload, "error.message", "")
                    or payload.get("error")
                    or payload
                )[:200]
                raise ProviderError(f"ImgBB 上传失败：{detail}")
            return {"value": str(url), "mode": "url"}

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
        raw = self._extract_value(response, options, str(status_path), "status_candidate_paths")
        if not raw and default:
            return default
        return normalize_provider_status(
            raw,
            completed_values=list(options.get("completed_values", [])) or None,
            failed_values=list(options.get("failed_values", [])) or None,
        )

    def _result_values(self, response: dict[str, Any], options: dict[str, Any]) -> dict[str, str]:
        return {
            "result_url": str(
                self._extract_value(
                    response,
                    options,
                    str(options.get("result_url_path", "result.url")),
                    "result_url_candidate_paths",
                )
            ),
            "content_base64": str(
                extract_path(
                    response,
                    str(options.get("result_base64_path", "result.base64")),
                    "",
                )
            ),
        }

    @staticmethod
    def _extract_value(
        response: dict[str, Any],
        options: Mapping[str, Any],
        primary_path: str,
        candidates_key: str,
    ) -> Any:
        """先按主路径提取（页面显式配置优先生效），缺失时再按候选路径兜底。"""
        value = extract_path(response, primary_path)
        if value not in (None, "", [], {}):
            return value
        candidates = options.get(candidates_key)
        if isinstance(candidates, list | tuple) and candidates:
            for path in candidates:
                value = extract_path(response, str(path))
                if value not in (None, "", [], {}):
                    return value
        return ""

    @staticmethod
    def _stringify_error(value: Any) -> str:
        """错误字段常见嵌套对象形态：优先取 message/detail 文本，避免把 dict repr 抛给用户。"""
        if isinstance(value, Mapping):
            for key in ("message", "detail", "description"):
                inner = value.get(key)
                if isinstance(inner, str) and inner.strip():
                    return inner
            try:
                return json.dumps(value, ensure_ascii=False)
            except TypeError:
                return str(value)
        if isinstance(value, list):
            joined = "；".join(
                HttpVideoProvider._stringify_error(item) for item in value if item is not None
            )
            return joined
        return "" if value is None else str(value)

    @staticmethod
    def _safe_task_segment(task_id: str) -> str:
        """任务 ID 拼进同源相对 URL 前做字符白名单过滤（服务端字符串不可信）。"""
        segment = re.sub(r"[^A-Za-z0-9_-]", "", task_id)
        if not segment:
            raise ProviderError("Provider 返回的任务 ID 不包含合法的 URL 字符")
        return segment

    def _content_download_url(self, options: dict[str, Any], task_id: str) -> str:
        segment = self._safe_task_segment(task_id)
        template = str(options.get("download_path", "")).strip("/")
        path = (
            template.format(task_id=segment)
            if template
            else f"{str(options.get('submit_path', 'videos')).strip('/')}/{segment}/content"
        )
        # 生成绝对地址持久化进 result_data：重试/恢复时不受 Provider base_url 变更影响；
        # 同源下载路径由 _download_poll_result 自动附带 Bearer 鉴权。
        return self._url(path)

    @staticmethod
    def _form_fields(payload: dict[str, Any]) -> dict[str, Any]:
        """multipart 表单的标量字段：布尔转小写字符串、嵌套结构转 JSON、None 剔除。"""
        fields: dict[str, Any] = {}
        for key, value in payload.items():
            if value is None:
                continue
            if isinstance(value, bool):
                fields[str(key)] = "true" if value else "false"
            elif isinstance(value, dict | list):
                fields[str(key)] = json.dumps(value, ensure_ascii=False)
            else:
                fields[str(key)] = value
        return fields

    async def _build_reference_files(
        self,
        input_image_values: list[Any],
        options: dict[str, Any],
    ) -> list[tuple[str, tuple[str, bytes, str]]]:
        """把本地参考图压成 JPEG bytes 组成 multipart files，bytes 可安全用于重试重发。"""
        field_name = str(options.get("input_image_field", "referenceImages"))

        def build() -> list[tuple[str, tuple[str, bytes, str]]]:
            parts: list[tuple[str, tuple[str, bytes, str]]] = []
            for item in input_image_values:
                source = item if isinstance(item, Path) else Path(str(item))
                if not source.is_file():
                    # prepare 与 submit 之间文件被删、或页面误配出非本地路径时，
                    # 给出受控报错而不是把超长 dataURL/原始异常一起暴露出去。
                    raise ProviderError(f"视频参考文件不存在或不是本地文件：{str(source)[:80]}")
                parts.append((field_name, reference_jpeg_part(source)))
            return parts

        return await asyncio.to_thread(build)

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
