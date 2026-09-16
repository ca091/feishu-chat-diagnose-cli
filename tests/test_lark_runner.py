import json

import pytest

from feishu_chat_diagnose.errors import LarkCliError
from feishu_chat_diagnose.lark_runner import LarkCliRunner


def test_runner_raises_sanitized_structured_error(mocker) -> None:
    completed = mocker.Mock(
        returncode=3,
        stdout="",
        stderr=json.dumps(
            {
                "ok": False,
                "error": {
                    "type": "auth",
                    "message": "token secret-value failed",
                    "hint": "login again",
                },
            }
        ),
    )
    mocker.patch("subprocess.run", return_value=completed)

    with pytest.raises(LarkCliError) as caught:
        LarkCliRunner(secret_values={"secret-value"}).run(["auth", "check"])

    assert "secret-value" not in str(caught.value)
    assert "auth" in str(caught.value)


def test_runner_uses_argument_array_without_shell(mocker) -> None:
    completed = mocker.Mock(returncode=0, stdout='{"ok": true}', stderr="")
    run = mocker.patch("subprocess.run", return_value=completed)

    result = LarkCliRunner(binary="lark-cli").run(["im", "+chat-list"])

    assert result == {"ok": True}
    assert run.call_args.args[0] == ["lark-cli", "im", "+chat-list"]
    assert run.call_args.kwargs["shell"] is False


def test_runner_preserves_scope_check_details(mocker) -> None:
    completed = mocker.Mock(
        returncode=1,
        stdout=json.dumps(
            {
                "ok": False,
                "granted": ["im:chat:read"],
                "missing": ["search:message"],
                "suggestion": 'lark-cli auth login --scope "search:message"',
            }
        ),
        stderr="",
    )
    mocker.patch("subprocess.run", return_value=completed)

    with pytest.raises(LarkCliError) as caught:
        LarkCliRunner().run(["auth", "check"])

    assert "missing scopes: search:message" in str(caught.value)
    assert "lark-cli auth login" in str(caught.value)
