from pathlib import Path

import pytest

from feishu_chat_diagnose.attachments import AttachmentManager
from feishu_chat_diagnose.models import AttachmentRef


class FakeGateway:
    def __init__(self) -> None:
        self.fail_keys: set[str] = set()
        self.download_content = b"content"

    def download_attachment(self, attachment: AttachmentRef, root: Path, output_name: str) -> Path:
        if attachment.file_key in self.fail_keys:
            raise RuntimeError("download failed")
        path = root / output_name
        path.write_bytes(self.download_content)
        return path


def ref(key: str, name: str, kind: str = "image") -> AttachmentRef:
    return AttachmentRef(
        source_message_id="om_1", file_key=key,
        resource_type="image" if kind == "image" else "file",
        media_type={"image": "image/png", "video": "video/mp4", "text": "text/plain"}[kind],
        name=name, kind=kind,
    )


def test_materialized_attachments_are_deleted_on_exit(tmp_path: Path) -> None:
    refs = [ref("img", "shot.png"), ref("video", "recording.mp4", "video")]
    with AttachmentManager(FakeGateway(), tmp_path, 1024).materialize(refs) as batch:
        paths = [item.local_path for item in batch.available]
        assert paths and all(path.exists() for path in paths)
    assert all(not path.exists() for path in paths)


def test_cleanup_occurs_when_consumer_raises(tmp_path: Path) -> None:
    path: Path | None = None
    with pytest.raises(RuntimeError, match="consumer"):
        with AttachmentManager(FakeGateway(), tmp_path, 1024).materialize([ref("img", "shot.png")]) as batch:
            path = batch.available[0].local_path
            raise RuntimeError("consumer")
    assert path is not None and not path.exists()


def test_text_attachment_respects_byte_limit(tmp_path: Path) -> None:
    gateway = FakeGateway()
    gateway.download_content = b"x" * 1025
    with AttachmentManager(gateway, tmp_path, 1024).materialize([ref("text", "console.log", "text")]) as batch:
        assert batch.failed[0].reason == "text attachment exceeds 1024 bytes"


def test_one_download_failure_does_not_hide_success(tmp_path: Path) -> None:
    gateway = FakeGateway()
    gateway.fail_keys.add("video")
    refs = [ref("img", "shot.png"), ref("video", "recording.mp4", "video")]
    with AttachmentManager(gateway, tmp_path, 1024).materialize(refs) as batch:
        assert len(batch.available) == 1
        assert batch.failed[0].attachment.file_key == "video"


def test_attachment_progress_uses_only_ordinal_and_kind(tmp_path: Path) -> None:
    gateway = FakeGateway()
    attachment = ref("private-file-key", "private screenshot.png")
    events = []

    with AttachmentManager(gateway, tmp_path, 1024, progress=events.append).materialize(
        [attachment]
    ):
        pass

    text = "\n".join(event.message for event in events)
    assert [event.name for event in events] == [
        "attachments.started", "attachment.available", "attachments.completed",
        "attachments.cleanup"
    ]
    assert "第 1 个图片附件" in text
    assert "private-file-key" not in text
    assert "private screenshot.png" not in text
    assert str(tmp_path) not in text
