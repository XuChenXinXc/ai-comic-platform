"""FFmpeg 异步包装器

将 FFmpeg 调用放到线程池中执行，避免阻塞主线程。
"""

from __future__ import annotations

import asyncio
import subprocess
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
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        _ffmpeg_executor,
        lambda: _run_ffmpeg_sync(cmd, timeout, check),
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
        if value is not None:
            cmd.append(str(value))

    if output_file:
        cmd.append(str(output_file))

    return cmd


async def get_video_duration(file_path: str | Path) -> float:
    """
    获取视频时长（秒）

    Args:
        file_path: 视频文件路径

    Returns:
        视频时长（秒）

    Raises:
        ValueError: 无法获取视频时长
    """
    settings = get_settings()
    cmd = [
        str(settings.ffmpeg_path),
        "-i", str(file_path),
    ]

    try:
        result = await run_ffmpeg_async(cmd, timeout=FFMPEG_SHORT_TIMEOUT, check=False)
        stderr = result.stderr

        # 从 stderr 中解析时长
        # Duration: 00:00:10.50, ...
        import re
        match = re.search(r"Duration: (\d{2}):(\d{2}):(\d{2}\.\d{2})", stderr)
        if match:
            hours, minutes, seconds = match.groups()
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)

        raise ValueError(f"无法解析视频时长：{stderr[:200]}")

    except subprocess.CalledProcessError as exc:
        raise ValueError(f"FFmpeg 执行失败：{exc.stderr[:200]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError("获取视频时长超时") from exc


def shutdown_ffmpeg_executor() -> None:
    """关闭 FFmpeg 线程池（应用退出时调用）"""
    _ffmpeg_executor.shutdown(wait=True)
