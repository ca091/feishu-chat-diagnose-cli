import json
import io
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

from feishu_chat_diagnose.attachments import MaterializedAttachment
from feishu_chat_diagnose.codex_app_server import CodexAppServerDiagnosis, StdioJsonRpcTransport
from feishu_chat_diagnose.errors import ModelOutputError
from feishu_chat_diagnose.models import (
    AttachmentRef, ConversationIdentity, DiagnosisSkill, MessageBundle, NormalizedMessage, Sender,
    TimeRange,
)


VALID = json.dumps({
    "problem_summary": "Export shifts text", "facts": ["Video reproduces issue"],
    "root_cause": "Scale is applied twice", "confidence": 82,
    "code_evidence": [{"path": "src/export.ts", "symbol": "exportCanvas", "line": 42,
                       "explanation": "Transform is duplicated"}],
    "alternative_hypotheses": [], "missing_information": [],
    "recommended_fix_direction": ["Normalize transform"],
    "recommended_verification": ["Add export regression test"],
    "skills_used": [],
})


class FakeTransport:
    def __init__(self, final: str = VALID) -> None:
        self.final = final
        self.requests: list[tuple[str, dict]] = []
        self.notifications: list[tuple[str, dict]] = []

    def request(self, method: str, params: dict) -> dict:
        self.requests.append((method, params))
        if method == "skills/list":
            cwd = params["cwds"][0]
            return {"data": [{"cwd": cwd, "errors": [], "skills": []}]}
        if method == "thread/start":
            return {"thread": {"id": "thread-1"}}
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        return {}

    def notify(self, method: str, params: dict) -> None:
        self.notifications.append((method, params))

    def events(self):
        yield {"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": self.final}}}
        yield {"method": "turn/completed", "params": {"turn": {"status": "completed"}}}

    def close(self) -> None:
        pass

    def set_progress(self, progress) -> None:
        self.progress = progress


@pytest.fixture
def bundle() -> MessageBundle:
    now = datetime.fromisoformat("2026-07-15T14:00:00+08:00")
    return MessageBundle(
        conversation=ConversationIdentity(chat_id="oc_1", name="Project", conversation_type="group"),
        time_range=TimeRange(start=now, end=now),
        messages=[NormalizedMessage(message_id="om_1", sender=Sender(open_id="ou_1", name="Zhang"),
                                    created_at=now, text="Export moves text")],
    )


def materialized(tmp_path: Path) -> list[MaterializedAttachment]:
    image = tmp_path / "shot.png"; image.write_bytes(b"png")
    video = tmp_path / "recording.mp4"; video.write_bytes(b"video")
    return [
        MaterializedAttachment(AttachmentRef(source_message_id="om_img", file_key="i", resource_type="image",
            media_type="image/png", name="shot.png", kind="image"), image),
        MaterializedAttachment(AttachmentRef(source_message_id="om_video", file_key="v", resource_type="file",
            media_type="video/mp4", name="recording.mp4", kind="video"), video),
    ]


def test_single_turn_is_read_only_and_contains_multimodal_context(tmp_path: Path, bundle) -> None:
    repo = tmp_path / "repo"; repo.mkdir()
    transport = FakeTransport()
    result = CodexAppServerDiagnosis(transport, model="codex-test").diagnose(repo, bundle, materialized(tmp_path))
    assert result.confidence == 82
    turn = next(params for method, params in transport.requests if method == "turn/start")
    assert turn["cwd"] == str(repo.resolve())
    assert turn["sandboxPolicy"] == {"type": "readOnly"}
    assert [item["type"] for item in turn["input"]] == ["text", "localImage"]
    assert "understand_video" in turn["input"][0]["text"] and "om_video" in turn["input"][0]["text"]
    schema = turn["outputSchema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    evidence_schema = schema["$defs"]["CodeEvidence"]
    assert evidence_schema["additionalProperties"] is False
    assert set(evidence_schema["required"]) == set(evidence_schema["properties"])


def test_explicit_skill_is_discovered_and_sent_as_native_input(tmp_path: Path, bundle) -> None:
    repo = tmp_path / "repo"; repo.mkdir()
    skill_path = repo / ".agents/skills/project-bugfix/SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("---\nname: project-bugfix\ndescription: Diagnose bugs\n---\n", encoding="utf-8")
    final = json.dumps({**json.loads(VALID), "skills_used": ["project-bugfix"]})
    transport = FakeTransport(final)

    def request(method, params):
        transport.requests.append((method, params))
        if method == "skills/list":
            return {"data": [{"cwd": str(repo.resolve()), "errors": [], "skills": [{
                "name": "project-bugfix", "path": str(skill_path.resolve()),
                "description": "Diagnose bugs", "enabled": True, "scope": "repo",
            }]}]}
        if method == "thread/start":
            return {"thread": {"id": "thread-1"}}
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        return {}

    transport.request = request
    events = []
    result = CodexAppServerDiagnosis(transport, progress=events.append).diagnose(
        repo, bundle, [], DiagnosisSkill(name="project-bugfix", path=skill_path)
    )

    turn = next(params for method, params in transport.requests if method == "turn/start")
    assert turn["input"][0] == {
        "type": "skill", "name": "project-bugfix", "path": str(skill_path.resolve())
    }
    assert result.skills_used == ["project-bugfix"]
    assert "codex.skill.selected" in [event.name for event in events]


def test_explicit_skill_must_be_discovered_by_codex(tmp_path: Path, bundle) -> None:
    repo = tmp_path / "repo"; repo.mkdir()
    skill_path = repo / ".agents/skills/project-bugfix/SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("---\nname: project-bugfix\ndescription: Diagnose bugs\n---\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="did not discover configured skill"):
        CodexAppServerDiagnosis(FakeTransport()).diagnose(
            repo, bundle, [], DiagnosisSkill(name="project-bugfix", path=skill_path)
        )


def test_invalid_final_json_is_not_retried(tmp_path: Path, bundle) -> None:
    repo = tmp_path / "repo"; repo.mkdir()
    transport = FakeTransport("invalid")
    with pytest.raises(ModelOutputError):
        CodexAppServerDiagnosis(transport, "codex-test").diagnose(repo, bundle, [])
    assert sum(method == "turn/start" for method, _ in transport.requests) == 1


def test_process_argv_requires_video_mcp(tmp_path: Path) -> None:
    captured = {}
    def factory(argv, **kwargs):
        captured.update(argv=argv, kwargs=kwargs)
        return object()
    CodexAppServerDiagnosis.start_process(factory, tmp_path, codex_bin="codex")
    assert captured["argv"][:2] == ["codex", "app-server"]
    assert any("mcp_servers.feishu_video.required=true" in arg for arg in captured["argv"])
    assert captured["kwargs"]["env"]["FEISHU_DIAGNOSE_MEDIA_ROOT"] == str(tmp_path.resolve())


def test_codex_progress_summarizes_events_without_raw_payload(tmp_path: Path, bundle) -> None:
    repo = tmp_path / "repo"; repo.mkdir()
    transport = FakeTransport()
    transport.events = lambda: iter([
        {"method": "item/started", "params": {"item": {
            "type": "commandExecution", "command": "echo secret-command"
        }}},
        {"method": "item/completed", "params": {"item": {
            "type": "mcpToolCall", "tool": "understand_video",
            "arguments": {"token": "secret-argument"}
        }}},
        {"method": "item/completed", "params": {"item": {
            "type": "agentMessage", "text": VALID
        }}},
        {"method": "turn/completed", "params": {"turn": {"status": "completed"}}},
    ])
    events = []
    client = CodexAppServerDiagnosis(transport, "codex-test", progress=events.append)

    client.diagnose(repo, bundle, [])

    names = [event.name for event in events]
    assert "codex.initialize.started" in names
    assert "codex.turn.started" in names
    assert "codex.item.started" in names
    assert "codex.item.completed" in names
    assert names[-1] == "codex.turn.completed"
    text = "\n".join(event.message for event in events)
    assert "secret-command" not in text
    assert "secret-argument" not in text


def test_stdio_transport_emits_heartbeat_during_quiet_wait(mocker) -> None:
    process = mocker.Mock()
    process.stdin = io.StringIO()
    process.stdout.fileno.return_value = 123
    clock = mocker.Mock(side_effect=[0.0, 0.0, 10.0, 10.0])
    select_call = mocker.patch(
        "feishu_chat_diagnose.codex_app_server.select.select",
        side_effect=[([], [], []), ([process.stdout], [], [])],
    )
    mocker.patch(
        "feishu_chat_diagnose.codex_app_server.os.read",
        return_value=b'{"jsonrpc":"2.0","id":1,"result":{}}\n',
    )
    events = []
    transport = StdioJsonRpcTransport(
        process, timeout_seconds=30, progress=events.append,
        heartbeat_seconds=10, clock=clock,
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
        "feishu_chat_diagnose.codex_app_server.select.select",
        return_value=([process.stdout], [], []),
    )
    mocker.patch(
        "feishu_chat_diagnose.codex_app_server.os.read",
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


def test_valid_final_answer_completes_without_turn_completed(tmp_path: Path, bundle) -> None:
    repo = tmp_path / "repo"; repo.mkdir()
    transport = FakeTransport()

    def events():
        yield {"method": "item/completed", "params": {"item": {
            "type": "agentMessage", "phase": "final_answer", "text": VALID,
        }}}
        raise TimeoutError("turn/completed was never emitted")

    transport.events = events
    progress = []

    result = CodexAppServerDiagnosis(
        transport, "codex-test", progress=progress.append
    ).diagnose(repo, bundle, [])

    assert result.confidence == 82
    assert progress[-1].name == "codex.turn.completed"
    assert "最终响应" in progress[-1].message


def test_commentary_json_does_not_complete_turn_early(tmp_path: Path, bundle) -> None:
    repo = tmp_path / "repo"; repo.mkdir()
    transport = FakeTransport()

    def events():
        yield {"method": "item/completed", "params": {"item": {
            "type": "agentMessage", "phase": "commentary", "text": VALID,
        }}}
        raise TimeoutError("still waiting for final answer")

    transport.events = events

    with pytest.raises(TimeoutError, match="still waiting"):
        CodexAppServerDiagnosis(transport, "codex-test").diagnose(repo, bundle, [])
