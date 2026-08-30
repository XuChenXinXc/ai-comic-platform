"""动态对白音频 Job 合同。

本模块只负责把对白转换成确定性的纯数据计划。它不访问数据库、不创建 Job、也不
调用 TTS Provider；调用方可以在自己的事务边界外消费 ``to_job_payload`` 的结果。
"""

from __future__ import annotations

import copy
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.dynamic_pipeline.schemas import Diagnostic
from app.services.signature_service import calculate_signature

AUDIO_DELIVERY_VERSION = "dynamic-audio-delivery-v1"
AudioPlanStatus = Literal["ready", "warning", "blocked"]
AudioJobStatus = Literal["queued", "ready", "warning", "blocked"]
DialogueLineStatus = Literal["ready", "blocked"]


class AudioDeliveryError(ValueError):
    """对白无法安全转换为音频交付计划时抛出的错误。"""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DialogueLine(_StrictModel):
    """一条不与具体 Provider 调用绑定的标准对白行。"""

    dialogue_line_id: str = Field(min_length=1)
    line_order: int = Field(ge=1)
    shot_id: str = Field(min_length=1)
    external_key: str = Field(min_length=1)
    speaker: str = ""
    text: str = ""
    source_text_snapshot: str = ""
    source_refs: list[str] = Field(default_factory=list)
    status: DialogueLineStatus = "ready"
    provenance: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_content(self) -> DialogueLine:
        if not self.source_text_snapshot and self.text:
            self.source_text_snapshot = self.text
        if not self.text and self.source_text_snapshot:
            self.text = self.source_text_snapshot.strip()
        if not self.text.strip():
            self.status = "blocked"
        return self


class AudioJobPlan(_StrictModel):
    """一条对白对应的动态 Job 计划。"""

    workflow_run_id: str = Field(min_length=1)
    workflow_stage_id: str = Field(min_length=1)
    shot_id: str = Field(min_length=1)
    external_key: str = Field(min_length=1)
    dialogue_line_id: str = Field(min_length=1)
    line_order: int = Field(ge=1)
    text: str = ""
    source_text_snapshot: str = ""
    speaker: str = ""
    voice_id: str = ""
    source_refs: list[str] = Field(default_factory=list)
    provider_id: str = Field(min_length=1)
    provider_model: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    input_signature: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list)
    status: AudioJobStatus = "ready"
    provenance: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_dependencies(self) -> AudioJobPlan:
        if any(not item.strip() for item in self.depends_on):
            raise ValueError("depends_on 只能包含非空任务 ID")
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError("depends_on 不能包含重复任务 ID")
        if not self.source_text_snapshot and self.text:
            self.source_text_snapshot = self.text
        if not self.text and self.source_text_snapshot:
            self.text = self.source_text_snapshot.strip()
        if not self.text.strip() and self.status != "blocked":
            self.status = "blocked"
        return self

    @model_validator(mode="before")
    @classmethod
    def normalize_field_aliases(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        if "workflow_stage_id" not in data and "stage_id" in data:
            data["workflow_stage_id"] = data["stage_id"]
        if "provider_model" not in data and "model" in data:
            data["provider_model"] = data["model"]
        if "provenance" not in data and "provenance_json" in data:
            data["provenance"] = data["provenance_json"]
        data.pop("stage_id", None)
        data.pop("model", None)
        data.pop("provenance_json", None)
        return data

    @property
    def model(self) -> str:
        """兼容现有媒体计划和 shot_audio 的 model 命名。"""

        return self.provider_model

    @property
    def stage_id(self) -> str:
        """兼容按 stage_id 读取动态计划的调用方。"""

        return self.workflow_stage_id

    @property
    def job_type(self) -> str:
        return "generate_dialogue_audio"

    @property
    def target_type(self) -> str:
        return "shot_audio"

    @property
    def provider_kind(self) -> str:
        return "tts"

    @property
    def provenance_json(self) -> dict[str, Any]:
        return copy.deepcopy(self.provenance)

    def to_job_payload(self) -> dict[str, Any]:
        """返回现有 JobCreate 可消费的动态字段，不执行入队。"""

        return _job_payload(self)

    @property
    def job_payload(self) -> dict[str, Any]:
        return self.to_job_payload()


class AudioDeliveryPlan(_StrictModel):
    """按分镜聚合的对白、Job 和诊断结果。"""

    workflow_run_id: str = Field(min_length=1)
    workflow_stage_id: str = Field(min_length=1)
    input_signature: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    provider_model: str = ""
    lines: list[DialogueLine] = Field(default_factory=list)
    jobs: list[AudioJobPlan] = Field(default_factory=list)
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    status: AudioPlanStatus = "ready"
    provenance: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def normalize_field_aliases(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        if "workflow_stage_id" not in data and "stage_id" in data:
            data["workflow_stage_id"] = data["stage_id"]
        if "provider_model" not in data and "model" in data:
            data["provider_model"] = data["model"]
        if "provenance" not in data and "provenance_json" in data:
            data["provenance"] = data["provenance_json"]
        data.pop("stage_id", None)
        data.pop("model", None)
        data.pop("provenance_json", None)
        return data

    @property
    def stage_id(self) -> str:
        return self.workflow_stage_id

    @property
    def model(self) -> str:
        return self.provider_model

    @property
    def provenance_json(self) -> dict[str, Any]:
        return copy.deepcopy(self.provenance)


def _coerce_dialogue_line(
    value: DialogueLine | Mapping[str, Any],
    *,
    external_key: str,
    shot_id: str,
) -> DialogueLine:
    if isinstance(value, DialogueLine):
        return value.model_copy(deep=True)
    if isinstance(value, Mapping):
        data = _copy(value)
        canonical = {"dialogue_line_id", "line_order", "shot_id", "external_key"}
        if canonical.issubset(data):
            if not _text(data.get("source_text_snapshot")) and _text(data.get("text")):
                data["source_text_snapshot"] = data["text"]
            line = DialogueLine.model_validate(data)
            if not line.text and line.source_text_snapshot:
                line = line.model_copy(
                    update={"text": _text(line.source_text_snapshot), "status": "ready"}
                )
            elif not line.text:
                line = line.model_copy(update={"status": "blocked"})
            return line
    return normalize_dialogue_lines(value, external_key=external_key, shot_id=shot_id)[0]


_ALIASES = {
    "speaker": ("speaker", "character", "character_name", "name", "role"),
    "text": ("text", "content", "line", "dialogue", "speech"),
    "dialogue_line_id": ("dialogue_line_id", "line_id", "id", "key"),
}


def _plain(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python")
    return value


def _copy(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return copy.deepcopy(value.model_dump(mode="python"))
    return copy.deepcopy(value)


def _text(value: Any) -> str:
    if value is None:
        return ""
    return value.strip() if isinstance(value, str) else str(value).strip()


def _snapshot_text(value: Any) -> str:
    """转换为字符串但不 trim，保留输入中的原始文本快照。"""

    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _pick(mapping: Mapping[str, Any], names: Sequence[str]) -> Any:
    first: Any = None
    for name in names:
        if name not in mapping:
            continue
        value = mapping[name]
        if first is None:
            first = value
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, list | tuple | set | frozenset | dict) and not value:
            continue
        return value
    return first


def _as_list(value: Any) -> list[Any]:
    value = _plain(value)
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set | frozenset):
        return sorted(value, key=repr)
    return [value]


def _ref_list(value: Any) -> list[str]:
    value = _plain(value)
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, Mapping):
        value = _pick(value, ("ref", "source_ref", "reference", "value", "name"))
        return _ref_list(value)
    result: list[str] = []
    for item in _as_list(value):
        if isinstance(item, Mapping):
            item = _pick(item, ("ref", "source_ref", "reference", "value", "name"))
        text = _text(item)
        if text:
            result.append(text)
    return result


def _dialogue_id(external_key: str, line_order: int, explicit_id: str) -> str:
    if explicit_id:
        return explicit_id
    return f"{external_key}-dialogue-{line_order}"


def normalize_dialogue_lines(
    dialogues: Any,
    *,
    external_key: str,
    shot_id: str,
    source_refs: Sequence[str] = (),
) -> list[DialogueLine]:
    """归一化一镜对白，保留快照并且不修改调用方输入。"""

    external_key = _text(external_key)
    shot_id = _text(shot_id)
    if not external_key or not shot_id:
        raise AudioDeliveryError("external_key 与 shot_id 不能为空")
    shared_refs = _ref_list(source_refs)
    lines: list[DialogueLine] = []
    for line_order, raw in enumerate(_as_list(dialogues), start=1):
        source = _plain(raw)
        if isinstance(source, str):
            source = {"text": source}
        elif not isinstance(source, Mapping):
            source = {"text": source}
        source = dict(source)
        speaker = _text(_pick(source, _ALIASES["speaker"]))
        raw_text = _pick(source, _ALIASES["text"])
        raw_snapshot = source.get("source_text_snapshot")
        if "source_text_snapshot" not in source or raw_snapshot is None:
            raw_snapshot = raw_text
        if raw_text is None:
            raw_text = raw_snapshot
        text = _text(raw_text)
        source_text_snapshot = _snapshot_text(raw_snapshot)
        explicit_id = _text(_pick(source, _ALIASES["dialogue_line_id"]))
        line_id = _dialogue_id(external_key, line_order, explicit_id)
        refs = _ref_list(_pick(source, ("source_refs", "source_ref", "references")))
        if not refs:
            refs = list(shared_refs)
        lines.append(
            DialogueLine(
                dialogue_line_id=line_id,
                line_order=line_order,
                shot_id=shot_id,
                external_key=external_key,
                speaker=speaker,
                text=text,
                source_text_snapshot=source_text_snapshot,
                source_refs=refs,
                status="ready" if text else "blocked",
                provenance={
                    "external_key": external_key,
                    "shot_id": shot_id,
                    "dialogue_line_id": line_id,
                    "line_order": line_order,
                    "source_refs": list(refs),
                },
            )
        )
    return lines


def _signature_payload(
    line: DialogueLine,
    *,
    workflow_run_id: str,
    workflow_stage_id: str,
    input_signature: str,
    provider_id: str,
    provider_model: str,
    voice_id: str,
    params: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "version": AUDIO_DELIVERY_VERSION,
        "dialogue_line_id": line.dialogue_line_id,
        "line_order": line.line_order,
        "shot_id": line.shot_id,
        "external_key": line.external_key,
        "source_text_snapshot": line.source_text_snapshot,
        "source_refs": list(line.source_refs),
        "speaker": line.speaker,
        "voice_id": voice_id,
        "provider_id": provider_id,
        "provider_model": provider_model,
        "params": _copy(dict(params)),
        "workflow_run_id": workflow_run_id,
        "workflow_stage_id": workflow_stage_id,
        "input_signature": input_signature,
    }


def _job_payload(plan: AudioJobPlan) -> dict[str, Any]:
    provenance = _copy(plan.provenance)
    input_data = {
        "workflow_run_id": plan.workflow_run_id,
        "workflow_stage_id": plan.workflow_stage_id,
        "shot_id": plan.shot_id,
        "external_key": plan.external_key,
        "dialogue_line_id": plan.dialogue_line_id,
        "line_order": plan.line_order,
        "source_text_snapshot": plan.source_text_snapshot,
        "source_refs": list(plan.source_refs),
        "speaker": plan.speaker,
        "voice_id": plan.voice_id,
        "provider_id": plan.provider_id,
        "provider_model": plan.provider_model,
        "model": plan.provider_model,
        "text": plan.text,
        "params": _copy(plan.params),
        "input_signature": plan.input_signature,
        "upstream_input_signature": plan.provenance.get("upstream_input_signature", ""),
        "provenance": provenance,
        "provenance_json": provenance,
        "depends_on": list(plan.depends_on),
        "plan_status": plan.status,
    }
    return {
        "job_type": "generate_dialogue_audio",
        "target_type": "shot_audio",
        "target_id": plan.shot_id,
        "workflow_run_id": plan.workflow_run_id,
        "workflow_stage_id": plan.workflow_stage_id,
        "idempotency_key": plan.idempotency_key,
        # Job runner 当前按 text/image/video 分类；合同仍明确 TTS provider kind。
        "provider_kind": "tts",
        "provider_id": plan.provider_id,
        "input_signature": plan.input_signature,
        "depends_on": list(plan.depends_on),
        "input_data": input_data,
        "status": plan.status,
        "queueable": plan.status != "blocked",
        "progress_total": 1,
    }


def build_audio_job_plans(
    lines: Sequence[DialogueLine | Mapping[str, Any]],
    *,
    workflow_run_id: str,
    workflow_stage_id: str,
    shot_id: str,
    external_key: str,
    input_signature: str,
    provider_id: str,
    provider_model: str,
    voice_by_speaker: Mapping[str, str] | None = None,
    params: Mapping[str, Any] | None = None,
) -> list[AudioJobPlan]:
    """为每条有文本的对白构造一个可直接消费的 Job 计划。"""

    values = {
        "workflow_run_id": _text(workflow_run_id),
        "workflow_stage_id": _text(workflow_stage_id),
        "shot_id": _text(shot_id),
        "external_key": _text(external_key),
        "provider_id": _text(provider_id),
        "provider_model": _text(provider_model),
    }
    upstream_input_signature = _text(input_signature)
    if not upstream_input_signature or not all(values.values()):
        raise AudioDeliveryError("workflow、shot、signature 和 Provider 字段不能为空")
    voice_map = {_text(key): _text(value) for key, value in (voice_by_speaker or {}).items()}
    shared_params = _copy(dict(params or {}))
    plans: list[AudioJobPlan] = []
    for raw_line in lines:
        line = _coerce_dialogue_line(
            raw_line,
            external_key=values["external_key"],
            shot_id=values["shot_id"],
        )
        if line.shot_id != values["shot_id"] or line.external_key != values["external_key"]:
            raise AudioDeliveryError("对白行的 shot_id/external_key 与 Job 参数不一致")
        if not line.text.strip():
            continue
        voice_id = voice_map.get(line.speaker, "")
        signature_parts = _signature_payload(
            line,
            workflow_run_id=values["workflow_run_id"],
            workflow_stage_id=values["workflow_stage_id"],
            input_signature=upstream_input_signature,
            provider_id=values["provider_id"],
            provider_model=values["provider_model"],
            voice_id=voice_id,
            params=shared_params,
        )
        job_signature = calculate_signature(signature_parts)
        idempotency_key = "dynamic-audio-v1:" + calculate_signature(
            values["workflow_run_id"],
            values["workflow_stage_id"],
            line.shot_id,
            line.external_key,
            line.dialogue_line_id,
            job_signature,
        )
        provenance = {
            "audio_delivery_version": AUDIO_DELIVERY_VERSION,
            "workflow_run_id": values["workflow_run_id"],
            "workflow_stage_id": values["workflow_stage_id"],
            "shot_id": line.shot_id,
            "external_key": line.external_key,
            "dialogue_line_id": line.dialogue_line_id,
            "line_order": line.line_order,
            "source_refs": list(line.source_refs),
            "source_text_snapshot": line.source_text_snapshot,
            "speaker": line.speaker,
            "voice_id": voice_id,
            "provider_id": values["provider_id"],
            "provider_model": values["provider_model"],
            "params": _copy(shared_params),
            "input_signature": job_signature,
            "upstream_input_signature": upstream_input_signature,
            "job_signature": job_signature,
        }
        plans.append(
            AudioJobPlan(
                **values,
                dialogue_line_id=line.dialogue_line_id,
                line_order=line.line_order,
                text=line.text,
                source_text_snapshot=line.source_text_snapshot,
                speaker=line.speaker,
                voice_id=voice_id,
                source_refs=list(line.source_refs),
                params=_copy(shared_params),
                input_signature=job_signature,
                idempotency_key=idempotency_key,
                depends_on=[],
                status="ready",
                provenance={**provenance, "dialogue_provenance": _copy(line.provenance)},
            )
        )
    return plans


def _mapping_sort_key(key: Any, value: Any) -> tuple[str, str]:
    if isinstance(key, tuple | list):
        parts = [part for part in (_text(item) for item in key) if part]
        if len(parts) >= 2:
            return parts[1], parts[0]
        if parts:
            return parts[0], parts[0]
    source = _plain(value)
    if isinstance(source, Mapping):
        external_key = _text(
            _pick(source, ("external_key", "external_id", "external_shot_key", "stable_key"))
        )
        shot_id = _text(_pick(source, ("shot_id", "id")))
    else:
        external_key = ""
        shot_id = ""
    return (external_key or _text(key), shot_id or _text(key))


def _mapping_identity(key: Any, source: Mapping[str, Any]) -> tuple[str, str]:
    key_values = (
        [part for part in (_text(item) for item in key)]
        if isinstance(key, tuple | list)
        else [_text(key)]
    )
    key_values = [item for item in key_values if item]
    has_dialogue_container = any(
        name in source for name in ("dialogues", "dialogue_lines", "dialogue_cues")
    ) or (
        "dialogue" in source
        and isinstance(source["dialogue"], list | tuple | set | frozenset | dict)
    )
    is_dialogue_object = not has_dialogue_container and any(
        name in source
        for name in ("text", "content", "line", "speech", "speaker", "character", "name", "role")
    )
    shot_field = next(
        (
            name
            for name in (("shot_id", "id") if not is_dialogue_object else ("shot_id",))
            if name in source
        ),
        None,
    )
    external_field = next(
        (
            name
            for name in ("external_key", "external_id", "external_shot_key", "stable_key")
            if name in source
        ),
        None,
    )
    shot_id = _text(source[shot_field]) if shot_field is not None else ""
    external_key = _text(source[external_field]) if external_field is not None else ""
    explicit_shot_id = shot_field is not None
    explicit_external_key = external_field is not None
    if len(key_values) >= 2:
        if not explicit_shot_id:
            shot_id = key_values[0]
        if not explicit_external_key:
            external_key = key_values[1]
    elif key_values:
        if not explicit_shot_id:
            shot_id = key_values[0]
        if not explicit_external_key:
            external_key = key_values[0]
    return shot_id, external_key


def _blocking_diagnostic(
    code: str,
    message: str,
    path: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> Diagnostic:
    return Diagnostic(
        level="blocking",
        code=code,
        message=message,
        path=path,
        details=dict(details) if details is not None else {},
    )


def build_audio_delivery_plan(
    shot_dialogues: Mapping[Any, Any],
    *,
    workflow_run_id: str,
    workflow_stage_id: str,
    input_signature: str,
    provider_id: str,
    provider_model: str,
    voice_by_speaker: Mapping[str, str] | None = None,
    params: Mapping[str, Any] | None = None,
) -> AudioDeliveryPlan:
    """按 shot/external key 的稳定顺序聚合对白 Job；空文本只阻断，不入队。"""

    if not isinstance(shot_dialogues, Mapping):
        raise AudioDeliveryError("shot_dialogues 必须是 Mapping")
    if not _text(workflow_run_id) or not _text(workflow_stage_id):
        raise AudioDeliveryError("workflow_run_id 与 workflow_stage_id 不能为空")
    if not _text(input_signature) or not _text(provider_id):
        raise AudioDeliveryError("input_signature 与 provider_id 不能为空")
    ordered = sorted(shot_dialogues.items(), key=lambda item: _mapping_sort_key(*item))
    all_lines: list[DialogueLine] = []
    all_jobs: list[AudioJobPlan] = []
    diagnostics: list[Diagnostic] = []
    for map_key, raw in ordered:
        source = _plain(raw)
        if isinstance(source, Mapping):
            dialogue_containers = ("dialogues", "dialogue", "dialogue_lines", "dialogue_cues")
            is_single_dialogue = not any(name in source for name in dialogue_containers) and (
                "dialogue_line_id" in source
                or any(
                    name in source
                    for name in ("speaker", "character", "character_name", "name", "role")
                )
                or any(name in source for name in ("text", "content", "line", "speech"))
            )
            shot_id, external_key = _mapping_identity(map_key, source)
            if is_single_dialogue:
                dialogues = source
            else:
                dialogues = _pick(source, dialogue_containers)
                if dialogues is None:
                    dialogues = []
            refs = _ref_list(_pick(source, ("source_refs", "source_ref", "references")))
        else:
            if isinstance(map_key, tuple | list):
                parts = [part for part in (_text(item) for item in map_key) if part]
                shot_id = parts[0] if parts else ""
                external_key = parts[1] if len(parts) > 1 else shot_id
            else:
                external_key = _text(map_key)
                shot_id = _text(map_key)
            dialogues = source
            refs = []
        try:
            lines = normalize_dialogue_lines(
                dialogues,
                external_key=external_key,
                shot_id=shot_id,
                source_refs=refs,
            )
            jobs = build_audio_job_plans(
                lines,
                workflow_run_id=workflow_run_id,
                workflow_stage_id=workflow_stage_id,
                shot_id=shot_id,
                external_key=external_key,
                input_signature=input_signature,
                provider_id=provider_id,
                provider_model=provider_model,
                voice_by_speaker=voice_by_speaker,
                params=params,
            )
        except (AudioDeliveryError, TypeError, ValueError, ValidationError) as exc:
            diagnostics.append(
                _blocking_diagnostic("audio_dialogue_invalid", str(exc), str(map_key))
            )
            continue
        all_lines.extend(lines)
        all_jobs.extend(jobs)
        for line in lines:
            if not line.text.strip():
                diagnostics.append(
                    _blocking_diagnostic(
                        "audio_dialogue_text_missing",
                        "对白缺少可生成音频的文本",
                        f"{external_key}.dialogue[{line.line_order - 1}].text",
                        {"dialogue_line_id": line.dialogue_line_id},
                    )
                )
    status: AudioPlanStatus = "blocked" if diagnostics else "ready"
    duplicate_ids = sorted(
        {
            line.dialogue_line_id
            for line in all_lines
            if sum(item.dialogue_line_id == line.dialogue_line_id for item in all_lines) > 1
        }
    )
    if duplicate_ids:
        if not any(item.code == "audio_duplicate_dialogue_line_id" for item in diagnostics):
            diagnostics.append(
                _blocking_diagnostic(
                    "audio_duplicate_dialogue_line_id",
                    "交付计划存在重复 dialogue_line_id，禁止音频行覆盖",
                    "lines",
                    {"dialogue_line_ids": duplicate_ids},
                )
            )
        all_jobs = [job for job in all_jobs if job.dialogue_line_id not in duplicate_ids]
    status = "blocked" if diagnostics else "ready"
    return AudioDeliveryPlan(
        workflow_run_id=_text(workflow_run_id),
        workflow_stage_id=_text(workflow_stage_id),
        input_signature=_text(input_signature),
        provider_id=_text(provider_id),
        provider_model=_text(provider_model),
        lines=all_lines,
        jobs=all_jobs,
        diagnostics=diagnostics,
        status=status,
        provenance={
            "audio_delivery_version": AUDIO_DELIVERY_VERSION,
            "workflow_run_id": _text(workflow_run_id),
            "workflow_stage_id": _text(workflow_stage_id),
            "input_signature": _text(input_signature),
            "provider_id": _text(provider_id),
            "provider_model": _text(provider_model),
        },
    )


def audio_job_to_shot_audio_payload(
    plan: AudioJobPlan | Mapping[str, Any],
    *,
    audio_id: str,
    version_no: int,
    file_path: str,
    duration_sec: float,
    provenance_extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """把 Job 计划投影成现有 ``shot_audio`` 可用的 insert/update payload。"""

    target = plan if isinstance(plan, AudioJobPlan) else AudioJobPlan.model_validate(_copy(plan))
    if not _text(audio_id) or not _text(file_path):
        raise AudioDeliveryError("audio_id 与 file_path 不能为空")
    if version_no < 1 or duration_sec <= 0 or not math.isfinite(duration_sec):
        raise AudioDeliveryError("version_no 必须为正数且 duration_sec 必须大于 0")
    provenance = _copy(target.provenance)
    provenance.update(_copy(dict(provenance_extra or {})))
    return {
        "id": _text(audio_id),
        "shot_id": target.shot_id,
        "dialogue_line_id": target.dialogue_line_id,
        "line_order": target.line_order,
        "dialogue_text": target.text or target.source_text_snapshot,
        "source_text_snapshot": target.source_text_snapshot,
        "speaker_name": target.speaker,
        "voice_id": target.voice_id,
        "file_path": _text(file_path),
        "duration_sec": duration_sec,
        "version_no": version_no,
        "provider_id": target.provider_id,
        "model": target.provider_model,
        "status": "completed",
        "is_current": 1,
        "source": "generated",
        "input_signature": target.input_signature,
        "provenance_json": json.dumps(provenance, ensure_ascii=False, sort_keys=True),
        "error": "",
    }


def audio_job_to_job_payload(plan: AudioJobPlan | Mapping[str, Any]) -> dict[str, Any]:
    """公开 JobCreate 兼容的 payload 投影，便于调用方直接入队。"""

    target = plan if isinstance(plan, AudioJobPlan) else AudioJobPlan.model_validate(_copy(plan))
    return _job_payload(target)


def to_job_payload(plan: AudioJobPlan | Mapping[str, Any]) -> dict[str, Any]:
    """``audio_job_to_job_payload`` 的简短兼容别名。"""

    return audio_job_to_job_payload(plan)


__all__ = [
    "AUDIO_DELIVERY_VERSION",
    "AudioDeliveryError",
    "AudioDeliveryPlan",
    "AudioJobPlan",
    "DialogueLine",
    "audio_job_to_job_payload",
    "audio_job_to_shot_audio_payload",
    "build_audio_delivery_plan",
    "build_audio_job_plans",
    "normalize_dialogue_lines",
    "to_job_payload",
]
