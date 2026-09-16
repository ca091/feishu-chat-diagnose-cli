from typer.testing import CliRunner

from feishu_chat_diagnose.case_store import CaseStore
from feishu_chat_diagnose.config import Settings
from feishu_chat_diagnose.cli import REQUIRED_FEISHU_SCOPES, _render_progress, app
from feishu_chat_diagnose.progress import ProgressEvent


def test_analyze_rejects_group_and_person_together() -> None:
    result = CliRunner().invoke(app, ["analyze", "--group", "G", "--person", "P", "--repo", "r"])
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


def test_help_lists_operator_commands() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("doctor", "analyze", "list", "show", "resume"):
        assert command in result.output


def test_analyze_help_lists_skill_override() -> None:
    result = CliRunner().invoke(app, ["analyze", "--help"])
    assert result.exit_code == 0
    assert "--skill" in result.output


def test_doctor_requires_only_scopes_used_by_the_cli() -> None:
    assert "search:message" not in REQUIRED_FEISHU_SCOPES
    assert "im:message:readonly" in REQUIRED_FEISHU_SCOPES


def test_doctor_reports_missing_video_key_without_traceback(monkeypatch) -> None:
    monkeypatch.setattr("feishu_chat_diagnose.cli.load_settings", lambda config: Settings())
    monkeypatch.setattr("feishu_chat_diagnose.cli.subprocess.run", lambda *args, **kwargs: None)
    monkeypatch.setattr("feishu_chat_diagnose.cli.LarkCliRunner.run", lambda *args, **kwargs: {})
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    result = CliRunner().invoke(app, ["doctor"])

    assert result.exit_code == 1
    assert "GEMINI_API_KEY is not set" in result.output
    assert "Traceback" not in result.output


def test_progress_renderer_flushes_user_facing_message(capsys) -> None:
    _render_progress(ProgressEvent("INFO", "codex.heartbeat", "Codex 正在执行，已等待 10 秒"))
    captured = capsys.readouterr()
    assert "Codex 正在执行，已等待 10 秒" in captured.err


def test_show_displays_only_recent_execution_log_lines(tmp_path) -> None:
    case_root = tmp_path / "cases"
    store = CaseStore(case_root)
    state = store.create({"repo_alias": "repo"})
    for index in range(35):
        store.append_text(state.case_id, "execution.log", f"line-{index}\n")
    config = tmp_path / "config.toml"
    config.write_text(
        f'[defaults]\ncase_dir = "{case_root}"\n', encoding="utf-8"
    )

    result = CliRunner().invoke(
        app, ["show", state.case_id, "--config", str(config)]
    )

    assert result.exit_code == 0
    assert "最近执行日志" in result.output
    assert "line-34" in result.output
    assert "line-0" not in result.output
