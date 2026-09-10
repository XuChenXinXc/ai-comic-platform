import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

_ERROR_URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_ERROR_BEARER_PATTERN = re.compile(
    r"\bBearer\s+[A-Za-z0-9._~+/=-]+",
    re.IGNORECASE,
)
_ERROR_SECRET_PATTERN = re.compile(
    r"\b("
    r"authorization|cookie|credentials?|password|passwd|secret|signature|token|"
    r"api[_-]?key|apikey|access[_-]?token|client[_-]?secret|private[_-]?key"
    r")\b(\s*[:=]\s*)([^,;\r\n]+)",
    re.IGNORECASE,
)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def sanitize_error_message(value: Any, *, max_length: int = 2000) -> str:
    text = str(value or "").strip()
    text = _ERROR_URL_PATTERN.sub("[已隐藏 URL]", text)
    text = _ERROR_BEARER_PATTERN.sub("Bearer [已隐藏]", text)
    text = _ERROR_SECRET_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[已隐藏]",
        text,
    )
    return text[:max_length]


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def json_loads(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, dict | list):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def row_dict(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    mapping: Mapping[str, Any] = row._mapping if hasattr(row, "_mapping") else row
    return dict(mapping)


def decode_columns(record: dict[str, Any], columns: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(record)
    for source, default in columns.items():
        if source not in result:
            continue
        target = source.removesuffix("_json")
        result[target] = json_loads(result.pop(source), default)
    return result


def provided_values(model: Any) -> dict[str, Any]:
    return model.model_dump(exclude_unset=True)
