"""FFmpeg 异步包装器

将 FFmpeg 调用放到线程池中执行，避免阻塞主线程。
"""

from __future__ import annotations

import asyncio
import functools
import json
import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.services.constants import (
    FFMPEG_DEFAULT_TIMEOUT,
    FFMPEG_EXECUTOR_MAX_WORKERS,
    FFMPEG_SHORT_TIMEOUT,
)

# 线程池用于执行 FFmpeg 任务
_ffmpeg_executor = ThreadPoolExecutor(
    max_workers=FFMPEG_EXECUTOR_MAX_WORKERS,
    thread_name_prefix="ffmpeg",
)


def _run_ffmpeg_sync(
    cmd: list[str],
    timeout: int,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """同步执行 FFmpeg 命令"""
    return subprocess.run(
        cmd,
        check=check,
        timeout=timeout,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )


async def run_ffmpeg_async(
    cmd: list[str],
    timeout: int = FFMPEG_DEFAULT_TIMEOUT,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """
    异步执行 FFmpeg 命令

    Args:
        cmd: FFmpeg 命令（包含完整路径）
        timeout: 超时时间（秒）
        check: 是否检查返回码

    Returns:
        CompletedProcess 对象

    Raises:
        subprocess.CalledProcessError: 命令执行失败
        subprocess.TimeoutExpired: 命令超时
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _ffmpeg_executor,
        lambda: _run_ffmpeg_sync(cmd, timeout, check),
    )


async def run_in_ffmpeg_executor(
    func: Callable[..., Any],
    /,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """
    把同步的 FFmpeg/ffprobe 相关调用放入 FFmpeg 线程池执行。

    供 async 路由 / Job handler 复用，避免同步 subprocess 在事件循环中
    直接执行导致阻塞。monkeypatch 替换后的同步替身同样适用。

    Args:
        func: 同步函数（含实例方法）
        *args: 位置参数
        **kwargs: 关键字参数

    Returns:
        func 的返回值
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _ffmpeg_executor,
        functools.partial(func, *args, **kwargs),
    )


def build_ffmpeg_command(
    *args: str | Path,
    input_file: str | Path | None = None,
    output_file: str | Path | None = None,
    **kwargs: Any,
) -> list[str]:
    """
    构建 FFmpeg 命令

    Args:
        *args: FFmpeg 参数
        input_file: 输入文件（自动添加 -i）
        output_file: 输出文件
        **kwargs: 其他参数（如 vcodec='libx264'）

    Returns:
        命令列表

    Examples:
        >>> build_ffmpeg_command(
        ...     input_file="input.mp4",
        ...     output_file="output.mp4",
        ...     vcodec="libx264",
        ...     crf=23,
        ... )
        ['ffmpeg', '-i', 'input.mp4', '-vcodec', 'libx264', '-crf', '23', 'output.mp4']
    """
    settings = get_settings()
    cmd = [str(settings.ffmpeg_path)]

    if input_file:
        cmd.extend(["-i", str(input_file)])

    cmd.extend(str(arg) for arg in args)

    for key, value in kwargs.items():
        # 将 Python 风格的参数名转换为 FFmpeg 风格
        # 例如 vcodec -> -vcodec
        flag = f"-{key.replace('_', '-')}"
        cmd.append(flag)
        if value is True:
            # 布尔开关：仅追加 flag（如 -y、-overwrite），不追加 "True"
            continue
        if value is not None and value is not False:
            cmd.append(str(value))

    if output_file:
        cmd.append(str(output_file))

    return cmd


def _run_ffprobe_sync(cmd: list[str], timeout: int) -> str:
    """同步执行 ffprobe 并返回标准输出（JSON）。"""
    result = subprocess.run(
        cmd,
        check=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    return result.stdout


async def get_video_duration(file_path: str | Path) -> float:
    """
    获取视频时长（秒）

    通过 ffprobe 读取容器元数据，避免解析 ffmpeg 的 stderr 文本。

    Args:
        file_path: 视频文件路径

    Returns:
        视频时长（秒）

    Raises:
        ValueError: ffprobe 执行失败或无法解析时长
    """
    settings = get_settings()
    cmd = [
        str(settings.ffprobe_path),
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(file_path),
    ]

    loop = asyncio.get_running_loop()
    try:
        stdout = await loop.run_in_executor(
            _ffmpeg_executor,
            lambda: _run_ffprobe_sync(cmd, FFMPEG_SHORT_TIMEOUT),
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr or ""
        raise ValueError(f"ffprobe 执行失败：{stderr[:200]}") from exc
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise ValueError(f"获取视频时长失败：{exc}") from exc

    try:
        duration = float(json.loads(stdout)["format"]["duration"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("无法解析视频时长：ffprobe 输出无效") from exc
    return duration


async def has_audio_stream(file_path: str | Path) -> bool:
    """探测文件是否包含音频流（BGM 上传时防止视频/图片文件冒充音频）。"""
    settings = get_settings()
    cmd = [
        str(settings.ffprobe_path),
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type",
        "-of",
        "json",
        str(file_path),
    ]
    loop = asyncio.get_running_loop()
    try:
        stdout = await loop.run_in_executor(
            _ffmpeg_executor,
            lambda: _run_ffprobe_sync(cmd, FFMPEG_SHORT_TIMEOUT),
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr or ""
        raise ValueError(f"ffprobe 执行失败：{stderr[:200]}") from exc
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise ValueError(f"音频流探测失败：{exc}") from exc
    try:
        streams = json.loads(stdout).get("streams") or []
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("音频流探测失败：ffprobe 输出无效") from exc
    return any(
        isinstance(stream, dict) and str(stream.get("codec_type", "")) == "audio"
        for stream in streams
    )


def shutdown_ffmpeg_executor() -> None:
    """关闭 FFmpeg 线程池（应用退出时调用）"""
    _ffmpeg_executor.shutdown(wait=True)
