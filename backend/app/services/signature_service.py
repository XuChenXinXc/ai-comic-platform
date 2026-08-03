from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel

SHOT_SIGNATURE_FIELDS = (
    "source_type",
    "source_ref",
    "scene_asset_id",
    "character_ids",
    "character_ids_json",
    "prop_ids",
    "prop_ids_json",
    "positions",
    "positions_json",
    "start_state",
    "action",
    "expressions",
    "expressions_json",
    "dialogue",
    "dialogue_json",
    "narration",
    "shot_type",
    "camera_angle",
    "camera_move",
    "duration_sec",
    "end_state",
    "next_hook",
    "frame_description",
    "image_prompt",
    "image_prompt_override",
    "video_prompt",
    "video_prompt_override",
    "negative_prompt",
    "reference_bindings",
    "reference_bindings_json",
)


def normalize_text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def _json_default(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, set | frozenset):
        return sorted(value)
    raise TypeError(f"无法序列化签名输入：{type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def _sha256(value: str | bytes) -> str:
    payload = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(payload).hexdigest()


def calculate_signature(*parts: Any) -> str:
    return _sha256(canonical_json(parts))


def source_signature(source_text: str) -> str:
    return _sha256(normalize_text(source_text))


def screenplay_signature(content: Mapping[str, Any] | BaseModel) -> str:
    return _sha256(canonical_json(content))


def asset_signature(
    profile: Mapping[str, Any],
    prompt: str,
    negative_prompt: str,
    style_prompt: str,
) -> str:
    return calculate_signature(
        {
            "profile": profile,
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "style_prompt": style_prompt,
        }
    )


def shot_signature(
    values: Mapping[str, Any] | BaseModel,
    **overrides: Any,
) -> str:
    raw = values.model_dump(mode="json") if isinstance(values, BaseModel) else dict(values)
    raw.update(overrides)
    semantic_values = {
        name: raw[name]
        for name in SHOT_SIGNATURE_FIELDS
        if name in raw
        and not (
            name in {"image_prompt_override", "video_prompt_override"}
            and not str(raw[name] or "").strip()
        )
    }
    return calculate_signature(semantic_values)


def _reference_fingerprints(reference_files: Sequence[str | Path]) -> list[Any]:
    fingerprints: list[Any] = []
    for reference in reference_files:
        path = Path(reference)
        if path.is_file():
            fingerprints.append(
                {
                    "name": path.name,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
        else:
            fingerprints.append(str(reference))
    return fingerprints


def asset_image_signature(
    asset_own_signature: str,
    provider: str,
    model: str,
    params: Mapping[str, Any],
    reference_files: Sequence[str | Path],
) -> str:
    return calculate_signature(
        asset_own_signature,
        provider,
        model,
        params,
        _reference_fingerprints(reference_files),
    )


def keyframe_signature(
    shot_own_signature: str,
    reference_bindings: Sequence[Mapping[str, Any]],
    provider: str,
    model: str,
    params: Mapping[str, Any],
) -> str:
    return calculate_signature(
        shot_own_signature,
        reference_bindings,
        provider,
        model,
        params,
    )


def video_signature(
    keyframe_version_id: str,
    video_prompt: str,
    duration_sec: float,
    provider: str,
    model: str,
    params: Mapping[str, Any],
    reference_bindings: Sequence[Mapping[str, Any]] = (),
) -> str:
    parts: list[Any] = [
        keyframe_version_id,
        video_prompt,
        duration_sec,
        provider,
        model,
        params,
    ]
    if reference_bindings:
        parts.append(reference_bindings)
    return calculate_signature(*parts)


def is_stale(saved_signature: str, current_signature: str) -> bool:
    return saved_signature != current_signature
