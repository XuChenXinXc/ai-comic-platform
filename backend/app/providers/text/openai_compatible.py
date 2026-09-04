from __future__ import annotations

import asyncio
import json
from types import UnionType
from typing import Annotated, Any, Union, get_args, get_origin

from pydantic import BaseModel, ValidationError

from app.schemas import ProviderConfig

from ..base import HttpProviderBase, ProviderError


class OpenAICompatibleTextProvider(HttpProviderBase):
    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)

    async def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: type[BaseModel],
        params: dict[str, Any],
    ) -> BaseModel:
        plain_text_field = getattr(schema, "plain_text_output_field", None)
        if isinstance(plain_text_field, str):
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
            repair_instruction = (
                "上一次输出无法通过校验。请只返回修复后的一句纯文本，"
                "不要输出 JSON、Markdown、标题或解释。"
            )
        else:
            plain_text_field = None
            messages = [
                {
                    "role": "system",
                    "content": (
                        f"{system_prompt}\n\n"
                        "只返回一个符合下列 JSON Schema 的 JSON 对象，不要输出 Markdown：\n"
                        f"{json.dumps(schema.model_json_schema(), ensure_ascii=False)}"
                    ),
                },
                {"role": "user", "content": user_prompt},
            ]
            repair_instruction = "上一次输出无法通过校验。请只返回修复后的完整 JSON 对象。"
        raw = await self._complete(
            messages,
            schema,
            params,
            json_response=plain_text_field is None,
        )
        try:
            return self._validate_output(schema, raw, plain_text_field)
        except (ValidationError, ValueError, json.JSONDecodeError) as first_error:
            repair_messages = [
                *messages,
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": f"{repair_instruction}\n校验错误：{first_error}",
                },
            ]
            repaired = await self._complete(
                repair_messages,
                schema,
                params,
                json_response=plain_text_field is None,
            )
            try:
                return self._validate_output(schema, repaired, plain_text_field)
            except (ValidationError, ValueError, json.JSONDecodeError) as exc:
                summary = repaired.strip().replace("\n", " ")[:500]
                output_type = "纯文本" if plain_text_field else "结构化"
                raise ProviderError(
                    f"文字 Provider 的{output_type}输出修复失败：{exc}；响应摘要：{summary}"
                ) from exc

    async def _complete(
        self,
        messages: list[dict[str, str]],
        schema: type[BaseModel],
        params: dict[str, Any],
        *,
        json_response: bool = True,
    ) -> str:
        options = {**self.config.params, **params}
        chat_path = str(options.pop("chat_path", "chat/completions"))
        model = str(options.pop("model", self.config.model))
        headers = self._headers(options)
        headers.setdefault("Content-Type", "application/json")
        response_format = options.pop("response_format", {"type": "json_object"})
        # 请求行为控制键不进请求体，但要保留给 _request：
        # verify_ssl=False 必须继续生效（此前被 pop 后恒 True，自签证书渠道
        # 无法关闭校验）；retry_unsafe_methods 同理。
        control_options: dict[str, Any] = {}
        for control_key in ("headers", "verify_ssl", "retry_unsafe_methods"):
            if control_key in options:
                control_options[control_key] = options.pop(control_key)
        payload: dict[str, Any] = {"model": model, "messages": messages, **options}
        if json_response and response_format:
            payload["response_format"] = response_format
        # 推理强度默认 high，但允许 provider 配置或任务 params 覆盖；
        # 大输出结构化任务（如分镜）用 medium/low 可大幅缩短单次调用耗时。
        payload["reasoning_effort"] = options.pop("reasoning_effort", "high")
        attempts = self.config.max_retries + 1
        last_reason = ""
        for attempt in range(attempts):
            response = await self._request_json(
                "POST",
                chat_path,
                options=control_options,
                headers=headers,
                json=payload,
            )
            extraction_error = ""
            try:
                content = self._extract_content(response)
            except ProviderError as exc:
                extraction_error = str(exc)
                content = ""
            if content.strip():
                return content
            finish_reason = self._finish_reason(response)
            last_reason = extraction_error or (
                f"返回空内容（finish_reason={finish_reason or '未知'}）"
            )
            if extraction_error and finish_reason:
                last_reason += f"（finish_reason={finish_reason}）"
            # 部分推理模型在长 prompt + json_object 组合下会返回空 content；
            # 后续尝试去掉 response_format（提示词本身已要求 JSON 输出）
            if json_response and payload.get("response_format"):
                payload.pop("response_format", None)
                last_reason += "；已尝试去掉 response_format"
            if attempt < attempts - 1:
                await asyncio.sleep(min(2**attempt * 0.5, 2))
        raise ProviderError(f"文字 Provider 返回空内容（已重试 {attempts} 次）：{last_reason}")

    @classmethod
    def _validate_output(
        cls,
        schema: type[BaseModel],
        raw: str,
        plain_text_field: str | None,
    ) -> BaseModel:
        if plain_text_field is not None:
            value = {plain_text_field: cls._parse_plain_text(raw, plain_text_field)}
        else:
            value = cls._parse_json(raw)
            value = cls._normalize_value(value, schema)
        return schema.model_validate(value)

    @classmethod
    def _normalize_value(cls, value: Any, annotation: Any) -> Any:
        if value is None:
            return None
        item_annotation = cls._list_item_annotation(annotation)
        if item_annotation is not None:
            values = value if isinstance(value, list) else [value]
            return [cls._normalize_value(item, item_annotation) for item in values]
        model = cls._model_annotation(annotation)
        if model is not None and isinstance(value, dict):
            normalized = dict(value)
            for field_name, field in model.model_fields.items():
                if field_name in normalized:
                    normalized[field_name] = cls._normalize_value(
                        normalized[field_name], field.annotation
                    )
            return normalized
        if cls._is_text_annotation(annotation) and not isinstance(value, str):
            return cls._readable_text(value)
        return value

    @classmethod
    def _list_item_annotation(cls, annotation: Any) -> Any | None:
        origin = get_origin(annotation)
        if origin is list:
            args = get_args(annotation)
            return args[0] if args else Any
        if origin in (Union, UnionType):
            for item in get_args(annotation):
                item_annotation = cls._list_item_annotation(item)
                if item_annotation is not None:
                    return item_annotation
        return None

    @classmethod
    def _model_annotation(cls, annotation: Any) -> type[BaseModel] | None:
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            return annotation
        origin = get_origin(annotation)
        if origin is Annotated:
            args = get_args(annotation)
            return cls._model_annotation(args[0]) if args else None
        if origin in (Union, UnionType):
            for item in get_args(annotation):
                model = cls._model_annotation(item)
                if model is not None:
                    return model
        return None

    @classmethod
    def _is_text_annotation(cls, annotation: Any) -> bool:
        if annotation is str:
            return True
        origin = get_origin(annotation)
        if origin is Annotated:
            args = get_args(annotation)
            return bool(args) and cls._is_text_annotation(args[0])
        if origin in (Union, UnionType):
            return any(cls._is_text_annotation(item) for item in get_args(annotation))
        return False

    @classmethod
    def _readable_text(cls, value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list):
            return "、".join(filter(None, (cls._readable_text(item) for item in value)))
        if isinstance(value, dict):
            for key in ("summary", "description", "text", "value", "content", "line"):
                if key in value and (text := cls._readable_text(value[key])):
                    return text
            return "；".join(
                f"{key}：{text}"
                for key, item in value.items()
                if (text := cls._readable_text(item))
            )
        return str(value or "").strip()

    @staticmethod
    def _parse_plain_text(raw: str, field_name: str) -> str:
        text = raw.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(parsed, str):
            return parsed.strip()
        if isinstance(parsed, dict) and isinstance(parsed.get(field_name), str):
            return parsed[field_name].strip()
        return text

    @staticmethod
    def _extract_content(response: dict[str, Any]) -> str:
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ProviderError("OpenAI-compatible 响应缺少 choices[0].message.content")
        message = choices[0].get("message")
        if not isinstance(message, dict) or "content" not in message:
            raise ProviderError("OpenAI-compatible 响应缺少 choices[0].message.content")
        content = message["content"]
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
            return "".join(parts)
        if isinstance(content, dict):
            for key in ("text", "content"):
                if isinstance(content.get(key), str):
                    return content[key]
        raise ProviderError("OpenAI-compatible content 类型无效")

    @staticmethod
    def _finish_reason(response: dict[str, Any]) -> str:
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return ""
        return str(choices[0].get("finish_reason") or "")

    @staticmethod
    def _parse_json(raw: str) -> Any:
        text = raw.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start < 0 or end <= start:
                raise
            return json.loads(text[start : end + 1])
