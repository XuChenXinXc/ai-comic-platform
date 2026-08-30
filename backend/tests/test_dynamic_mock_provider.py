from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.dynamic_pipeline.schemas import CharacterBiosOutput, RhythmOutput, SourceAnalysisOutput
from app.dynamic_pipeline.stage_gates import (
    evaluate_character_bios_gate,
    evaluate_rhythm_gate,
    evaluate_source_gate,
)
from app.providers.mock import MockTextProvider
from app.schemas import ProviderConfig

DYNAMIC_SCHEMAS = (SourceAnalysisOutput, CharacterBiosOutput, RhythmOutput)


def _provider(**params: Any) -> MockTextProvider:
    return MockTextProvider(
        ProviderConfig(
            id="mock-text-dynamic",
            kind="text",
            adapter="mock",
            model="mock-model",
            params=params,
        )
    )


def _generate(provider: MockTextProvider, schema: type[Any]) -> Any:
    return asyncio.run(
        provider.generate_json(
            system_prompt="生成动态阶段产物",
            user_prompt="雨夜里主角在仓库发现一枚关键徽记。",
            schema=schema,
            params={},
        )
    )


@pytest.mark.parametrize("schema", DYNAMIC_SCHEMAS)
def test_mock_text_dynamic_schema_output_passes_model_validate(schema: type[Any]) -> None:
    output = _generate(_provider(), schema)

    validated = schema.model_validate(output.model_dump())

    assert isinstance(validated, schema)


@pytest.mark.parametrize(
    ("schema", "gate", "kwargs"),
    [
        pytest.param(SourceAnalysisOutput, evaluate_source_gate, {}, id="source"),
        pytest.param(CharacterBiosOutput, evaluate_character_bios_gate, {}, id="character-bios"),
        pytest.param(RhythmOutput, evaluate_rhythm_gate, {"target_duration_sec": 30}, id="rhythm"),
    ],
)
def test_mock_text_dynamic_output_passes_thirty_second_stage_gate(
    schema: type[Any], gate: Any, kwargs: dict[str, Any]
) -> None:
    output = _generate(_provider(), schema)

    result = gate(output, **kwargs)

    assert result.status == "ready"
    assert result.passed is True
    assert result.can_confirm is True


def test_mock_text_delay_sec_is_awaited(monkeypatch: pytest.MonkeyPatch) -> None:
    delay_sec = 0.03
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr("app.providers.mock.asyncio.sleep", fake_sleep)
    asyncio.run(
        _provider().generate_json(
            system_prompt="生成动态阶段产物",
            user_prompt="测试延迟",
            schema=SourceAnalysisOutput,
            params={"delay_sec": delay_sec},
        )
    )

    assert delays == [delay_sec]
