from __future__ import annotations

import os
from pathlib import Path


VIDEO_ENV_NAMES = (
    "DASHSCOPE_API_KEY",
    "FEISHU_DIAGNOSE_VIDEO_BASE_URL",
    "FEISHU_DIAGNOSE_MEDIA_ROOT",
    "FEISHU_DIAGNOSE_VIDEO_MODEL",
    "FEISHU_DIAGNOSE_VIDEO_TIMEOUT",
)


def build_video_environment(
    media_root: Path,
    *,
    model: str,
    base_url: str = "",
    api_key: str = "",
    timeout_seconds: float = 300,
) -> dict[str, str]:
    """Build a child-only environment shared by diagnosis and video self-checks."""
    environment = os.environ.copy()
    if key := environment.get("DASHSCOPE_API_KEY") or api_key:
        environment["DASHSCOPE_API_KEY"] = key
    environment.update({
        "FEISHU_DIAGNOSE_VIDEO_BASE_URL": base_url,
        "FEISHU_DIAGNOSE_MEDIA_ROOT": str(media_root.resolve()),
        "FEISHU_DIAGNOSE_VIDEO_MODEL": model,
        "FEISHU_DIAGNOSE_VIDEO_TIMEOUT": str(timeout_seconds),
    })
    return environment
