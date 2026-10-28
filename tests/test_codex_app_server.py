import json
from datetime import datetime
from pathlib import Path

import pytest

from feishu_chat_diagnose.attachments import MaterializedAttachment
from feishu_chat_diagnose.codex_app_server import CodexAppServerDiagnosis
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

    def set_request_handler(self, handler) -> None:
        self.request_handler = handler


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
    context = bundle.messages[0].model_copy(update={"message_id": "context", "text": "Earlier background"})
    bundle.messages.insert(0, context)
    bundle.analysis_message_ids = ["om_1"]
    transport = FakeTransport()
    result = CodexAppServerDiagnosis(transport, model="codex-test").diagnose(repo, bundle, materialized(tmp_path))
    assert result.confidence == 82
    turn = next(params for method, params in transport.requests if method == "turn/start")
    assert turn["cwd"] == str(repo.resolve())
    assert turn["sandboxPolicy"] == {"type": "readOnly"}
    assert [item["type"] for item in turn["input"]] == ["text", "localImage"]
    assert "understand_video" in turn["input"][0]["text"] and "om_video" in turn["input"][0]["text"]
    prompt = turn["input"][0]["text"]
    transcript = json.loads(prompt.split("TRANSCRIPT_JSON:\n", 1)[1].split("\n\nTEXT_ATTACHMENTS_JSON:", 1)[0])
    assert [(message["message_id"], message["role"]) for message in transcript] == [
        ("context", "context_only"), ("om_1", "analysis_target"),
    ]
    assert "Do not diagnose separate topics in context_only messages" in prompt
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


@pytest.mark.parametrize("environment_key", [None, "environment-video-key"])
def test_launch_defers_mcp_and_keeps_video_key_out_of_diagnostic_process(
    tmp_path, monkeypatch, mocker, environment_key
) -> None:
    import os

    if environment_key is None:
        monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    else:
        monkeypatch.setenv("DASHSCOPE_API_KEY", environment_key)
    popen = mocker.patch("feishu_chat_diagnose.codex_app_server.subprocess.Popen")
    mocker.patch("feishu_chat_diagnose.codex_app_server.StdioJsonRpcTransport")

    codex = CodexAppServerDiagnosis.launch(
        tmp_path, video_model="qwen3-vl-plus", video_api_key="configured-video-key",
        video_timeout_seconds=123, video_base_url="https://gateway.example/compatible-mode/v1",
    )

    argv = popen.call_args.args[0]
    child_env = popen.call_args.kwargs["env"]
    assert argv[:2] == ["codex", "app-server"]
    assert argv == ["codex", "app-server", "--stdio"]
    assert "DASHSCOPE_API_KEY" not in child_env
    assert codex.video_service.mcp_settings.resolve_api_key() == (environment_key or "configured-video-key")
    assert os.environ.get("DASHSCOPE_API_KEY") == environment_key
    assert "configured-video-key" not in " ".join(argv)
    assert "environment-video-key" not in " ".join(argv)
    assert codex.video_service.mcp_settings.timeout_seconds == 123


@pytest.mark.parametrize("fails", [False, True])
def test_native_video_tool_enforces_attachment_scope_and_records_failure(tmp_path, bundle, mocker, fails):
    from feishu_chat_diagnose.models import VideoUnderstandingResult
    from feishu_chat_diagnose.video_tool import VideoUnderstandingError

    transport = FakeTransport()
    service = mocker.Mock()
    service.understand_video.return_value = VideoUnderstandingResult(
        source_message_id="om_video", summary="录屏", observations=[], missing_information=[], backend="local",
    )
    if fails:
        service.understand_video.side_effect = VideoUnderstandingError("本地和 MCP 均失败")
    attachments = materialized(tmp_path)
    responses = []
    original_events = transport.events

    def events():
        arguments = {"path": str(attachments[1].local_path), "source_message_id": "wrong", "question": "看画面"}
        responses.append(transport.request_handler("item/tool/call", {"tool": "understand_video", "arguments": arguments}))
        service.understand_video.assert_not_called()
        arguments["source_message_id"] = "om_video"
        responses.append(transport.request_handler("item/tool/call", {"tool": "understand_video", "arguments": arguments}))
        yield from original_events()

    transport.events = events
    result = CodexAppServerDiagnosis(transport, video_service=service).diagnose(tmp_path, bundle, attachments)
    assert not responses[0]["success"]
    assert responses[1]["success"] is not fails
    assert bool(result.missing_information) is fails
    service.understand_video.assert_called_once()
    thread = next(p for method, p in transport.requests if method == "thread/start")
    assert thread["dynamicTools"][0]["name"] == "understand_video"
    assert thread["sandbox"] == "read-only"


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
