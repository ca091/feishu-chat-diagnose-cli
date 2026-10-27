from __future__ import annotations

from pathlib import Path

from .errors import DiagnoseError


# Conservative compatibility limit: the Base64 string must be strictly below 10 MB.
MAX_ENCODED_VIDEO_BYTES = 10_000_000
VIDEO_MIME_TYPES = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska",
    ".flv": "video/x-flv",
    ".wmv": "video/x-ms-wmv",
    ".webm": "video/webm",
}


def video_suffix_for_media_type(media_type: str) -> str:
    mime = media_type.split(";", 1)[0].strip().lower()
    return next((suffix for suffix, value in VIDEO_MIME_TYPES.items() if value == mime), "")


class VideoInputError(DiagnoseError):
    pass


def validate_video_size(size: int) -> None:
    if size <= 0:
        raise VideoInputError("视频文件不能为空")
    if 4 * ((size + 2) // 3) >= MAX_ENCODED_VIDEO_BYTES:
        raise VideoInputError("视频过大：Base64 编码后须小于 10 MB（原文件约小于 7.5 MB），请先压缩视频")


def validate_video(path: Path) -> Path:
    try:
        canonical = path.expanduser().resolve()
        if not canonical.is_file():
            raise VideoInputError("视频路径必须是已存在的普通文件")
        if canonical.suffix.lower() not in VIDEO_MIME_TYPES:
            raise VideoInputError("不支持的视频扩展名；请使用 MP4、MOV、AVI、MKV、FLV、WMV 或 WebM")
        validate_video_size(canonical.stat().st_size)
        with canonical.open("rb") as handle:
            if not handle.read(1):
                raise VideoInputError("视频文件不能为空")
        return canonical
    except (OSError, ValueError, RuntimeError) as error:
        if isinstance(error, VideoInputError):
            raise
        raise VideoInputError("无法读取视频文件，请检查路径和权限") from None
