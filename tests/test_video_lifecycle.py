import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Event, Thread
import time

import pytest

from feishu_chat_diagnose import video_mcp


class ToolContext:
    async def report_progress(self, **kwargs):
        pass


@pytest.fixture
def slow_video_api(tmp_path, monkeypatch):
    requests = []
    release = Event()
    disconnected = Event()
    payload = {"choices": [{"message": {"content": json.dumps({
        "source_message_id": "om_video", "summary": "ok",
        "observations": [], "missing_information": [],
    })}}]}
    body = json.dumps(payload).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            requests.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                # Keep each read below the I/O timeout while exceeding the total deadline.
                for byte in body:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    if not release.is_set():
                        time.sleep(0.01)
            except OSError:
                disconnected.set()

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        video = tmp_path / "video.mp4"
        video.write_bytes(b"video")
        monkeypatch.setattr(video_mcp, "_service", None)
        monkeypatch.setenv("FEISHU_DIAGNOSE_MEDIA_ROOT", str(tmp_path))
        monkeypatch.setenv("FEISHU_DIAGNOSE_VIDEO_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1")
        monkeypatch.setenv("FEISHU_DIAGNOSE_VIDEO_MODEL", "qwen-test")
        monkeypatch.setenv("FEISHU_DIAGNOSE_VIDEO_TIMEOUT", "0.15")
        monkeypatch.setenv("DASHSCOPE_API_KEY", "fake-key")
        try:
            yield video, requests, release, disconnected
        finally:
            release.set()
            if video_mcp._service is not None:
                client = video_mcp._service.client
                if hasattr(client, "aclose"):
                    asyncio.run(client.aclose())
                else:
                    client.close()
            server.shutdown()
            thread.join(timeout=5)


async def call_video(video: Path):
    return await video_mcp.understand_video(str(video), "om_video", "Locate failure", ToolContext())


def test_total_timeout_closes_request_and_caches_failure(slow_video_api):
    video, requests, release, disconnected = slow_video_api

    async def scenario():
        for _ in range(2):
            with pytest.raises(TimeoutError):
                await call_video(video)
        await asyncio.sleep(0.1)
        assert len(requests) == 1
        assert disconnected.is_set(), "timed-out HTTP response is still being read"
        release.set()
        await asyncio.sleep(0.1)
        assert not video_mcp._service._results
        assert video_mcp._service._failures

    asyncio.run(scenario())


def test_concurrent_tool_calls_share_one_request(slow_video_api, monkeypatch):
    video, requests, release, _ = slow_video_api
    monkeypatch.setenv("FEISHU_DIAGNOSE_VIDEO_TIMEOUT", "2")

    async def scenario():
        first = asyncio.create_task(call_video(video))
        second = asyncio.create_task(call_video(video))
        try:
            async with asyncio.timeout(2):
                while not requests:
                    await asyncio.sleep(0.01)
            await asyncio.sleep(0.1)
        finally:
            release.set()
        results = await asyncio.gather(first, second)
        assert results[0] == results[1]
        assert len(requests) == 1

    asyncio.run(scenario())


def test_cancelled_tool_call_is_terminal_and_closes_request(slow_video_api, monkeypatch):
    video, requests, _, disconnected = slow_video_api
    monkeypatch.setenv("FEISHU_DIAGNOSE_VIDEO_TIMEOUT", "2")

    async def scenario():
        task = asyncio.create_task(call_video(video))
        async with asyncio.timeout(2):
            while not requests:
                await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(RuntimeError, match="取消"):
            await call_video(video)
        await asyncio.sleep(0.1)
        assert len(requests) == 1
        assert disconnected.is_set()
        assert not video_mcp._service._results

    asyncio.run(scenario())


def test_cancelling_queued_caller_does_not_cancel_paid_request(slow_video_api, monkeypatch):
    video, requests, release, _ = slow_video_api
    monkeypatch.setenv("FEISHU_DIAGNOSE_VIDEO_TIMEOUT", "2")

    async def scenario():
        owner = asyncio.create_task(call_video(video))
        async with asyncio.timeout(2):
            while not requests:
                await asyncio.sleep(0.01)
        waiter = asyncio.create_task(call_video(video))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        release.set()
        result = await owner
        assert await call_video(video) == result
        assert len(requests) == 1
        assert not video_mcp._service._failures

    asyncio.run(scenario())
