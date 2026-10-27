from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Protocol

from .models import AttachmentRef
from .progress import ProgressEvent, ProgressLevel, ProgressSink
from .video_input import VIDEO_MIME_TYPES, video_suffix_for_media_type


class AttachmentGateway(Protocol):
    def download_attachment(
        self, attachment: AttachmentRef, root: Path, output_name: str
    ) -> Path: ...


@dataclass(slots=True)
class MaterializedAttachment:
    attachment: AttachmentRef
    local_path: Path
    text_content: str = ""


@dataclass(slots=True)
class AttachmentFailure:
    attachment: AttachmentRef
    reason: str


@dataclass(slots=True)
class MaterializedBatch:
    available: list[MaterializedAttachment] = field(default_factory=list)
    failed: list[AttachmentFailure] = field(default_factory=list)


class AttachmentManager:
    def __init__(
        self,
        gateway: AttachmentGateway,
        root: Path,
        max_text_bytes: int,
        progress: ProgressSink | None = None,
    ) -> None:
        self.gateway = gateway
        self.root = root
        self.max_text_bytes = max_text_bytes
        self.progress = progress or (lambda event: None)

    @contextmanager
    def materialize(self, attachments: list[AttachmentRef]) -> Iterator[MaterializedBatch]:
        self.root.mkdir(parents=True, exist_ok=True)
        root = self.root.resolve()
        created: list[Path] = []
        batch = MaterializedBatch()
        self._emit("INFO", "attachments.started", f"开始准备 {len(attachments)} 个附件")
        try:
            for index, attachment in enumerate(attachments, start=1):
                suffix = _safe_suffix(attachment)
                output_name = f"resource-{index:04d}{suffix}"
                try:
                    path = self.gateway.download_attachment(attachment, root, output_name).resolve()
                    if not path.is_relative_to(root):
                        raise ValueError("downloaded attachment escapes temporary root")
                    created.append(path)
                    text_content = self._decode_text(path, attachment)
                    batch.available.append(
                        MaterializedAttachment(attachment, path, text_content)
                    )
                    self._emit(
                        "INFO", "attachment.available",
                        f"第 {index} 个{_kind_label(attachment.kind)}附件已准备",
                    )
                except Exception as error:
                    candidate = (root / output_name).resolve()
                    if candidate.exists() and candidate.is_relative_to(root):
                        created.append(candidate)
                    batch.failed.append(AttachmentFailure(attachment, str(error)))
                    self._emit(
                        "WARNING", "attachment.failed",
                        f"第 {index} 个{_kind_label(attachment.kind)}附件准备失败",
                    )
            self._emit(
                "INFO", "attachments.completed",
                f"附件准备完成：可用 {len(batch.available)}，失败 {len(batch.failed)}",
            )
            yield batch
        finally:
            for path in reversed(created):
                path.unlink(missing_ok=True)
            self._emit("INFO", "attachments.cleanup", "临时附件已清理")

    def _decode_text(self, path: Path, attachment: AttachmentRef) -> str:
        if attachment.kind != "text":
            return ""
        size = path.stat().st_size
        if size > self.max_text_bytes:
            raise ValueError(f"text attachment exceeds {self.max_text_bytes} bytes")
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("text attachment is not valid UTF-8") from error

    def _emit(self, level: ProgressLevel, name: str, message: str) -> None:
        self.progress(ProgressEvent(level, name, message))


def _safe_suffix(attachment: AttachmentRef) -> str:
    suffix = Path(attachment.name).suffix.lower()
    if attachment.kind == "video" and suffix not in VIDEO_MIME_TYPES:
        if inferred := video_suffix_for_media_type(attachment.media_type):
            return inferred
    if suffix and len(suffix) <= 12 and suffix[1:].isalnum():
        return suffix
    return {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "text/plain": ".txt",
        "application/json": ".json",
    }.get(attachment.media_type.lower(), ".bin")


def _kind_label(kind: str) -> str:
    return {"image": "图片", "video": "视频", "text": "文本"}.get(kind, "普通")
