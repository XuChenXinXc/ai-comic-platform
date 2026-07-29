from __future__ import annotations

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
        raw = await self._complete(messages, schema, params)
        try:
            return schema.model_validate(self._parse_json(raw))
        except (ValidationError, ValueError, json.JSONDecodeError) as first_error:
            repair_messages = [
                *messages,
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": (
                        "上一次输出无法通过校验。请只返回修复后的完整 JSON 对象。\n"
                        f"校验错误：{first_error}"
                    ),
                },
            ]
            repaired = await self._complete(repair_messages, schema, params)
            try:
                return schema.model_validate(self._parse_json(repaired))
            except (ValidationError, ValueError, json.JSONDecodeError) as exc:
                summary = repaired.strip().replace("\n", " ")[:500]
                raise ProviderError(
                    f"文字 Provider 的结构化输出修复失败：{exc}；响应摘要：{summary}"
                ) from exc

    async def _complete(
        self,
        messages: list[dict[str, str]],
        schema: type[BaseModel],
        params: dict[str, Any],
    ) -> str:
        options = {**self.config.params, **params}
        chat_path = str(options.pop("chat_path", "chat/completions"))
        model = str(options.pop("model", self.config.model))
        headers = self._headers(options)
        headers.setdefault("Content-Type", "application/json")
        response_format = options.pop("response_format", {"type": "json_object"})
        for internal_key in ("headers", "verify_ssl"):
            options.pop(internal_key, None)
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "response_format": response_format,
            **options,
        }
        response = await self._request_json(
            "POST",
            chat_path,
            options=self.config.params,
            headers=headers,
            json=payload,
        )
        content = self._extract_content(response)
        if not content.strip():
            raise ProviderError("文字 Provider 返回空内容")
        return content

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
