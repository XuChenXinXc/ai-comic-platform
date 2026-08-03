from __future__ import annotations

import asyncio
import base64
import json
import subprocess
import threading
import time
from io import BytesIO
from pathlib import Path
from typing import Any, Literal, get_args, get_origin
from uuid import uuid4

from PIL import Image, ImageDraw
from pydantic import BaseModel

from app.config import get_settings
from app.schemas import (
    AssetExtractionOutput,
    AssetPromptOutput,
    NovelAnalysisOutput,
    ProviderConfig,
    ProviderPollResult,
    ProviderSubmission,
    ScreenplayOutput,
    ShotExtractionOutput,
    ShotImagePromptOutput,
    ShotVideoPromptOptimizeOutput,
    ShotVideoPromptOutput,
    VisualStyleOutput,
)
from app.services.file_service import atomic_copy, atomic_write_bytes

from .base import ProviderError


class MockTextProvider:
    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    async def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: type[BaseModel],
        params: dict[str, Any],
    ) -> BaseModel:
        delay = float(params.get("delay_sec", self.config.params.get("delay_sec", 0)))
        if delay > 0:
            await asyncio.sleep(delay)
        payload = self._payload(schema, user_prompt)
        return schema.model_validate(payload)

    def _payload(self, schema: type[BaseModel], user_prompt: str) -> dict[str, Any]:
        excerpt = " ".join(user_prompt.split())[:80] or "一段待创作的故事"
        if schema is NovelAnalysisOutput:
            return {
                "summary": f"故事围绕“{excerpt}”展开。",
                "genre": "剧情",
                "time_period": "当代",
                "locations": ["主要场景"],
                "worldview": "现实世界",
                "main_characters": ["主角"],
                "character_relations": [{"from": "主角", "to": "目标", "relation": "追寻"}],
                "core_conflict": "主角必须克服阻碍完成目标。",
                "plot_structure": {
                    "beginning": "目标出现",
                    "middle": "冲突升级",
                    "end": "作出选择",
                },
                "emotion_curve": [{"stage": "开端", "emotion": "平静"}],
                "foreshadowing": ["一个被忽略的细节"],
                "visualizable_content": ["主角进入主要场景"],
                "hard_to_visualize": ["内心独白"],
                "production_risks": [],
            }
        if schema is VisualStyleOutput:
            return {"style_prompt": "冷峻悬疑二维国漫，利落轮廓，低饱和冷色，分层暗光"}
        if schema is ScreenplayOutput:
            return {
                "title": "Mock 漫剧",
                "logline": "主角在一次选择中改变了自己的处境。",
                "scenes": [
                    {
                        "scene_no": 1,
                        "scene_name": "主要场景",
                        "time_weather": "白天，晴",
                        "characters": ["主角"],
                        "objective": "找到目标",
                        "action": "主角走入场景并观察四周。",
                        "dialogue": [{"character": "主角", "text": "就是这里。"}],
                        "narration": [],
                        "emotion": "谨慎",
                        "transition_to_next": "主角发现线索。",
                    }
                ],
            }
        if schema is AssetExtractionOutput:
            return {
                "characters": [
                    {
                        "name": "主角",
                        "identity": "故事主角",
                        "concept": "谨慎的线索追寻者",
                        "worldview": "当代现实世界",
                        "appearance": "清晰的面部轮廓",
                        "hair": "黑色短发",
                        "eyes": "深棕色眼睛",
                        "normal_expression": "谨慎而专注",
                        "costume": "深色外套",
                        "main_color": "深灰色",
                        "accent_color": "暗红色",
                        "costume_material": "哑光棉质",
                        "immutable_features": ["黑色短发", "深色外套"],
                        "expression_group": ["谨慎", "惊讶", "坚定"],
                        "prompt": "清晰面部轮廓，黑色短发，深色外套，谨慎而专注",
                        "negative_prompt": "多人，身份漂移，服装变化，文字，水印",
                        "consistency_keywords": ["黑色短发", "深色外套"],
                        "composition": "纯白背景横向角色设定面板",
                        "importance": "core",
                    }
                ],
                "scenes": [
                    {
                        "name": "主要场景",
                        "indoor_outdoor": "室内",
                        "worldview": "当代现实世界",
                        "plot_function": "承载主角发现线索的剧情",
                        "lighting": "柔和侧光",
                        "layout": "前景入口，中景人物，远景窗户",
                        "furnishings": ["旧木桌", "高窗"],
                        "prompt": "入口、旧木桌和高窗位置固定，柔和侧光从高窗进入",
                        "negative_prompt": "人物，空间结构矛盾，家具漂移，文字，水印",
                        "consistency_keywords": ["入口位置", "旧木桌", "高窗"],
                        "composition": "主透视图、双向全景、俯视平面图和细节区",
                        "importance": "core",
                    }
                ],
                "props": [
                    {
                        "name": "关键物品",
                        "description": "推动剧情的随身物品",
                        "worldview": "当代现实世界",
                        "category": "线索道具",
                        "owner": "主角",
                        "silhouette": "小型圆形轮廓",
                        "dimensions": "掌心大小",
                        "plot_function": "提供线索",
                        "material": "做旧金属",
                        "color": "暗金色",
                        "special_features": ["表面刻纹"],
                        "prompt": "掌心大小的暗金色圆形物品，做旧金属表面带固定刻纹",
                        "negative_prompt": "完整人物，多件物品，结构错误，文字，水印",
                        "consistency_keywords": ["圆形轮廓", "暗金色", "固定刻纹"],
                        "composition": "纯白背景横向道具设定面板",
                        "importance": "normal",
                    }
                ],
            }
        if schema is AssetPromptOutput:
            return {
                "prompt": "主体正面设定图，结构清晰，细节稳定",
                "negative_prompt": "模糊，畸形，多余肢体，文字，水印",
                "consistency_keywords": ["固定轮廓", "固定配色", "固定服装"],
                "composition": "居中全身，纯净背景",
            }
        if schema is ShotExtractionOutput:
            catalog: list[dict[str, Any]] = []
            marker = "当前项目可用素材目录：\n"
            if marker in user_prompt:
                catalog_text = user_prompt.split(marker, 1)[1].split("\n来源内容：", 1)[0]
                try:
                    parsed_catalog = json.loads(catalog_text)
                    if isinstance(parsed_catalog, list):
                        catalog = [item for item in parsed_catalog if isinstance(item, dict)]
                except json.JSONDecodeError:
                    catalog = []

            def catalog_id(asset_type: str, name: str, fallback: str) -> str:
                candidate = next(
                    (
                        item
                        for item in catalog
                        if item.get("asset_type") == asset_type
                        and (
                            item.get("name") == name
                            or name
                            in (
                                item.get("aliases", [])
                                if isinstance(item.get("aliases"), list)
                                else []
                            )
                        )
                    ),
                    None,
                )
                return str(candidate.get("id")) if candidate and candidate.get("id") else fallback

            return {
                "shots": [
                    {
                        "shot_no": 1,
                        "scene_no": 1,
                        "source_ref": "开场",
                        "scene_name": "主要场景",
                        "scene_asset_id": catalog_id("scene", "主要场景", "mock-scene-id"),
                        "character_names": ["主角"],
                        "character_asset_ids": [
                            catalog_id("character", "主角", "mock-character-id")
                        ],
                        "prop_names": [],
                        "prop_asset_ids": [],
                        "positions": [{"character": "主角", "position": "画面左侧"}],
                        "start_state": "主角站在入口",
                        "action": "主角向前走两步并停下观察。",
                        "expressions": [{"character": "主角", "expression": "谨慎"}],
                        "dialogue": [],
                        "narration": "",
                        "shot_type": "中景",
                        "camera_angle": "平视",
                        "camera_move": "缓慢推进",
                        "duration_sec": 6,
                        "end_state": "主角停在画面中央",
                        "next_hook": "视线转向画外",
                        "frame_description": "主角位于场景中央，侧光勾勒轮廓。",
                        "image_prompt": (
                            "中景平视，主角站在入口，动作发生前的稳定起始状态，"
                            "侧光勾勒轮廓，前方留出移动空间。"
                        ),
                        "video_prompt": (
                            "0-2秒：主角向前走两步，镜头缓慢推进；"
                            "2-5秒：主角停下谨慎观察；"
                            "5-6秒：主角视线转向画外，末0.5秒保持稳定。"
                        ),
                        "negative_prompt": (
                            "身份漂移，服装变化，新增人物，左右位置交换，"
                            "肢体畸形，镜头抖动，文字，水印"
                        ),
                    }
                ]
            }
        if schema is ShotImagePromptOutput:
            return {
                "prompt": "中景，平视，主角位于画面中央，侧光，动作起始状态清晰",
                "negative_prompt": "身份漂移，服装变化，多余人物，文字，水印",
                "reference_requirements": [{"role": "character_identity", "name": "主角"}],
            }
        if schema is ShotVideoPromptOutput:
            return {
                "prompt": "0-2秒主角向前走；2-4秒停下观察；4-6秒视线转向画外，镜头缓慢推进。",
                "negative_prompt": "新增人物，服装漂移，肢体变形，镜头抖动",
                "timeline": [
                    {"start_sec": 0, "end_sec": 2, "action": "主角向前走"},
                    {"start_sec": 2, "end_sec": 6, "action": "停下并观察"},
                ],
                "continuity_notes": ["保持角色身份、服装和画面方向不变"],
            }
        if schema is ShotVideoPromptOptimizeOutput:
            return {
                "prompt": (
                    "0-2秒：主角向前走；2-4秒：停下观察；4-5秒：视线转向画外。"
                    "固定机位，画面变化由人物动作推动。"
                ),
            }
        return self._generic_model_payload(schema)

    def _generic_model_payload(self, schema: type[BaseModel]) -> dict[str, Any]:
        return {
            name: self._generic_value(field.annotation, name)
            for name, field in schema.model_fields.items()
            if field.is_required()
        }

    def _generic_value(self, annotation: Any, name: str) -> Any:
        origin = get_origin(annotation)
        args = get_args(annotation)
        if origin is Literal:
            return args[0]
        if origin is list:
            return []
        if origin is dict:
            return {}
        if origin is not None and type(None) in args:
            return None
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            return self._generic_model_payload(annotation)
        if annotation is str:
            return name
        if annotation is int:
            return 1
        if annotation is float:
            return 1.0
        if annotation is bool:
            return False
        return {}


class MockImageProvider:
    def __init__(self, config: ProviderConfig) -> None:
        self.config = config
        self._results: dict[str, str] = {}

    async def submit(
        self,
        *,
        prompt: str,
        negative_prompt: str,
        reference_files: list[Path],
        params: dict[str, Any],
    ) -> ProviderSubmission:
        width = max(64, min(int(params.get("width", 768)), 2048))
        height = max(64, min(int(params.get("height", 432)), 2048))
        task_id = f"mock-image-{uuid4().hex}"
        encoded = await asyncio.to_thread(self._render_png, prompt, width, height)
        self._results[task_id] = encoded
        return ProviderSubmission(
            task_id=task_id,
            status="completed",
            result={"content_base64": encoded},
        )

    async def poll(self, task_id: str) -> ProviderPollResult:
        encoded = self._results.get(task_id)
        if encoded is None:
            return ProviderPollResult(task_id=task_id, status="failed", error="Mock 图片任务不存在")
        return ProviderPollResult(
            task_id=task_id,
            status="completed",
            content_base64=encoded,
        )

    async def download(self, result: ProviderPollResult, target: Path) -> Path:
        if not result.content_base64:
            raise ProviderError("Mock 图片结果为空")
        content = base64.b64decode(result.content_base64, validate=True)
        return await asyncio.to_thread(atomic_write_bytes, target, content)

    @staticmethod
    def _render_png(prompt: str, width: int, height: int) -> str:
        digest = sum(prompt.encode("utf-8")) % 255
        image = Image.new("RGB", (width, height), (36 + digest // 4, 48, 58 + digest // 3))
        draw = ImageDraw.Draw(image)
        for index in range(8):
            inset = index * min(width, height) // 24
            color = (
                (232 + digest + index * 7) % 255,
                (97 + index * 13) % 255,
                (58 + digest // 2) % 255,
            )
            draw.rectangle(
                (inset, inset, width - inset - 1, height - inset - 1),
                outline=color,
                width=max(1, min(width, height) // 180),
            )
        output = BytesIO()
        image.save(output, format="PNG")
        return base64.b64encode(output.getvalue()).decode("ascii")


class MockVideoProvider:
    def __init__(self, config: ProviderConfig) -> None:
        self.config = config
        settings = get_settings()
        configured_store = config.params.get("task_store_dir")
        store = (
            Path(configured_store)
            if configured_store
            else settings.workspace_dir / "provider_tasks"
        )
        if not store.is_absolute():
            store = settings.workspace_dir / store
        self.task_dir = (store / "mock_video").resolve()
        self.result_dir = self.task_dir / "results"
        self.temp_dir = self.task_dir / "temp"
        self.task_dir.mkdir(parents=True, exist_ok=True)
        self.result_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.ffmpeg_path = str(config.params.get("ffmpeg_path", settings.ffmpeg_path))
        self._lock = threading.Lock()

    async def prepare_input_image(self, local_file: Path) -> dict[str, Any]:
        if not local_file.is_file() or local_file.stat().st_size <= 0:
            raise ProviderError("Mock 视频输入关键帧不存在或为空")
        return {"local_file": str(local_file.resolve())}

    async def submit(
        self,
        *,
        prompt: str,
        input_image: dict[str, Any],
        duration_sec: float,
        params: dict[str, Any],
    ) -> ProviderSubmission:
        if duration_sec <= 0:
            raise ProviderError("视频时长必须大于 0")
        task_id = f"mock-video-{uuid4().hex}"
        delay = max(0.0, float(params.get("delay_sec", self.config.params.get("delay_sec", 0.1))))
        record = {
            "task_id": task_id,
            "status": "waiting_provider",
            "prompt": prompt,
            "input_image": input_image,
            "duration_sec": duration_sec,
            "created_at": time.time(),
            "ready_at": time.time() + delay,
            "result_file": "",
            "error": "",
        }
        await asyncio.to_thread(self._write_task, task_id, record)
        return ProviderSubmission(task_id=task_id, status="waiting_provider")

    async def poll(self, task_id: str) -> ProviderPollResult:
        return await asyncio.to_thread(self._poll_sync, task_id)

    async def download(self, result: ProviderPollResult, target: Path) -> Path:
        local_file = result.data.get("local_file")
        if not local_file:
            raise ProviderError("Mock 视频结果缺少本地文件")
        return await asyncio.to_thread(atomic_copy, local_file, target)

    def _task_path(self, task_id: str) -> Path:
        if not task_id.startswith("mock-video-") or any(char in task_id for char in "\\/"):
            raise ProviderError("Mock 视频 task_id 无效")
        return self.task_dir / f"{task_id}.json"

    def _write_task(self, task_id: str, record: dict[str, Any]) -> None:
        content = json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")
        atomic_write_bytes(self._task_path(task_id), content, temp_dir=self.temp_dir)

    def _read_task(self, task_id: str) -> dict[str, Any] | None:
        path = self._task_path(task_id)
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise ProviderError("Mock 视频任务记录损坏") from exc
        return value if isinstance(value, dict) else None

    def _poll_sync(self, task_id: str) -> ProviderPollResult:
        with self._lock:
            record = self._read_task(task_id)
            if record is None:
                return ProviderPollResult(
                    task_id=task_id,
                    status="failed",
                    error="Mock 视频任务不存在",
                )
            if record["status"] == "failed":
                return ProviderPollResult(
                    task_id=task_id,
                    status="failed",
                    error=record.get("error", "Mock 视频任务失败"),
                )
            result_file = Path(record.get("result_file") or "")
            if record["status"] == "completed" and result_file.is_file():
                return ProviderPollResult(
                    task_id=task_id,
                    status="completed",
                    data={"local_file": str(result_file)},
                )
            if time.time() < float(record["ready_at"]):
                return ProviderPollResult(task_id=task_id, status="waiting_provider")
            try:
                result_file = self._render_video(record)
            except Exception as exc:
                record["status"] = "failed"
                record["error"] = str(exc)
                self._write_task(task_id, record)
                return ProviderPollResult(task_id=task_id, status="failed", error=str(exc))
            record["status"] = "completed"
            record["result_file"] = str(result_file)
            self._write_task(task_id, record)
            return ProviderPollResult(
                task_id=task_id,
                status="completed",
                data={"local_file": str(result_file)},
            )

    def _render_video(self, record: dict[str, Any]) -> Path:
        task_id = record["task_id"]
        target = self.result_dir / f"{task_id}.mp4"
        temporary = self.temp_dir / f"{uuid4().hex}.part.mp4"
        duration = max(0.1, float(record["duration_sec"]))
        input_path = Path(record.get("input_image", {}).get("local_file", ""))
        if input_path.is_file():
            input_args = ["-loop", "1", "-i", str(input_path)]
        else:
            input_args = ["-f", "lavfi", "-i", "color=c=0x26313a:s=640x360:r=24"]
        command = [
            self.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            *input_args,
            "-t",
            f"{duration:.3f}",
            "-vf",
            "scale=640:360:force_original_aspect_ratio=decrease,"
            "pad=640:360:(ow-iw)/2:(oh-ih)/2,format=yuv420p",
            "-r",
            "24",
            "-c:v",
            "libx264",
            "-movflags",
            "+faststart",
            str(temporary),
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=max(30, int(duration * 5)),
            )
            if completed.returncode != 0:
                error = completed.stderr.strip()[-1000:]
                raise ProviderError(f"FFmpeg 生成 Mock 视频失败：{error}")
            if not temporary.is_file() or temporary.stat().st_size <= 0:
                raise ProviderError("FFmpeg 未生成有效视频")
            temporary.replace(target)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ProviderError(f"无法执行 FFmpeg：{exc}") from exc
        finally:
            temporary.unlink(missing_ok=True)
        return target
