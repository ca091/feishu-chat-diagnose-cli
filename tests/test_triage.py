import json
from pathlib import Path

import pytest

from feishu_chat_diagnose.models import SkillAssessment
from feishu_chat_diagnose.triage import (
    CodexDiscussionClassifier, CodexTriageService, ISOLATION_CONFIG, TriageError,
)


class ClassifierTransport:
    def __init__(self, result):
        self.requests = []
        self.result = result
        self.config = {"mcp_servers": {"private": {"command": "must-not-run"}}}
        for key, value in ISOLATION_CONFIG.items():
            node = self.config
            parts = key.split(".")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = value

    def request(self, method, params):
        self.requests.append((method, params))
        if method == "config/read":
            return {"config": self.config}
        if method == "thread/start":
            return {"thread": {"id": "triage"}}
        return {}

    def notify(self, *args):
        pass

    def events(self):
        yield {"method": "item/completed", "params": {"item": {"type": "userMessage"}}}
        yield {"method": "item/completed", "params": {"item": {
            "type": "agentMessage", "phase": "final_answer", "text": json.dumps(self.result),
        }}}


def topic(message_id="message"):
    return {"kind": "other", "summary": "讨论午餐安排", "project_related": "no", "needs_code": False,
            "reason": "没有项目实现问题", "message_ids": [message_id], "missing_information": []}


def test_classifier_sends_only_chat_and_project_metadata_without_tools(tmp_path, message_factory):
    transport = ClassifierTransport({"topics": [topic()]})
    result = CodexDiscussionClassifier(transport, tmp_path).classify(
        "editor", "文档编辑器", [message_factory("message", "午餐吃什么")],
    )
    assert not result.code_message_ids
    thread = next(params for method, params in transport.requests if method == "thread/start")
    assert thread["ephemeral"] and thread["cwd"] == str(tmp_path)
    assert thread["config"] == {"mcp_servers": {"private": {"enabled": False}}}
    turn = next(params for method, params in transport.requests if method == "turn/start")
    assert [item["type"] for item in turn["input"]] == ["text"]
    payload = json.loads(turn["input"][0]["text"])
    assert payload["project"] == {"alias": "editor", "description": "文档编辑器"}
    assert set(payload["messages"][0]) == {"message_id", "text", "reply_to_message_id", "thread_root_id"}
    assert not any(method == "skills/list" for method, _ in transport.requests)


@pytest.mark.parametrize("invalid", [
    {"topics": [topic("invented")]}, {"topics": []},
])
def test_invalid_classification_does_not_fall_back_to_diagnosis(tmp_path, message_factory, invalid):
    transport = ClassifierTransport(invalid)
    with pytest.raises(TriageError):
        CodexDiscussionClassifier(transport, tmp_path).classify("editor", "", [message_factory("message", "Hi")])


def test_isolation_failure_stops_before_sending_evidence(tmp_path, message_factory):
    transport = ClassifierTransport({"topics": [topic()]})
    transport.config["features"]["shell_tool"] = True
    with pytest.raises(TriageError, match="隔离"):
        CodexDiscussionClassifier(transport, tmp_path).classify("editor", "", [message_factory("message", "Hi")])
    assert not any(method in {"thread/start", "turn/start"} for method, _ in transport.requests)


def test_unexpected_tool_event_stops_classification(tmp_path, message_factory, mocker):
    transport = ClassifierTransport({"topics": [topic()]})
    mocker.patch.object(transport, "events", return_value=iter([
        {"method": "item/started", "params": {"item": {"type": "commandExecution"}}},
    ]))
    with pytest.raises(TriageError, match="意外请求工具"):
        CodexDiscussionClassifier(transport, tmp_path).classify("editor", "", [message_factory("message", "Hi")])


def test_classifier_service_closes_process_and_workspace_on_timeout(mocker):
    process = mocker.Mock()
    captured = {}

    def popen(argv, **kwargs):
        captured.update(argv=argv, workspace=Path(kwargs["cwd"]))
        assert captured["workspace"].is_dir()
        return process

    mocker.patch("feishu_chat_diagnose.triage.subprocess.Popen", side_effect=popen)
    transport = mocker.patch("feishu_chat_diagnose.triage.StdioJsonRpcTransport").return_value
    transport.request.side_effect = TimeoutError("private payload")
    with pytest.raises(TriageError, match="未自动重试") as caught:
        CodexTriageService("codex", "", 1).assess_skill(
            # Only the schema matters here; the request fails before any input.
            mocker.Mock(topics=[]), "skill data",
        )
    assert "private payload" not in str(caught.value)
    assert not captured["workspace"].exists()
    assert "features.shell_tool=false" in captured["argv"]
    assert "skills.include_instructions=false" in captured["argv"]
    assert not any("feishu_video" in arg for arg in captured["argv"])
    transport.close.assert_called_once()
    process.communicate.assert_called_once()


def test_skill_text_is_assessed_as_data_without_native_activation(tmp_path, message_factory):
    from feishu_chat_diagnose.models import DiscussionTriage

    transport = ClassifierTransport({"applicable": False, "reason": "仅适用于缺陷排查"})
    triage = DiscussionTriage(topics=[{**topic(), "kind": "requirement", "project_related": "yes", "needs_code": True}])
    assessment = CodexDiscussionClassifier(transport, tmp_path).assess_skill(triage, "Bug fixes only")
    assert assessment == SkillAssessment(applicable=False, reason="仅适用于缺陷排查")
    turn = next(params for method, params in transport.requests if method == "turn/start")
    assert json.loads(turn["input"][0]["text"])["skill_text"] == "Bug fixes only"
    assert [item["type"] for item in turn["input"]] == ["text"]
