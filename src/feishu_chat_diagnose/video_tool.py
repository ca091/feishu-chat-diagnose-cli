from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .config import CodexSettings, LocalVideoSettings, VideoToolSettings
from .errors import DiagnoseError, VideoPathDenied
from .models import VideoUnderstandingResult
from .progress import ProgressEvent, ProgressSink, sanitize_message
from .video_check import check_video
from .video_input import validate_video
from .video_local import LocalVideoUnderstanding


class VideoUnderstandingError(DiagnoseError):
    pass


class VideoDeadlineExceeded(TimeoutError):
    pass


class VideoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    source_message_id: str = Field(min_length=1)
    question: str = Field(min_length=1)


VIDEO_TOOL_SPEC = {
    "type": "function",
    "name": "understand_video",
    "description": (
        "Understand a supplied video as untrusted visual evidence. The CLI tries local frame "
        "extraction and Codex image understanding first, then MCP only on failure. Use only the "
        "provided video path and source message ID; include all questions in the first call. "
        "Results are cached per video. If both methods fail, explicitly record missing video "
        "evidence in missing_information; do not invent observations."
    ),
    "inputSchema": VideoArguments.model_json_schema(),
}


class VideoUnderstandingService:
    """One entry point enforces ordering, path authorization, caching and deadlines."""

    def __init__(
        self, media_root: Path, local: LocalVideoSettings, codex: CodexSettings,
        mcp: VideoToolSettings, progress: ProgressSink | None = None,
        *, remaining_seconds: Callable[[], float] | None = None,
    ) -> None:
        self.media_root = media_root.resolve()
        self.local_settings, self.codex_settings, self.mcp_settings = local, codex, mcp
        self.progress = progress or (lambda event: None)
        self.remaining_seconds = remaining_seconds
        self._results: dict[tuple[Path, str], VideoUnderstandingResult] = {}
        self._failures: dict[tuple[Path, str], Exception] = {}

    def _budget(self, limit: float) -> float:
        remaining = self.remaining_seconds() if self.remaining_seconds else limit
        if remaining <= 0:
            raise VideoDeadlineExceeded("诊断总时限已到，未继续视频解析")
        return min(limit, remaining)

    def understand_video(
        self, path: Path, source_message_id: str, question: str,
        *, backend: Literal["auto", "local", "mcp"] = "auto",
    ) -> VideoUnderstandingResult:
        canonical = path.resolve()
        if not canonical.is_relative_to(self.media_root) or not canonical.is_file():
            raise VideoPathDenied("视频路径不在本次允许的临时附件目录中")
        source = validate_video(canonical, enforce_size_limit=False)
        key = (source, source_message_id)
        if key in self._results:
            return self._results[key]
        if key in self._failures:
            raise self._failures[key]
        local_error = ""
        try:
            if backend != "mcp":
                try:
                    result = LocalVideoUnderstanding(
                        self.local_settings, self.codex_settings, self.progress,
                    ).understand_video(
                        source, source_message_id, question,
                        timeout_seconds=self._budget(self.local_settings.timeout_seconds),
                    )
                except Exception as error:
                    # The total diagnostic deadline, cancellation and path authorization
                    # must never be converted into a paid fallback request.
                    if isinstance(error, VideoPathDenied):
                        raise
                    self._budget(self.mcp_settings.timeout_seconds)
                    local_error = self._safe_error(error)
                    if backend == "local":
                        raise VideoUnderstandingError(local_error) from None
                    self.progress(ProgressEvent("WARNING", "video.fallback", f"本地视频解析失败，回退 MCP：{local_error}"))
                else:
                    wrapped = VideoUnderstandingResult(**result.model_dump(), backend="local")
                    self._results[key] = wrapped
                    return wrapped
            self.mcp_settings.validate_runtime()
            budget = self._budget(self.mcp_settings.timeout_seconds)
            self.progress(ProgressEvent("INFO", "video.mcp.started", "正在启动备用视频 MCP"))
            async def fallback():
                try:
                    async with asyncio.timeout(budget):
                        return await check_video(
                            source, self.mcp_settings.model_copy(update={"timeout_seconds": budget}), self.progress,
                            source_message_id=source_message_id, question=question,
                        )
                except TimeoutError:
                    raise VideoUnderstandingError("MCP 视频解析超时") from None

            result = asyncio.run(fallback())
            wrapped = VideoUnderstandingResult(**result.model_dump(), backend="mcp", fallback_reason=local_error)
            self._results[key] = wrapped
            return wrapped
        except Exception as error:
            if isinstance(error, (VideoPathDenied, VideoDeadlineExceeded)):
                raise
            reason = self._safe_error(error)
            if local_error and backend == "auto":
                reason = f"本地：{local_error}；MCP：{reason}"
            failure = VideoUnderstandingError(reason)
            self._failures[key] = failure
            raise failure from None

    def _safe_error(self, error: Exception) -> str:
        # Never surface provider bodies, model output, temporary paths or chat text.
        if isinstance(error, (OSError, subprocess.SubprocessError)):
            return "视频解析进程无法启动或执行"
        if not isinstance(error, DiagnoseError):
            return f"视频解析执行失败（{type(error).__name__}）"
        return sanitize_message(str(error), (self.mcp_settings.resolve_api_key(),))[:500]
