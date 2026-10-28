import pytest

from feishu_chat_diagnose.codex_session import ReadOnlyCodexSession


@pytest.mark.parametrize("phase", ["final_answer", None])
def test_final_response_ignores_commentary_and_supports_legacy_completion(tmp_path, mocker, phase):
    transport = mocker.Mock()
    commentary = {"method": "item/completed", "params": {"item": {
        "type": "agentMessage", "phase": "commentary", "text": "not the result",
    }}}
    answer = {"method": "item/completed", "params": {"item": {
        "type": "agentMessage", "phase": phase, "text": "result",
    }}}
    completed = {"method": "turn/completed", "params": {"turn": {"status": "completed"}}}
    transport.events.return_value = iter([commentary, answer, completed])
    validated = []

    assert ReadOnlyCodexSession(transport, tmp_path).final_response(validated.append) == "result"
    assert validated == ([commentary, answer] if phase == "final_answer" else [commentary, answer, completed])


def test_caller_can_reject_event_before_final_response_is_accepted(tmp_path, mocker):
    transport = mocker.Mock()
    transport.events.return_value = iter([{
        "method": "item/completed", "params": {"item": {
            "type": "agentMessage", "phase": "final_answer", "text": "untrusted result",
        }},
    }])

    def reject(event):
        raise RuntimeError("caller policy rejected event")

    with pytest.raises(RuntimeError, match="caller policy"):
        ReadOnlyCodexSession(transport, tmp_path).final_response(reject)
