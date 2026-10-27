from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import timedelta
import logging
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, TextContent
from pydantic import ValidationError

from .config import VideoToolSettings
from .errors import DiagnoseError
from .models import VideoToolResult
from .progress import ProgressEvent, ProgressSink
from .video_environment import build_video_environment
from .video_input import VideoInputError, validate_video


STARTUP_TIMEOUT_SECONDS = 30
HEARTBEAT_SECONDS = 10
SOURCE_MESSAGE_ID = "video-check"
QUESTION = "请用中文概括视频画面，并按时间点列出可见的操作、现象和异常；无法确认的内容请明确说明。"


class VideoCheckError(DiagnoseError):
    pass


class _RedactTransportLogs(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = "视频 MCP 传输诊断：已隐藏原始协议数据"
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


@contextmanager
def _sanitized_transport_logs() -> Iterator[None]:
    # The SDK logs raw invalid stdout (including validation input) before the
    # client can redact it. Limit filtering to this transport and this call.
    logger = logging.getLogger("mcp.client.stdio")
    redactor = _RedactTransportLogs()
    logger.addFilter(redactor)
    try:
        yield
    finally:
        logger.removeFilter(redactor)


def _parse_result(result: CallToolResult) -> VideoToolResult:
    text = "\n".join(item.text for item in result.content if isinstance(item, TextContent))
    if result.isError:
        raise VideoCheckError(f"understand_video 执行失败：{text or '未返回错误详情'}")
    try:
        if result.structuredContent is not None:
            parsed = VideoToolResult.model_validate(result.structuredContent)
        else:
            parsed = VideoToolResult.model_validate_json(text)
    except ValidationError:
        raise VideoCheckError("视频工具返回的结果不符合 VideoToolResult，未输出原始响应") from None
    if parsed.source_message_id != SOURCE_MESSAGE_ID:
        raise VideoCheckError("视频工具返回的 source_message_id 不匹配")
    return parsed


def _exception_message(error: BaseException) -> str:
    if isinstance(error, BaseExceptionGroup):
        return "; ".join(dict.fromkeys(_exception_message(item) for item in error.exceptions))
    if isinstance(error, TimeoutError):
        return "操作超时"
    return str(error) or type(error).__name__


async def check_video(
    path: Path,
    settings: VideoToolSettings,
    progress: ProgressSink | None = None,
) -> VideoToolResult:
    """Call the real video MCP once, without Codex, Feishu, or case persistence."""
    source = validate_video(path)

    def emit(name: str, message: str) -> None:
        if progress is not None:
            progress(ProgressEvent("INFO", name, message))

    stage = "准备临时视频"
    provider_stage = "视频上传与理解"

    async def provider_progress(current: float, total: float | None, message: str | None) -> None:
        nonlocal provider_stage
        if message:
            provider_stage = message
            emit("video_check.provider.progress", message)

    async def heartbeat(started: float) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            emit(
                "video_check.heartbeat",
                f"{provider_stage}，已等待 {time.monotonic() - started:.0f} 秒（调用上限 {settings.timeout_seconds:g} 秒）",
            )
    try:
        with tempfile.TemporaryDirectory(prefix="feishu-video-check-") as directory:
            media_root = Path(directory)
            video = media_root / ("video" + source.suffix)
            shutil.copyfile(source, video)
            environment = build_video_environment(
                media_root, model=settings.model, api_key=settings.resolve_api_key(),
                base_url=settings.base_url,
                timeout_seconds=settings.timeout_seconds,
            )
            server = StdioServerParameters(
                command=sys.executable,
                args=["-m", "feishu_chat_diagnose.video_mcp"],
                env=environment,
            )
            stage = "启动视频 MCP 与发现工具"
            emit("video_check.mcp.starting", "正在启动视频 MCP 并发现 understand_video")
            # The server's raw stderr can contain SDK diagnostics and credentials.
            with _sanitized_transport_logs(), open(os.devnull, "w") as errlog:
                async with stdio_client(server, errlog=errlog) as (read, write):
                    async with ClientSession(read, write) as session:
                        async with asyncio.timeout(STARTUP_TIMEOUT_SECONDS):
                            await session.initialize()
                            tools = await session.list_tools()
                        if not any(tool.name == "understand_video" for tool in tools.tools):
                            raise VideoCheckError("视频 MCP 未提供 understand_video 工具")
                        stage = "视频上传与理解"
                        emit(
                            "video_check.tool.calling",
                            f"正在上传并理解视频（模型：{settings.model}）",
                        )
                        heartbeat_task = asyncio.create_task(heartbeat(time.monotonic()))
                        try:
                            # Progress messages must not extend the overall call deadline.
                            async with asyncio.timeout(settings.timeout_seconds):
                                response = await session.call_tool(
                                    "understand_video",
                                    arguments={
                                        "path": str(video.resolve()),
                                        "source_message_id": SOURCE_MESSAGE_ID,
                                        "question": QUESTION,
                                    },
                                    read_timeout_seconds=timedelta(seconds=settings.timeout_seconds),
                                    progress_callback=provider_progress,
                                )
                        finally:
                            heartbeat_task.cancel()
                            with suppress(asyncio.CancelledError):
                                await heartbeat_task
                        stage = "校验视频结果"
                        result = _parse_result(response)
                        stage = "关闭视频 MCP"
        emit("video_check.completed", "视频 MCP 调用完成，结果已通过结构校验，临时副本已清理")
        return result
    except Exception as error:
        detail = f"（最近进度：{provider_stage}）" if stage == "视频上传与理解" else ""
        raise VideoCheckError(f"{stage}失败{detail}：{_exception_message(error)}") from None
