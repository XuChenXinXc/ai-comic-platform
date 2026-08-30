from __future__ import annotations

import copy

import pytest

from app.dynamic_pipeline.provider_capabilities import (
    ProviderCapabilitiesSnapshot,
    adapt_generation_duration,
    normalize_capabilities,
    reference_limit,
)


def test_normalize_capabilities_uses_conservative_defaults() -> None:
    result = normalize_capabilities({})

    assert result.provider_id == ""
    assert result.model == ""
    assert result.capability_version == "1"
    assert result.supports_multiple_images is False
    assert result.max_reference_images == 1
    assert result.supported_durations == [6]
    assert result.min_duration_sec == 6
    assert result.max_duration_sec == 6
    assert result.duration_step_sec == 1
    assert result.supports_end_frame is False
    assert result.supports_audio is False
    assert result.supports_dialogue is False


def test_normalize_capabilities_accepts_model_and_ignores_non_capability_fields() -> None:
    raw = {
        "provider_id": "mock",
        "model": "mock-video",
        "capability_version": "2",
        "supports_multiple_images": True,
        "max_reference_images": 3,
        "supported_durations": [4, 6, 8],
        "min_duration_sec": 4,
        "max_duration_sec": 8,
        "duration_step_sec": 2,
        "supports_end_frame": True,
        "supports_audio": True,
        "supports_dialogue": True,
        "api_key": "must-not-be-read",
    }
    original = copy.deepcopy(raw)

    result = normalize_capabilities(raw)

    assert result == ProviderCapabilitiesSnapshot.model_validate(
        {key: value for key, value in raw.items() if key != "api_key"}
    )
    assert raw == original


@pytest.mark.parametrize(
    "raw",
    [
        {"max_reference_images": 0},
        {"supported_durations": [0]},
        {"min_duration_sec": 0},
        {"max_duration_sec": 0},
        {"min_duration_sec": 8, "max_duration_sec": 7},
        {"duration_step_sec": 0},
    ],
)
def test_normalize_capabilities_rejects_invalid_ranges(raw: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        normalize_capabilities(raw)


def test_adapt_generation_duration_prefers_supported_duration_6() -> None:
    capabilities = normalize_capabilities({})

    exact = adapt_generation_duration(6, capabilities)
    lower = adapt_generation_duration(5, capabilities)
    higher = adapt_generation_duration(7, capabilities)

    assert exact.effective_duration_sec == 6
    assert exact.changed is False
    assert exact.diagnostics == []
    assert lower.effective_duration_sec == 6
    assert lower.changed is True
    assert lower.diagnostics[0].code == "provider_duration_unsupported"
    assert lower.diagnostics[0].level == "warning"
    assert higher.effective_duration_sec == 6
    assert higher.changed is True


def test_adapt_generation_duration_clamps_and_aligns_to_step() -> None:
    capabilities = normalize_capabilities(
        {
            "supported_durations": [],
            "min_duration_sec": 4,
            "max_duration_sec": 10,
            "duration_step_sec": 2,
        }
    )

    below = adapt_generation_duration(1, capabilities)
    aligned = adapt_generation_duration(6, capabilities)
    between = adapt_generation_duration(9, capabilities)
    above = adapt_generation_duration(12, capabilities)

    assert below.effective_duration_sec == 4
    assert aligned.effective_duration_sec == 6
    assert between.effective_duration_sec == 8
    assert above.effective_duration_sec == 10


@pytest.mark.parametrize("requested", [0, -1])
def test_adapt_generation_duration_rejects_non_positive_request(requested: float) -> None:
    with pytest.raises(ValueError):
        adapt_generation_duration(requested, normalize_capabilities({}))


def test_reference_limit_blocks_single_image_provider_over_one() -> None:
    capabilities = normalize_capabilities({"max_reference_images": 3})

    result = reference_limit(capabilities, 2)

    assert result.requested_count == 2
    assert result.allowed_count == 1
    assert result.changed is True
    assert result.status == "blocked"
    assert result.diagnostics[0].code == "provider_reference_limit"
    assert result.diagnostics[0].level == "blocking"


def test_reference_limit_uses_multiple_image_limit() -> None:
    capabilities = normalize_capabilities(
        {"supports_multiple_images": True, "max_reference_images": 3}
    )

    ready = reference_limit(capabilities, 3)
    blocked = reference_limit(capabilities, 4)

    assert ready.allowed_count == 3
    assert ready.changed is False
    assert ready.status == "ready"
    assert ready.diagnostics == []
    assert blocked.allowed_count == 3
    assert blocked.changed is True
    assert blocked.status == "blocked"
    assert blocked.diagnostics[0].code == "provider_reference_limit"


def test_reference_limit_rejects_negative_count() -> None:
    with pytest.raises(ValueError):
        reference_limit(normalize_capabilities({}), -1)


def test_capability_helpers_do_not_mutate_input_or_capabilities() -> None:
    raw = {
        "supported_durations": [4, 6, 8],
        "min_duration_sec": 4,
        "max_duration_sec": 8,
        "duration_step_sec": 2,
    }
    original = copy.deepcopy(raw)
    capabilities = normalize_capabilities(raw)
    capability_snapshot = capabilities.model_dump()

    adapt_generation_duration(5, capabilities)
    reference_limit(capabilities, 2)

    assert raw == original
    assert capabilities.model_dump() == capability_snapshot
