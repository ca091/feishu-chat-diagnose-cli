import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from feishu_chat_diagnose.errors import ModelOutputError, VideoPathDenied
from feishu_chat_diagnose.video_mcp import VideoToolService


class FakeFiles:
    def __init__(self) -> None:
        self.upload_count = 0
        self.deleted_names: list[str] = []

    def upload(self, *, file: str):
        self.upload_count += 1
        return SimpleNamespace(name="files/video-1", state="ACTIVE", uri="fake://video")

    def get(self, *, name: str):
        return SimpleNamespace(name=name, state="ACTIVE", uri="fake://video")

    def delete(self, *, name: str) -> None:
        self.deleted_names.append(name)


class FakeModels:
    def __init__(self) -> None:
        self.generate_count = 0
        self.responses = [json.dumps({
            "source_message_id": "om_video", "summary": "Text shifts after saving.",
            "observations": [{"timestamp": "00:18", "observation": "Text moves right."}],
            "missing_information": [],
        })]

    def generate_content(self, **kwargs):
        self.generate_count += 1
        return SimpleNamespace(text=self.responses.pop(0))


@pytest.fixture
def fake_genai():
    return SimpleNamespace(files=FakeFiles(), models=FakeModels())


@pytest.fixture
def case_video(tmp_path: Path) -> Path:
    root = tmp_path / "allowed"
    root.mkdir()
    video = root / "recording.mp4"
    video.write_bytes(b"video")
    return video


def test_video_path_must_be_inside_case_media_root(fake_genai, tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    service = VideoToolService(fake_genai, allowed, "gemini-test")
    with pytest.raises(VideoPathDenied):
        service.understand_video(tmp_path / "outside.mp4", "om_video", "Locate failure")


def test_video_is_understood_and_remote_upload_deleted(fake_genai, case_video: Path) -> None:
    service = VideoToolService(fake_genai, case_video.parent, "gemini-test", poll_seconds=0)
    result = service.understand_video(case_video, "om_video", "Locate failure")
    assert result.observations[0].timestamp == "00:18"
    assert fake_genai.files.deleted_names == ["files/video-1"]


def test_invalid_output_is_not_retried(fake_genai, case_video: Path) -> None:
    fake_genai.models.responses = ["not-json"]
    service = VideoToolService(fake_genai, case_video.parent, "gemini-test", poll_seconds=0)
    with pytest.raises(ModelOutputError):
        service.understand_video(case_video, "om_video", "Locate failure")
    with pytest.raises(ModelOutputError):
        service.understand_video(case_video, "om_video", "Try again")
    assert fake_genai.models.generate_count == 1
    assert fake_genai.files.upload_count == 1


def test_duplicate_success_uses_cached_result(fake_genai, case_video: Path) -> None:
    service = VideoToolService(fake_genai, case_video.parent, "gemini-test", poll_seconds=0)
    first = service.understand_video(case_video, "om_video", "Locate failure")
    second = service.understand_video(case_video, "om_video", "Inspect again")
    assert second == first
    assert fake_genai.models.generate_count == 1
