from __future__ import annotations

import asyncio
import json
from typing import Any

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
        for internal_key in ("headers", "verify_ssl"):
            options.pop(internal_key, None)
        payload: dict[str, Any] = {"model": model, "messages": messages, **options}
        if json_response and response_format:
            payload["response_format"] = response_format
        payload["reasoning_effort"] = "xhigh"
        attempts = self.config.max_retries + 1
        last_reason = ""
        for attempt in range(attempts):
            response = await self._request_json(
                "POST",
                chat_path,
                options=self.config.params,
                headers=headers,
                json=payload,
            )
            try:
                content = self._extract_content(response)
            except ProviderError as exc:
                last_reason = str(exc)
                content = ""
            if content.strip():
                return content
            if not last_reason:
                finish_reason = (
                    str((response.get("choices") or [{}])[0].get("finish_reason", ""))
                    if isinstance(response.get("choices"), list)
                    else ""
                )
                last_reason = f"返回空内容（finish_reason={finish_reason or '未知'}）"
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
        return schema.model_validate(value)

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
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("OpenAI-compatible 响应缺少 choices[0].message.content") from exc
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
        raise ProviderError("OpenAI-compatible content 类型无效")

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
