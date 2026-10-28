import asyncio

import pytest

from feishu_chat_diagnose.config import CodexSettings, LocalVideoSettings, VideoToolSettings
from feishu_chat_diagnose.errors import VideoPathDenied
from feishu_chat_diagnose.models import VideoToolResult
from feishu_chat_diagnose.video_local import LocalVideoError
from feishu_chat_diagnose.video_tool import (
    VideoDeadlineExceeded, VideoUnderstandingError, VideoUnderstandingService,
)


@pytest.fixture
def video_service(tmp_path, mocker):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"video")
    result = VideoToolResult(source_message_id="om_video", summary="画面为黑屏", observations=[], missing_information=[])
    local = mocker.patch("feishu_chat_diagnose.video_tool.LocalVideoUnderstanding.understand_video", return_value=result)
    mcp = mocker.patch("feishu_chat_diagnose.video_tool.check_video", new=mocker.AsyncMock(return_value=result))
    events = []
    service = VideoUnderstandingService(tmp_path, LocalVideoSettings(), CodexSettings(), VideoToolSettings(), events.append)
    return service, path, local, mcp, events


def test_local_success_needs_no_mcp_key_and_is_cached(video_service):
    service, path, local, mcp, events = video_service
    with path.open("wb") as stream:
        stream.truncate(8_000_000)  # The remote provider's 7.5 MB cap does not apply locally.
    first = service.understand_video(path, "om_video", "看画面")
    assert first.backend == "local" and first.summary == "画面为黑屏"
    assert service.understand_video(path, "om_video", "再看") is first
    assert local.call_count == 1
    mcp.assert_not_called()


def test_failed_local_falls_back_once_with_source_and_question(video_service):
    service, path, local, mcp, events = video_service
    service.mcp_settings = VideoToolSettings(api_key="secret", base_url="https://gateway.example/v1")
    local.side_effect = LocalVideoError("解码失败")
    result = service.understand_video(path, "om_video", "哪里异常？")
    assert result.backend == "mcp" and result.fallback_reason == "解码失败"
    service.understand_video(path, "om_video", "再看")
    assert mcp.await_count == 1
    assert mcp.call_args.kwargs["source_message_id"] == "om_video"
    assert mcp.call_args.kwargs["question"] == "哪里异常？"
    assert [e.name for e in events] == ["video.fallback", "video.mcp.started"]


def test_both_failures_are_reported_and_cached_without_raw_payload(video_service):
    service, path, local, mcp, events = video_service
    service.mcp_settings = VideoToolSettings(api_key="secret", base_url="https://gateway.example/v1")
    local.side_effect = LocalVideoError("无法读取抽样画面")
    mcp.side_effect = RuntimeError("secret and private chat payload")
    for _ in range(2):
        with pytest.raises(VideoUnderstandingError, match="本地：.*MCP：") as error:
            service.understand_video(path, "om_video", "看画面")
        assert "private chat" not in str(error.value) and "secret" not in str(error.value)
    assert local.call_count == mcp.await_count == 1


def test_cancel_and_expired_deadline_never_start_fallback(video_service):
    service, path, local, mcp, events = video_service
    local.side_effect = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        service.understand_video(path, "om_video", "看画面")
    mcp.assert_not_called()
    local.side_effect = LocalVideoError("超时")
    budgets = iter([120, 0])
    service.remaining_seconds = lambda: next(budgets)
    with pytest.raises(VideoDeadlineExceeded):
        service.understand_video(path, "om_video", "看画面")
    mcp.assert_not_called()


def test_path_denied_and_local_only_failure_do_not_fallback(video_service, tmp_path):
    service, path, local, mcp, events = video_service
    service.media_root = tmp_path / "other"
    with pytest.raises(VideoPathDenied):
        service.understand_video(path, "om_video", "看画面")
    local.assert_not_called()
    service.media_root = tmp_path
    local.side_effect = LocalVideoError("ffmpeg 不存在")
    with pytest.raises(VideoUnderstandingError, match="ffmpeg"):
        service.understand_video(path, "om_video", "看画面", backend="local")
    mcp.assert_not_called()


def test_mcp_timeout_cancels_request_and_is_not_repeated(video_service):
    service, path, local, mcp, events = video_service
    service.mcp_settings = VideoToolSettings(api_key="secret", base_url="https://gateway.example/v1", timeout_seconds=0.01)
    cancelled = []

    async def stall(*args, **kwargs):
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.append(True)

    local.side_effect = LocalVideoError("解码失败")
    mcp.side_effect = stall
    for _ in range(2):
        with pytest.raises(VideoUnderstandingError, match="MCP 视频解析超时"):
            service.understand_video(path, "om_video", "看画面")
    assert cancelled == [True]


@pytest.mark.parametrize("backend", ["auto", "local"])
def test_cli_local_modes_ignore_repository_config_and_cleanup_without_mcp_key(
    tmp_path, diagnose_home, monkeypatch, mocker, backend,
):
    import json
    from typer.testing import CliRunner
    from feishu_chat_diagnose.cli import app
    from feishu_chat_diagnose.models import VideoUnderstandingResult

    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    config = tmp_path / "config.toml"
    config.write_text('[repositories.invalid]\npath = 123\n')
    video = tmp_path / "original.mp4"
    video.write_bytes(b"original")
    mcp = mocker.patch("feishu_chat_diagnose.cli.check_video")
    service = mocker.patch("feishu_chat_diagnose.cli.VideoUnderstandingService").return_value
    copies = []

    def understand(path, source, question, **options):
        assert path != video and path.read_bytes() == b"original"
        assert options["backend"] == backend
        copies.append(path)
        return VideoUnderstandingResult(
            source_message_id=source, summary="本地观察", observations=[], missing_information=[], backend="local",
        )

    service.understand_video.side_effect = understand
    command = ["video-check", str(video), "--config", str(config), "--json"]
    if backend == "local":
        command.extend(["--backend", "local"])
    result = CliRunner().invoke(app, command)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["backend"] == "local"
    assert copies and not copies[0].exists()
    assert video.read_bytes() == b"original" and not diagnose_home.exists()
    mcp.assert_not_called()
