"""
Edge TTS Provider

使用微软 Edge 的 TTS 服务，完全免费。
无需 API Key，直接可用。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.providers.base import TTSParams

try:
    import edge_tts
    EDGE_TTS_AVAILABLE = True
except ImportError:
    EDGE_TTS_AVAILABLE = False


async def _generate_tts(text: str, voice: str, output_path: Path) -> None:
    """异步生成 TTS 音频"""
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(str(output_path))


def _ensure_no_running_loop() -> None:
    """已有运行事件循环时给出明确报错，避免误用同步包装。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError("当前已在事件循环内运行，请直接 await 对应的异步函数")


def generate(params: TTSParams) -> Path:
    """
    生成 TTS 音频

    Args:
        params: TTS 参数，包含 text（文本）和 voice（声音 ID）

    Returns:
        生成的音频文件路径
    """
    if not EDGE_TTS_AVAILABLE:
        raise RuntimeError(
            "Edge TTS 未安装。请运行: pip install edge-tts"
        )

    text = params.get("text", "")
    voice = params.get("voice", "zh-CN-XiaoxiaoNeural")
    output_path = params.get("output_path")

    if not text:
        raise ValueError("text 参数不能为空")
    if not output_path:
        raise ValueError("output_path 参数不能为空")

    # 使用 asyncio 运行异步函数
    _ensure_no_running_loop()
    asyncio.run(_generate_tts(text, voice, output_path))

    return output_path


async def list_voices() -> list[dict[str, str]]:
    """
    获取所有可用的声音列表

    Returns:
        声音列表，每个元素包含 Name, ShortName, Gender, Locale 等信息
    """
    if not EDGE_TTS_AVAILABLE:
        return []

    voices = await edge_tts.list_voices()
    # 只返回中文声音
    return [
        {
            "name": v["ShortName"],
            "display_name": v["FriendlyName"],
            "gender": v["Gender"],
            "locale": v["Locale"],
        }
        for v in voices
        if v["Locale"].startswith("zh-")
    ]


def list_voices_sync() -> list[dict[str, str]]:
    """
    同步版本：获取所有可用的中文声音列表

    Returns:
        声音列表
    """
    _ensure_no_running_loop()
    return asyncio.run(list_voices())


# 预定义的常用中文声音
COMMON_VOICES = [
    {
        "name": "zh-CN-XiaoxiaoNeural",
        "display_name": "晓晓 - 温柔女声",
        "gender": "Female",
        "description": "温柔、亲切，适合旁白、温柔女性角色",
    },
    {
        "name": "zh-CN-XiaoyiNeural",
        "display_name": "晓伊 - 活泼女声",
        "gender": "Female",
        "description": "活泼、可爱，适合少女、活力角色",
    },
    {
        "name": "zh-CN-YunjianNeural",
        "display_name": "云健 - 沉稳男声",
        "gender": "Male",
        "description": "沉稳、成熟，适合成熟男性、权威角色",
    },
    {
        "name": "zh-CN-YunxiNeural",
        "display_name": "云希 - 阳光男声",
        "gender": "Male",
        "description": "阳光、开朗，适合年轻男性、活力角色",
    },
    {
        "name": "zh-CN-YunxiaNeural",
        "display_name": "云霞 - 知性男声",
        "gender": "Male",
        "description": "知性、专业，适合解说、正式场合",
    },
    {
        "name": "zh-CN-YunyangNeural",
        "display_name": "云扬 - 专业男声",
        "gender": "Male",
        "description": "专业、稳重，适合新闻播报、正式场合",
    },
    {
        "name": "zh-CN-liaoning-XiaobeiNeural",
        "display_name": "晓北 - 东北女声",
        "gender": "Female",
        "description": "东北口音，活泼、幽默",
    },
    {
        "name": "zh-CN-shaanxi-XiaoniNeural",
        "display_name": "晓妮 - 陕西女声",
        "gender": "Female",
        "description": "陕西口音，朴实、亲切",
    },
    {
        "name": "zh-HK-HiuMaanNeural",
        "display_name": "曉曼 - 粤语女声",
        "gender": "Female",
        "description": "粤语，温柔、优雅",
    },
    {
        "name": "zh-HK-WanLungNeural",
        "display_name": "雲龍 - 粤语男声",
        "gender": "Male",
        "description": "粤语，沉稳、成熟",
    },
    {
        "name": "zh-TW-HsiaoChenNeural",
        "display_name": "曉臻 - 台湾女声",
        "gender": "Female",
        "description": "台湾口音，温柔、亲切",
    },
    {
        "name": "zh-TW-YunJheNeural",
        "display_name": "雲哲 - 台湾男声",
        "gender": "Male",
        "description": "台湾口音，稳重、专业",
    },
]
