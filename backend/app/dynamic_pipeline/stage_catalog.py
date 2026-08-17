"""动态漫工作流的固定阶段目录。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class StageDefinition:
    """一个顶层工作流阶段的不可变定义。"""

    key: str
    ordinal: int
    prerequisite_keys: tuple[str, ...]
    parallelizable: bool
    description: str
    parallel_description: str

    @property
    def can_run_in_parallel(self) -> bool:
        """返回该阶段是否可以与同一屏障中的阶段并行执行。"""

        return self.parallelizable


STAGE_KEYS: tuple[str, ...] = (
    "0-source",
    "1-character-bios",
    "2-rhythm",
    "3-script",
    "4-storyboard",
    "5a-image-prompts",
    "5b-video-prompts",
    "6-delivery",
)


STAGE_DEFINITIONS: Mapping[str, StageDefinition] = MappingProxyType(
    {
        "0-source": StageDefinition(
            key="0-source",
            ordinal=0,
            prerequisite_keys=(),
            parallelizable=False,
            description="保存源文快照并完成源文拆解。",
            parallel_description="按阶段屏障串行执行。",
        ),
        "1-character-bios": StageDefinition(
            key="1-character-bios",
            ordinal=1,
            prerequisite_keys=("0-source",),
            parallelizable=False,
            description="生成角色小传和一致性视觉锚点。",
            parallel_description="按阶段屏障串行执行。",
        ),
        "2-rhythm": StageDefinition(
            key="2-rhythm",
            ordinal=2,
            prerequisite_keys=("1-character-bios",),
            parallelizable=False,
            description="生成节奏、爽点和时间轴建议。",
            parallel_description="按阶段屏障串行执行。",
        ),
        "3-script": StageDefinition(
            key="3-script",
            ordinal=3,
            prerequisite_keys=("2-rhythm",),
            parallelizable=False,
            description="生成可执行的详细剧本。",
            parallel_description="按阶段屏障串行执行。",
        ),
        "4-storyboard": StageDefinition(
            key="4-storyboard",
            ordinal=4,
            prerequisite_keys=("3-script",),
            parallelizable=False,
            description="生成标准连续分镜。",
            parallel_description="按阶段屏障串行执行。",
        ),
        "5a-image-prompts": StageDefinition(
            key="5a-image-prompts",
            ordinal=5,
            prerequisite_keys=("4-storyboard",),
            parallelizable=True,
            description="编译图片提示词和关键帧语义计划。",
            parallel_description="可与 5b-video-prompts 并行执行。",
        ),
        "5b-video-prompts": StageDefinition(
            key="5b-video-prompts",
            ordinal=5,
            prerequisite_keys=("4-storyboard",),
            parallelizable=True,
            description="编译视频提示词和连续性计划。",
            parallel_description="可与 5a-image-prompts 并行执行。",
        ),
        "6-delivery": StageDefinition(
            key="6-delivery",
            ordinal=6,
            prerequisite_keys=("5a-image-prompts", "5b-video-prompts"),
            parallelizable=False,
            description="执行图片、视频、音频、字幕和合成交付子任务。",
            parallel_description="顶层阶段按屏障执行；内部子任务不拆为顶层阶段。",
        ),
    }
)


def _require_stage_key(stage_key: str) -> StageDefinition:
    try:
        return STAGE_DEFINITIONS[stage_key]
    except KeyError as exc:
        raise ValueError(f"非法阶段键: {stage_key!r}") from exc


def get_stage_definition(stage_key: str) -> StageDefinition:
    """返回阶段定义；阶段键非法时抛出 ``ValueError``。"""

    return _require_stage_key(stage_key)


def get_stage_ordinal(stage_key: str) -> int:
    """返回阶段在工作流屏障中的 ordinal。"""

    return _require_stage_key(stage_key).ordinal


def get_stage_prerequisites(stage_key: str) -> tuple[str, ...]:
    """返回阶段的直接前置阶段键。"""

    return _require_stage_key(stage_key).prerequisite_keys


def get_descendant_stage_keys(stage_key: str) -> tuple[str, ...]:
    """按稳定目录顺序返回阶段的全部后代，不包含自身。"""

    _require_stage_key(stage_key)
    descendants: set[str] = set()
    frontier = [stage_key]

    while frontier:
        parent_key = frontier.pop()
        for candidate_key in STAGE_KEYS:
            if parent_key in STAGE_DEFINITIONS[candidate_key].prerequisite_keys:
                if candidate_key not in descendants:
                    descendants.add(candidate_key)
                    frontier.append(candidate_key)

    return tuple(key for key in STAGE_KEYS if key in descendants)


def get_next_stage_key(stage_key: str) -> str | None:
    """返回下一个 ordinal 阶段的稳定键。

    5a 和 5b 共享同一个 ordinal，因此从任一分支返回 6-delivery；从
    4-storyboard 返回目录中的第一个并行分支 5a-image-prompts。
    """

    definition = _require_stage_key(stage_key)
    for candidate_key in STAGE_KEYS:
        if STAGE_DEFINITIONS[candidate_key].ordinal > definition.ordinal:
            return candidate_key
    return None
