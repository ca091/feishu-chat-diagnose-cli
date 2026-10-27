import asyncio
import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from feishu_chat_diagnose.cli import app
from feishu_chat_diagnose.config import VideoToolSettings, load_video_tool_settings
from feishu_chat_diagnose.models import VideoToolResult
import feishu_chat_diagnose.video_check as video_check


# A real stdio MCP subprocess; the tool substitutes fixed evidence for the video provider.
SERVER = r'''
import asyncio
import json
import os
from pathlib import Path

log = Path(os.environ["VIDEO_CHECK_TEST_LOG"])
log.write_text(json.dumps({"pid": os.getpid()}))
mode = os.environ.get("VIDEO_CHECK_TEST_MODE", "structured")
if mode == "crash":
    import sys
    sys.stderr.write("startup failed: " + os.environ["DASHSCOPE_API_KEY"])
    sys.exit(1)
if mode == "protocol_noise":
    print("broken protocol: " + os.environ["DASHSCOPE_API_KEY"], flush=True)
if mode == "startup_timeout":
    import time
    time.sleep(60)
if mode == "api_request_stall":
    import sys
    import time
    sys.path.insert(0, os.environ["VIDEO_CHECK_TEST_SOURCE"])
    from feishu_chat_diagnose import video_mcp
    class Client:
        async def post(self, *args, **kwargs):
            root = Path(os.environ["FEISHU_DIAGNOSE_MEDIA_ROOT"])
            log.write_text(json.dumps({"pid": os.getpid(), "path": str(next(root.glob("*.mp4")))}))
            await asyncio.sleep(60)
    video_mcp._service = video_mcp.VideoToolService(
        Client(), Path(os.environ["FEISHU_DIAGNOSE_MEDIA_ROOT"]),
        "qwen-test", timeout_seconds=0.4,
    )
    video_mcp.mcp.run(transport="stdio")
    sys.exit()

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent

mcp = FastMCP("video-check-test")

@mcp.tool(name="other" if mode == "missing" else "understand_video")
async def understand_video(path: str, source_message_id: str, question: str) -> CallToolResult:
    video = Path(path)
    root = Path(os.environ["FEISHU_DIAGNOSE_MEDIA_ROOT"])
    assert video.is_relative_to(root) and video.read_bytes() == b"test video"
    assert question and source_message_id == "video-check"
    log.write_text(json.dumps({
        "pid": os.getpid(), "path": path,
        "model": os.environ["FEISHU_DIAGNOSE_VIDEO_MODEL"],
        "key_ok": os.environ["DASHSCOPE_API_KEY"] == "environment-video-key",
        "timeout": os.environ["FEISHU_DIAGNOSE_VIDEO_TIMEOUT"],
    }))
    if mode == "timeout":
        await asyncio.sleep(60)
    if mode == "error":
        return CallToolResult(isError=True, content=[TextContent(
            type="text", text="provider rejected " + os.environ["DASHSCOPE_API_KEY"]
        )])
    if mode == "invalid":
        return CallToolResult(content=[TextContent(type="text", text="not-json")])
    payload = {
        "source_message_id": "wrong" if mode == "wrong_source" else source_message_id,
        "summary": "点击导出后没有响应",
        "observations": [{"timestamp": "00:02", "observation": "点击导出按钮"}],
        "missing_information": [],
    }
    if mode == "invalid_structured":
        payload["observations"] = "invalid"
    text = TextContent(type="text", text=json.dumps(payload))
    if mode == "text":
        return CallToolResult(content=[text])
    return CallToolResult(content=[text], structuredContent=payload)

mcp.run(transport="stdio")
'''


@pytest.fixture
def video_config(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    config = tmp_path / "config.toml"
    config.write_text('''
[video_tool]
model = "qwen-test"
base_url = "https://gateway.example/compatible-mode/v1"
api_key = "configured-video-key"
[repositories.broken]
path = "/missing/repository"
diagnosis_skill = "missing"
[codex]
timeout_seconds = "unrelated invalid setting"
''', encoding="utf-8")
    return config


@pytest.fixture
def local_video(tmp_path):
    path = tmp_path / "original video.mp4"
    path.write_bytes(b"test video")
    return path


@pytest.fixture
def mcp_server(tmp_path, monkeypatch):
    log = tmp_path / "server.json"
    monkeypatch.setenv("VIDEO_CHECK_TEST_LOG", str(log))
    monkeypatch.setenv("DASHSCOPE_API_KEY", "environment-video-key")
    parameters = video_check.StdioServerParameters
    captured = []

    def server_parameters(**kwargs):
        assert kwargs["args"] == ["-m", "feishu_chat_diagnose.video_mcp"]
        captured.append(kwargs)
        return parameters(**{**kwargs, "args": ["-c", SERVER]})

    monkeypatch.setattr(video_check, "StdioServerParameters", server_parameters)
    return log, captured


def assert_resources_cleaned(log, captured, original):
    assert original.read_bytes() == b"test video"
    assert not Path(captured[0]["env"]["FEISHU_DIAGNOSE_MEDIA_ROOT"]).exists()
    pid = json.loads(log.read_text())["pid"]
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_real_stdio_text_fallback_and_cleanup(mcp_server, local_video, monkeypatch):
    log, captured = mcp_server
    monkeypatch.setenv("VIDEO_CHECK_TEST_MODE", "text")
    events = []
    settings = VideoToolSettings(model="qwen-test", api_key="configured-video-key")

    result = asyncio.run(video_check.check_video(local_video, settings, events.append))

    assert result.summary == "点击导出后没有响应"
    assert result.observations[0].timestamp == "00:02"
    recorded = json.loads(log.read_text())
    assert recorded["key_ok"] and recorded["model"] == "qwen-test"
    assert recorded["timeout"] == "300.0"
    assert Path(recorded["path"]) != local_video
    assert events[-1].name == "video_check.completed"
    assert os.environ["DASHSCOPE_API_KEY"] == "environment-video-key"
    assert_resources_cleaned(log, captured, local_video)


@pytest.mark.parametrize("mode,expected", [
    ("missing", "未提供 understand_video"),
    ("error", "provider rejected"),
    ("invalid", "不符合 VideoToolResult"),
    ("invalid_structured", "不符合 VideoToolResult"),
    ("wrong_source", "source_message_id 不匹配"),
    ("crash", "Connection closed"),
    ("timeout", "[Tt]imed out|[Tt]imeout|超时"),
    ("startup_timeout", "超时"),
])
def test_real_stdio_failures_cleanup(mcp_server, local_video, monkeypatch, mode, expected):
    log, captured = mcp_server
    monkeypatch.setenv("VIDEO_CHECK_TEST_MODE", mode)
    if mode == "startup_timeout":
        monkeypatch.setattr(video_check, "STARTUP_TIMEOUT_SECONDS", 0.4)
    settings = VideoToolSettings(model="qwen-test", timeout_seconds=0.15 if mode == "timeout" else 10)

    with pytest.raises(video_check.VideoCheckError, match=expected):
        asyncio.run(video_check.check_video(local_video, settings))

    assert_resources_cleaned(log, captured, local_video)


def test_cli_json_over_real_stdio_without_feishu_codex_or_cases(
    video_config, local_video, mcp_server, diagnose_home, mocker, monkeypatch
):
    for dependency in ("build_runtime", "CodexAppServerDiagnosis.launch", "LarkCliRunner.run", "CaseStore"):
        mocker.patch(f"feishu_chat_diagnose.cli.{dependency}", side_effect=AssertionError("video-check must be independent"))
    monkeypatch.chdir(local_video.parent)
    result = CliRunner().invoke(app, ["video-check", local_video.name, "--config", str(video_config), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["source_message_id"] == "video-check"
    assert payload["summary"] == "点击导出后没有响应"
    assert "正在上传并理解视频" in result.stderr
    assert_resources_cleaned(*mcp_server, local_video)
    assert not diagnose_home.exists()


def test_cancelled_call_cleans_subprocess_and_temporary_video(mcp_server, local_video, monkeypatch):
    log, captured = mcp_server
    monkeypatch.setenv("VIDEO_CHECK_TEST_MODE", "timeout")

    async def cancel_after_call_starts():
        task = asyncio.create_task(video_check.check_video(local_video, VideoToolSettings()))
        try:
            async with asyncio.timeout(10):
                while not log.exists() or "path" not in json.loads(log.read_text()):
                    await asyncio.sleep(0.02)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(cancel_after_call_starts())
    assert_resources_cleaned(log, captured, local_video)


def test_stalled_api_reports_stage_and_heartbeat_before_timeout(mcp_server, local_video, monkeypatch):
    log, captured = mcp_server
    monkeypatch.setenv("VIDEO_CHECK_TEST_MODE", "api_request_stall")
    monkeypatch.setenv("VIDEO_CHECK_TEST_SOURCE", str(Path(video_check.__file__).parents[1]))
    monkeypatch.setattr(video_check, "HEARTBEAT_SECONDS", 0.05, raising=False)
    events = []
    with pytest.raises(video_check.VideoCheckError, match="上传"):
        asyncio.run(video_check.check_video(local_video, VideoToolSettings(timeout_seconds=0.4), events.append))
    assert any(event.name == "video_check.heartbeat" for event in events)
    assert any("正在发送视频并请求阿里模型理解" in event.message for event in events)
    assert_resources_cleaned(log, captured, local_video)


def test_cli_error_is_redacted_and_stdout_empty(video_config, local_video, mcp_server, monkeypatch):
    monkeypatch.setenv("VIDEO_CHECK_TEST_MODE", "error")
    result = CliRunner().invoke(app, ["video-check", str(local_video), "--config", str(video_config), "--json"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "provider rejected [REDACTED]" in result.stderr
    assert "environment-video-key" not in result.output
    assert "configured-video-key" not in result.output
    assert "Traceback" not in result.output
    assert_resources_cleaned(*mcp_server, local_video)


def test_malformed_stdio_does_not_expose_raw_payload(
    video_config, local_video, mcp_server, monkeypatch, caplog
):
    monkeypatch.setenv("VIDEO_CHECK_TEST_MODE", "protocol_noise")
    monkeypatch.setattr(video_check, "STARTUP_TIMEOUT_SECONDS", 2)
    result = CliRunner().invoke(app, ["video-check", str(local_video), "--config", str(video_config), "--json"])

    # The SDK tolerates an unrelated malformed stdout line; the real tool
    # response must still be valid, and its parse diagnostics must be safe.
    assert result.exit_code == 0
    assert json.loads(result.stdout)["source_message_id"] == "video-check"
    assert "environment-video-key" not in result.output + caplog.text
    assert "已隐藏原始协议数据" in caplog.text
    assert_resources_cleaned(*mcp_server, local_video)


def test_cli_plain_output_and_result_redaction(video_config, local_video, mocker):
    result = VideoToolResult(
        source_message_id="video-check", summary="configured-video-key rejected",
        observations=[{"timestamp": "00:02", "observation": "点击按钮"}],
        missing_information=["缺少日志"],
    )
    mocker.patch("feishu_chat_diagnose.cli.check_video", new=mocker.AsyncMock(return_value=result))
    output = CliRunner().invoke(app, ["video-check", str(local_video), "--config", str(video_config)])

    assert output.exit_code == 0
    assert "qwen-test" in output.stdout and "耗时" in output.stdout
    assert "00:02：点击按钮" in output.stdout and "缺少日志" in output.stdout
    assert "[REDACTED] rejected" in output.stdout
    assert "configured-video-key" not in output.output


def test_cli_json_redacts_results_without_breaking_json(video_config, local_video, mocker):
    response = VideoToolResult(
        source_message_id="video-check", summary='configured-video-key and "quotes"',
        observations=[], missing_information=[],
    )
    mocker.patch("feishu_chat_diagnose.cli.check_video", new=mocker.AsyncMock(return_value=response))
    output = CliRunner().invoke(app, ["video-check", str(local_video), "--config", str(video_config), "--json"])
    assert output.exit_code == 0
    assert json.loads(output.stdout)["summary"] == '[REDACTED] and "quotes"'


def test_missing_video_config_explains_init(diagnose_home, local_video):
    result = CliRunner().invoke(app, ["video-check", str(local_video), "--json"])
    assert result.exit_code == 1
    assert result.stdout == "" and "feishu-diagnose init" in result.stderr


@pytest.mark.parametrize("kind", ["missing", "empty", "unreadable"])
def test_invalid_file_fails_before_subprocess(video_config, tmp_path, mocker, kind):
    path = tmp_path / "video.mp4"
    if kind in {"empty", "unreadable"}:
        path.touch()
    if kind == "unreadable":
        mocker.patch.object(Path, "open", side_effect=PermissionError("no access"))
        mocker.patch("feishu_chat_diagnose.cli.load_video_tool_settings", return_value=VideoToolSettings())
    spawn = mocker.patch.object(video_check, "stdio_client", side_effect=AssertionError("must not spawn"))

    result = CliRunner().invoke(app, ["video-check", str(path), "--config", str(video_config), "--json"])

    assert result.exit_code == 2, result.output
    assert result.stdout == ""
    assert "Traceback" not in result.output
    spawn.assert_not_called()


def test_cli_interrupt_returns_130(video_config, local_video, mocker):
    mocker.patch("feishu_chat_diagnose.cli.check_video", new=mocker.AsyncMock(side_effect=KeyboardInterrupt))
    result = CliRunner().invoke(app, ["video-check", str(local_video), "--config", str(video_config)])
    assert result.exit_code == 130
    assert "已中断" in result.stderr


def test_video_config_selection_and_unrelated_settings(video_config, diagnose_home, monkeypatch):
    # Deliberately malformed repository/Codex settings must not block video checks.
    assert load_video_tool_settings(video_config).model == "qwen-test"
    monkeypatch.setenv("FEISHU_DIAGNOSE_CONFIG", str(video_config))
    assert load_video_tool_settings().api_key.get_secret_value() == "configured-video-key"
    diagnose_home.mkdir()
    global_config = diagnose_home / "config.toml"
    global_config.write_text('[video_tool]\napi_key="global-key"\nbase_url="https://gateway.example/compatible-mode/v1"\n', encoding="utf-8")
    assert load_video_tool_settings(global_config).api_key.get_secret_value() == "global-key"
    monkeypatch.delenv("FEISHU_DIAGNOSE_CONFIG")
    assert load_video_tool_settings().api_key.get_secret_value() == "global-key"


@pytest.mark.parametrize("content,expected", [
    ('[video_tool]\nprovider="other"\napi_key="key"', "provider"),
    ('[video_tool]\nmodel=" "\napi_key="key"', "model"),
    ('[video_tool]\napi_key=""', "DASHSCOPE_API_KEY"),
    ('[video_tool]\ntimeout_seconds=0\napi_key="key"', "timeout_seconds"),
    ('[video_tool]\ntimeout_seconds=inf\napi_key="key"', "timeout_seconds"),
    ('[video_tool]\napi_key=["private-key"]', "Invalid video configuration"),
    ('[video_tool', "Invalid configuration"),
])
def test_invalid_video_config_fails_before_subprocess(tmp_path, monkeypatch, mocker, content, expected):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    config = tmp_path / "config.toml"
    config.write_text(content, encoding="utf-8")
    check = mocker.patch("feishu_chat_diagnose.cli.check_video")
    result = CliRunner().invoke(app, ["video-check", "unused.mp4", "--config", str(config), "--json"])

    assert result.exit_code == 1
    assert expected in result.stderr
    assert result.stdout == "" and "private-key" not in result.output
    check.assert_not_called()


@pytest.mark.parametrize("kind", ["oversized", "unsupported"])
def test_invalid_video_content_fails_before_subprocess(video_config, tmp_path, mocker, kind):
    path = tmp_path / ("video.mp4" if kind == "oversized" else "video.txt")
    with path.open("wb") as handle:
        handle.truncate(7_499_998 if kind == "oversized" else 10)
    spawn = mocker.patch.object(video_check, "stdio_client")
    result = CliRunner().invoke(app, ["video-check", str(path), "--config", str(video_config), "--json"])
    assert result.exit_code == 2 and result.stdout == ""
    assert ("请先压缩" if kind == "oversized" else "扩展名") in result.stderr
    spawn.assert_not_called()


@pytest.mark.parametrize("status", [200, 401])
def test_real_video_mcp_calls_compatible_http_endpoint(tmp_path, local_video, monkeypatch, status):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    import base64

    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, self.headers["Authorization"], payload))
            result = {
                "source_message_id": "video-check", "summary": "真实 MCP 与兼容接口联通",
                "observations": [{"timestamp": "00:02", "observation": "点击按钮"}],
                "missing_information": [],
            }
            envelope = {"choices": [{"message": {"content": json.dumps(result)}}]}
            body = json.dumps(envelope if status == 200 else {"error": "private-gateway-key"}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        config = tmp_path / "http-config.toml"
        config.write_text(
            '[video_tool]\nmodel="qwen-test"\napi_key="private-gateway-key"\n'
            f'base_url="http://127.0.0.1:{server.server_port}/compatible-mode/v1"\n'
            'timeout_seconds=10\n', encoding="utf-8",
        )
        try:
            output = CliRunner().invoke(app, ["video-check", str(local_video), "--config", str(config), "--json"])
        finally:
            server.shutdown()
            thread.join(timeout=5)
    assert output.exit_code == (0 if status == 200 else 1), output.output
    if status == 200:
        assert json.loads(output.stdout)["summary"] == "真实 MCP 与兼容接口联通"
    else:
        assert output.stdout == "" and "HTTP 401" in output.stderr
    assert "private-gateway-key" not in output.output
    path, auth, payload = requests[0]
    assert len(requests) == 1 and path == "/compatible-mode/v1/chat/completions"
    assert auth == "Bearer private-gateway-key"
    assert payload["model"] == "qwen-test" and payload["enable_thinking"] is False
    url = payload["messages"][0]["content"][0]["video_url"]["url"]
    assert base64.b64decode(url.split(",")[1]) == local_video.read_bytes()
