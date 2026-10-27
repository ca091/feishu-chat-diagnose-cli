import pytest
from typer.testing import CliRunner

from feishu_chat_diagnose.case_store import CaseStore
from feishu_chat_diagnose.config import Settings
from feishu_chat_diagnose.cli import REQUIRED_FEISHU_SCOPES, _render_progress, app, build_runtime
from feishu_chat_diagnose.progress import ProgressEvent


def test_analyze_rejects_group_and_person_together() -> None:
    result = CliRunner().invoke(app, ["analyze", "--group", "G", "--person", "P", "--repo", "r"])
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


def test_help_lists_operator_commands() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("init", "doctor", "video-check", "analyze", "list", "show", "resume"):
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
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)

    result = CliRunner().invoke(app, ["doctor"])

    assert result.exit_code == 1
    assert "DASHSCOPE_API_KEY or video_tool.api_key" in result.output
    assert "Traceback" not in result.output


@pytest.mark.parametrize("environment_key", [None, "environment-video-key"])
def test_doctor_and_runtime_forward_resolved_video_credentials(
    tmp_path, monkeypatch, mocker, environment_key
) -> None:
    settings = Settings(video_tool={"api_key": "configured-video-key", "base_url": "https://gateway.example/compatible-mode/v1"})
    if environment_key:
        monkeypatch.setenv("DASHSCOPE_API_KEY", environment_key)
    else:
        monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    mocker.patch("feishu_chat_diagnose.cli.load_settings", return_value=settings)
    mocker.patch("feishu_chat_diagnose.cli.subprocess.run")
    mocker.patch("feishu_chat_diagnose.cli.LarkCliRunner.run", return_value={})
    launch = mocker.patch("feishu_chat_diagnose.cli.CodexAppServerDiagnosis.launch")

    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "video provider credentials: present" in result.output
    launch.return_value.close.assert_called_once()
    assert "configured-video-key" not in result.output
    assert "environment-video-key" not in result.output
    doctor_options = launch.call_args.kwargs

    build_runtime(settings).codex_factory(tmp_path)
    for options in (doctor_options, launch.call_args.kwargs):
        assert options["video_api_key"] == (environment_key or "configured-video-key")
        assert options["video_model"] == "qwen3-vl-plus"
        assert options["video_base_url"] == "https://gateway.example/compatible-mode/v1"


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


@pytest.mark.parametrize("args", [
    ["doctor"], ["list"], ["show", "missing"], ["resume", "missing"],
    ["analyze", "--group", "Test", "--repo", "demo"],
])
def test_missing_global_config_explains_init_without_traceback(
    diagnose_home, tmp_path, monkeypatch, args
) -> None:
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 1
    assert "feishu-diagnose init" in result.output
    assert "Traceback" not in result.output


def test_help_works_before_initialization(diagnose_home) -> None:
    assert CliRunner().invoke(app, ["--help"]).exit_code == 0
    assert not diagnose_home.exists()


def test_case_commands_share_global_storage_from_any_directory(
    diagnose_home, tmp_path, monkeypatch
) -> None:
    diagnose_home.mkdir()
    (diagnose_home / "config.toml").write_text(
        '[defaults]\ncase_dir = "cases"\n', encoding="utf-8"
    )
    store = CaseStore(diagnose_home / "cases")
    state = store.create({"repo_alias": "demo"})
    store.transition(state.case_id, "resolving_conversation")
    store.fail(state.case_id, "resolving_conversation", "example failure")
    store.write_text(state.case_id, "report.md", "global report")

    def fake_resume(runtime, case_id, **kwargs):
        assert runtime.store.root == diagnose_home / "cases"
        return runtime.store.read_state(case_id)

    monkeypatch.setattr("feishu_chat_diagnose.cli.DiagnosisOrchestrator.resume", fake_resume)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    for cwd in (tmp_path, unrelated):
        monkeypatch.chdir(cwd)
        result = CliRunner().invoke(app, ["list"])
        assert result.exit_code == 0, result.output
        assert state.case_id in result.output
        result = CliRunner().invoke(app, ["show", state.case_id])
        assert result.exit_code == 0, result.output
        assert "global report" in result.output
        result = CliRunner().invoke(app, ["resume", state.case_id])
        assert result.exit_code == 1, result.output
        assert "stage: failed" in result.output


@pytest.mark.parametrize("command", ["analyze", "resume"])
@pytest.mark.parametrize("stage,error,exit_code", [
    ("failed", "diagnosis failed", 1), ("failed", "", 1),
    ("completed", "", 0), ("cancelled", "", 0),
])
def test_diagnosis_commands_signal_failure_to_shell(tmp_path, mocker, command, stage, error, exit_code) -> None:
    store = CaseStore(tmp_path / "cases")
    state = store.create({"repo_alias": "demo"}).model_copy(update={"stage": stage, "error": error})
    runtime = mocker.Mock(store=store)
    runtime.analyze.return_value = state
    runtime.resume.return_value = state
    mocker.patch("feishu_chat_diagnose.cli.load_settings", return_value=Settings())
    mocker.patch("feishu_chat_diagnose.cli.build_runtime", return_value=runtime)
    args = (["analyze", "--group", "Test", "--repo", "demo"] if command == "analyze"
            else ["resume", state.case_id])

    result = CliRunner().invoke(app, args)

    assert result.exit_code == exit_code, result.output
    assert f"stage: {stage}" in result.output
    if error:
        assert f"error: {error}" in result.output
