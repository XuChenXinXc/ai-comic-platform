"""批量连续性精修（batch_refine_prompts）格式与校验回归测试。

背景：视频提示词已自然语言化（无【】段、无机械时间轴），批量精修模板必须
按新格式工作，且精修结果在写入 override 前必须通过引用一致性与自然语言格式校验。
"""

import asyncio
from typing import Any

from media_test_helpers import _shot_create

from app.services.shot_service import ShotService


def _create_two_shots(media_job_services: dict[str, Any]) -> ShotService:
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    chapter = media_job_services["chapter"]
    scene = media_job_services["scene"]
    shots = ShotService(database, jobs)
    for index in range(2):
        shots.create(
            chapter["id"],
            _shot_create(
                scene_asset_id=scene["id"],
                action=f"镜头{index + 1}动作",
                video_prompt=f"镜头{index + 1}人物向前走。",
            ).model_dump(),
        )
    return shots


def test_refine_block_issues_rejects_template_and_lost_references() -> None:
    original = (
        "以 @Image1 作为这一镜的起始画面。人物向前走，随后停下。\n"
        "Negative: 画面中不出现：文字。\nMotion style lock: 保持二维绘画属性。"
    )
    kept = (
        "以 @Image1 作为这一镜的起始画面。人物向前走，随后停下。\n"
        "Negative: 画面中不出现：文字。\nMotion style lock: 保持二维绘画属性。"
    )
    block = [{"shot_no": 1, "final_video_prompt": original}]
    assert ShotService._refine_block_issues(block, [kept], "final_video_prompt", "video") == {}

    # 丢失 @Image 引用 → 拒绝
    lost_reference = "人物向前走，随后停下。"
    issues = ShotService._refine_block_issues(
        block, [lost_reference], "final_video_prompt", "video"
    )
    assert any("丢失引用" in problem for problem in issues[0])

    # 回退成旧模板【】段 → 拒绝
    templated = "【核心动作】\n本镜只执行以下唯一一套时间轴，0-2秒：人物向前走。"
    issues = ShotService._refine_block_issues(
        block, [templated], "final_video_prompt", "video"
    )
    assert any("【】" in problem for problem in issues[0])


def test_refine_image_keeps_sectioned_format_without_breaking_references() -> None:
    original = (
        "【参考】\n@Image1：角色林风，锁定角色身份与服装。\n"
        "【剧情时刻】\n林风站在山门石阶中央。\n【成图限制】\n不做海报。"
    )
    refined = (
        "【参考】\n@Image1：角色林风，锁定角色身份与服装。\n"
        "【剧情时刻】\n林风站在山门石阶中央。\n【连续性要求】\n衔接上一镜站位。\n"
        "【成图限制】\n不做海报。"
    )
    block = [{"shot_no": 1, "final_image_prompt": original}]
    # 图片提示词保留【】段落是合法格式；只校验引用不丢
    assert (
        ShotService._refine_block_issues(block, [refined], "final_image_prompt", "image")
        == {}
    )


def test_refine_one_type_retries_invalid_block_before_writing(
    media_job_services: dict[str, Any],
    monkeypatch: Any,
) -> None:
    project = media_job_services["project"]
    shots = _create_two_shots(media_job_services)
    listed = shots.list_project(project["id"])

    calls: list[str] = []

    class FakeRefineProvider:
        async def generate_json(
            self,
            *,
            system_prompt: str,
            user_prompt: str,
            schema: type[Any],
            params: dict[str, Any],
        ) -> Any:
            calls.append(user_prompt)
            if "上一次输出未通过校验" not in user_prompt:
                # 第一次返回旧模板格式（引用也丢失），触发重试
                return schema.model_validate(
                    {
                        "prompts": [
                            "【核心动作】本镜只执行唯一时间轴",
                            "【核心动作】本镜只执行唯一时间轴",
                        ]
                    }
                )
            # 重试返回原提示词全文（引用与自然格式都保留）
            return schema.model_validate(
                {
                    "prompts": [item["final_video_prompt"] for item in listed],
                    "changes": [],
                }
            )

    monkeypatch.setattr(
        shots.providers,
        "resolve",
        lambda **_: (
            FakeRefineProvider(),
            {"id": "mock-text", "model": "", "params": {}, "timeout_sec": 60},
        ),
    )
    queued = shots.queue_refine_prompts(project["id"], {"prompt_types": ["video"]})
    result = asyncio.run(
        shots._refine_one_type(
            queued,
            project,
            "video",
            chunk_size=6,
            params={},
            progress_offset=0,
            progress_total=1,
        )
    )
    assert len(calls) == 2
    assert result["applied"] == 2
    assert result["skipped"] == 0
    refreshed = shots.list_project(project["id"])
    assert all(str(item.get("video_prompt_override") or "").strip() for item in refreshed)
