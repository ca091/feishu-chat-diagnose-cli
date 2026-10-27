import io
import subprocess
import sys
import time

import pytest

from feishu_chat_diagnose.codex_protocol import StdioJsonRpcTransport


def test_stdio_transport_emits_heartbeat_during_quiet_wait(mocker) -> None:
    process = mocker.Mock()
    process.stdin = io.StringIO()
    process.stdout.fileno.return_value = 123
    now = [0.0]

    def select_once(*args):
        if now[0] == 0:
            now[0] = 10.0
            return [], [], []
        return [process.stdout], [], []

    select_call = mocker.patch(
        "feishu_chat_diagnose.codex_protocol.select.select", side_effect=select_once,
    )
    mocker.patch(
        "feishu_chat_diagnose.codex_protocol.os.read",
        return_value=b'{"jsonrpc":"2.0","id":1,"result":{}}\n',
    )
    events = []
    transport = StdioJsonRpcTransport(
        process, timeout_seconds=30, progress=events.append,
        heartbeat_seconds=10, clock=lambda: now[0],
    )

    assert transport.request("initialize", {}) == {}

    assert select_call.call_count == 2
    assert events[0].name == "codex.heartbeat"
    assert "10 秒" in events[0].message


@pytest.fixture
def stdio_transport():
    transports = []

    def launch(script, timeout=1.0):
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", script],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )
        transport = StdioJsonRpcTransport(process, timeout_seconds=timeout)
        transports.append(transport)
        return transport

    yield launch
    for transport in transports:
        transport.close()
        transport.process.communicate()


def test_stdio_transport_preserves_multiple_lines_in_one_write(stdio_transport) -> None:
    transport = stdio_transport('''
import os, sys
sys.stdin.readline()
os.write(1, b'{"id":1,"result":{"first":true}}\\n{"id":2,"result":{"second":true}}\\n')
sys.stdin.read()
''')

    assert transport.request("first", {}) == {"first": True}
    assert transport.request("second", {}) == {"second": True}


def test_stdio_transport_times_out_on_unfinished_line(stdio_transport) -> None:
    transport = stdio_transport('''
import os, sys, time
sys.stdin.readline()
os.write(1, b'{"id":1,"result":')
time.sleep(2)
''', timeout=0.2)
    started = time.monotonic()

    with pytest.raises(TimeoutError, match="within 0.2 seconds"):
        transport.request("initialize", {})

    assert time.monotonic() - started < 1.5


def test_stdio_transport_reassembles_fragmented_utf8(mocker) -> None:
    process = mocker.Mock()
    process.stdin = io.StringIO()
    process.stdout.fileno.return_value = 123
    payload = '{"id":1,"result":{"text":"中文"}}\n'.encode("utf-8")
    split = payload.index("中".encode("utf-8")) + 1
    mocker.patch(
        "feishu_chat_diagnose.codex_protocol.select.select",
        return_value=([process.stdout], [], []),
    )
    mocker.patch(
        "feishu_chat_diagnose.codex_protocol.os.read",
        side_effect=[payload[:split], payload[split:]],
    )

    assert StdioJsonRpcTransport(process).request("initialize", {}) == {"text": "中文"}


@pytest.mark.parametrize("payload", [b'{"id":1,"result":{}}', b""])
def test_stdio_transport_handles_eof(stdio_transport, payload) -> None:
    transport = stdio_transport(
        f"import os, sys\nsys.stdin.readline()\nos.write(1, {payload!r})"
    )
    if payload:
        assert transport.request("initialize", {}) == {}
    else:
        with pytest.raises(RuntimeError, match="app-server exited"):
            transport.request("initialize", {})


@pytest.mark.parametrize("phase", ["request", "events"])
def test_notifications_cannot_extend_request_or_session_deadline(mocker, phase) -> None:
    process = mocker.Mock()
    process.stdin = io.StringIO()
    process.stdout.fileno.return_value = 123
    now = [0.0]
    reads = [0]

    def readable(*args):
        now[0] += 0.06
        return [process.stdout], [], []

    def read(*args):
        reads[0] += 1
        if phase == "events" and reads[0] == 1:
            return b'{"id":1,"result":{}}\n'
        return b'{"method":"progress","params":{}}\n'

    mocker.patch("feishu_chat_diagnose.codex_protocol.select.select", side_effect=readable)
    mocker.patch("feishu_chat_diagnose.codex_protocol.os.read", side_effect=read)
    transport = StdioJsonRpcTransport(
        process, timeout_seconds=0.1, total_timeout_seconds=0.3, clock=lambda: now[0],
    )
    if phase == "request":
        with pytest.raises(TimeoutError, match="within 0.1 seconds"):
            transport.request("initialize", {})
        assert 0.1 <= now[0] < 0.2
    else:
        assert transport.request("initialize", {}) == {}
        events = []
        with pytest.raises(TimeoutError, match="total timeout of 0.3 seconds"):
            for event in transport.events():
                events.append(event)
        assert events
        assert 0.3 <= now[0] < 0.4
