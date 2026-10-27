import asyncio
import base64
import json
from pathlib import Path

import httpx
import pytest

from feishu_chat_diagnose.errors import ModelOutputError, VideoPathDenied
from feishu_chat_diagnose.video_input import VideoInputError
from feishu_chat_diagnose.video_mcp import VideoToolService


RESULT = {
    "source_message_id": "om_video", "summary": "Text shifts after saving.",
    "observations": [{"timestamp": "00:18", "observation": "Text moves right."}],
    "missing_information": [],
}


@pytest.fixture
def case_video(tmp_path: Path) -> Path:
    root = tmp_path / "allowed"
    root.mkdir()
    video = root / "recording.mp4"
    video.write_bytes(b"video")
    return video


@pytest.fixture
def api_client():
    requests = []
    responses = [httpx.Response(200, json={"choices": [{
        "message": {"content": json.dumps(RESULT)}, "finish_reason": "stop",
    }]})]

    def handle(request):
        requests.append(request)
        response = responses[0] if len(responses) == 1 else responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    client = httpx.AsyncClient(
        base_url="https://gateway.example/compatible-mode/v1/",
        headers={"Authorization": "Bearer fake-key"},
        transport=httpx.MockTransport(handle),
    )
    try:
        yield client, requests, responses
    finally:
        asyncio.run(client.aclose())


def service(api_client, case_video):
    return VideoToolService(api_client[0], case_video.parent, "qwen-test", timeout_seconds=12)


def test_video_path_must_be_inside_case_media_root(api_client, case_video, tmp_path):
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"video")
    with pytest.raises(VideoPathDenied):
        asyncio.run(service(api_client, case_video).understand_video(outside, "om_video", "Locate failure"))
    assert not api_client[1]


def test_symlink_outside_media_root_is_denied(api_client, case_video, tmp_path):
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"video")
    link = case_video.parent / "link.mp4"
    link.symlink_to(outside)
    with pytest.raises(VideoPathDenied):
        asyncio.run(service(api_client, case_video).understand_video(link, "om_video", "Locate failure"))
    assert not api_client[1]


def test_video_request_preserves_base_path_and_original_video(api_client, case_video):
    stages = []
    async def report(message):
        stages.append(message)

    result = asyncio.run(service(api_client, case_video).understand_video(
        case_video, "om_video", "Locate failure", progress=report,
    ))
    assert result.observations[0].timestamp == "00:18"
    request, = api_client[1]
    assert str(request.url) == "https://gateway.example/compatible-mode/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer fake-key"
    assert request.extensions["timeout"]["read"] == 12
    payload = json.loads(request.content)
    assert payload["model"] == "qwen-test"
    assert payload["enable_thinking"] is False and payload["stream"] is False
    assert payload["max_tokens"] == 4096
    video, text = payload["messages"][0]["content"]
    assert video["fps"] == 2 and video["type"] == "video_url"
    assert video["video_url"]["url"].startswith("data:video/mp4;base64,")
    assert base64.b64decode(video["video_url"]["url"].split(",")[1]) == b"video"
    assert "om_video" in text["text"] and "Locate failure" in text["text"]
    assert "untrusted evidence" in text["text"] and "source_message_id" in text["text"]
    assert stages == ["正在检查并编码视频", "正在发送视频并请求阿里模型理解（上传与生成共用请求）", "正在校验视频理解结果"]
    assert case_video.read_bytes() == b"video"


@pytest.mark.parametrize("raw", ["not-json", json.dumps({**RESULT, "source_message_id": "wrong"})])
def test_invalid_output_is_not_retried(api_client, case_video, raw):
    api_client[2][:] = [httpx.Response(200, json={"choices": [{"message": {"content": raw}}]})]
    tool = service(api_client, case_video)
    for question in ("Locate failure", "Try again"):
        with pytest.raises(ModelOutputError):
            asyncio.run(tool.understand_video(case_video, "om_video", question))
    assert len(api_client[1]) == 1


def test_duplicate_success_uses_cached_result(api_client, case_video):
    tool = service(api_client, case_video)
    first = asyncio.run(tool.understand_video(case_video, "om_video", "Locate failure"))
    second = asyncio.run(tool.understand_video(case_video, "om_video", "Inspect again"))
    assert second == first
    assert len(api_client[1]) == 1


# Representative authentication, rate-limit, server, and redirect responses.
@pytest.mark.parametrize("status", [401, 429, 500, 302])
def test_provider_errors_are_safe_and_not_retried(api_client, case_video, status):
    api_client[2][:] = [httpx.Response(status, text="fake-key data:video/mp4;base64,PRIVATE")]
    tool = service(api_client, case_video)
    for _ in range(2):
        with pytest.raises(RuntimeError, match=f"HTTP {status}") as caught:
            asyncio.run(tool.understand_video(case_video, "om_video", "Locate failure"))
        assert "fake-key" not in str(caught.value) and "PRIVATE" not in str(caught.value)
    assert len(api_client[1]) == 1


@pytest.mark.parametrize("error", [httpx.ReadTimeout("PRIVATE"), httpx.ConnectError("fake-key")])
def test_transport_errors_are_safe_and_not_retried(api_client, case_video, error):
    api_client[2][:] = [error]
    tool = service(api_client, case_video)
    for _ in range(2):
        with pytest.raises((RuntimeError, TimeoutError)) as caught:
            asyncio.run(tool.understand_video(case_video, "om_video", "Locate failure"))
        assert "PRIVATE" not in str(caught.value) and "fake-key" not in str(caught.value)
    assert len(api_client[1]) == 1


@pytest.mark.parametrize("payload", [
    None, {}, {"choices": []}, {"choices": [{"message": {"content": None}}]},
    {"choices": [{"message": {"content": json.dumps(RESULT)}, "finish_reason": "length"}]},
])
def test_malformed_or_truncated_api_envelope(api_client, case_video, payload):
    api_client[2][:] = [httpx.Response(200, content=json.dumps(payload))]
    with pytest.raises(RuntimeError, match="响应格式无效或内容被截断"):
        asyncio.run(service(api_client, case_video).understand_video(case_video, "om_video", "Locate failure"))


@pytest.mark.parametrize("size,valid", [(7_499_997, True), (7_499_998, False), (0, False)])
def test_encoded_size_boundary_before_network(api_client, case_video, size, valid):
    with case_video.open("wb") as handle:
        handle.truncate(size)
    if valid:
        asyncio.run(service(api_client, case_video).understand_video(case_video, "om_video", "Locate failure"))
        assert len(api_client[1]) == 1
    else:
        with pytest.raises(VideoInputError):
            asyncio.run(service(api_client, case_video).understand_video(case_video, "om_video", "Locate failure"))
        assert not api_client[1]


def test_mov_extension_uses_matching_mime_type(api_client, case_video):
    video = case_video.rename(case_video.with_suffix(".MOV"))
    asyncio.run(service(api_client, video).understand_video(video, "om_video", "Locate failure"))
    video_url = json.loads(api_client[1][0].content)["messages"][0]["content"][0]
    assert video_url["video_url"]["url"].startswith("data:video/quicktime;base64,")


def test_client_has_explicit_credentials_endpoint_and_timeout(case_video, monkeypatch, mocker):
    from feishu_chat_diagnose import video_mcp

    monkeypatch.setattr(video_mcp, "_service", None)
    monkeypatch.setenv("FEISHU_DIAGNOSE_MEDIA_ROOT", str(case_video.parent))
    monkeypatch.setenv("FEISHU_DIAGNOSE_VIDEO_MODEL", "qwen-test")
    monkeypatch.setenv("FEISHU_DIAGNOSE_VIDEO_BASE_URL", "https://gateway.example/compatible-mode/v1")
    monkeypatch.setenv("FEISHU_DIAGNOSE_VIDEO_TIMEOUT", "12")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "fake-key")
    factory = mocker.patch("feishu_chat_diagnose.video_mcp.httpx.AsyncClient")
    video_mcp.service_from_environment()
    assert factory.call_args.kwargs == {
        "base_url": "https://gateway.example/compatible-mode/v1/",
        "headers": {"Authorization": "Bearer fake-key"},
        "timeout": 12,
        "follow_redirects": False,
    }
