from pathlib import Path

from feishu_chat_diagnose.case_store import CaseStore
from feishu_chat_diagnose.progress import CaseProgressLogger, ProgressEvent


def test_progress_is_sent_to_callback_and_persisted(tmp_path: Path) -> None:
    store = CaseStore(tmp_path / "cases")
    events: list[ProgressEvent] = []
    logger = CaseProgressLogger(store, "case-1", events.append)

    logger.info("codex.turn.started", "Codex 仓库诊断已开始")

    assert events[0].name == "codex.turn.started"
    text = (store.case_dir("case-1") / "execution.log").read_text(encoding="utf-8")
    assert "INFO codex.turn.started Codex 仓库诊断已开始" in text


def test_progress_redacts_environment_secrets(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_API_KEY", "secret-value")
    store = CaseStore(tmp_path / "cases")
    events: list[ProgressEvent] = []
    logger = CaseProgressLogger(store, "case-1", events.append)

    logger.error("codex.failed", "Bearer token-value and secret-value failed")

    output = (store.case_dir("case-1") / "execution.log").read_text(encoding="utf-8")
    assert "secret-value" not in output
    assert "token-value" not in output
    assert "[REDACTED]" in output
    assert "secret-value" not in events[0].message


def test_progress_redacts_configured_secret_in_file_and_callback(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    store = CaseStore(tmp_path / "cases")
    events = []
    logger = CaseProgressLogger(store, "case-1", events.append, secrets=("configured-video-key",))
    logger.error("codex.failed", "provider rejected configured-video-key")

    output = (store.case_dir("case-1") / "execution.log").read_text(encoding="utf-8")
    assert "configured-video-key" not in output
    assert events[0].message == "provider rejected [REDACTED]"


def test_append_failure_warns_once_without_raising(tmp_path: Path, mocker) -> None:
    store = CaseStore(tmp_path / "cases")
    mocker.patch.object(store, "append_text", side_effect=OSError("disk unavailable"))
    events: list[ProgressEvent] = []
    logger = CaseProgressLogger(store, "case-1", events.append)

    logger.info("stage.one", "第一步")
    logger.info("stage.two", "第二步")

    assert [event.name for event in events] == [
        "stage.one", "logging.disabled", "stage.two"
    ]
    assert events[1].level == "WARNING"
