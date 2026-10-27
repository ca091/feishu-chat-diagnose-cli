from concurrent.futures import ThreadPoolExecutor
import tomllib

import pytest
from typer.testing import CliRunner

from feishu_chat_diagnose.cli import app
from feishu_chat_diagnose.config import load_settings
from feishu_chat_diagnose.initialization import initialize


def test_init_creates_usable_template_without_external_calls(
    diagnose_home, tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)

    def unexpected(*args, **kwargs):
        raise AssertionError("init must not invoke external services")

    monkeypatch.setattr("feishu_chat_diagnose.cli.build_runtime", unexpected)
    monkeypatch.setattr("feishu_chat_diagnose.cli.subprocess.run", unexpected)
    monkeypatch.setattr("feishu_chat_diagnose.cli.CodexAppServerDiagnosis.launch", unexpected)
    result = CliRunner().invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    assert str(diagnose_home / "config.toml") in result.output
    assert "feishu-diagnose doctor" in result.output
    assert (diagnose_home / "cases").is_dir()
    settings = load_settings()
    assert settings.repositories == {}
    assert settings.defaults.case_dir == diagnose_home / "cases"
    assert settings.video_tool.model == "qwen3-vl-plus"
    assert settings.video_tool.api_key.get_secret_value() == ""


def test_init_preserves_existing_config_and_ignores_env(
    diagnose_home, tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    other = tmp_path / "other.toml"
    other.write_text("# unrelated config\n", encoding="utf-8")
    monkeypatch.setenv("FEISHU_DIAGNOSE_CONFIG", str(other))
    diagnose_home.mkdir()
    config = diagnose_home / "config.toml"
    config.write_text("# user config\n", encoding="utf-8")
    before = config.stat().st_mtime_ns
    for _ in range(2):
        result = CliRunner().invoke(app, ["init"])
        assert result.exit_code == 0, result.output
        assert "保留原文件" in result.output
        assert "FEISHU_DIAGNOSE_CONFIG" in result.output
    assert config.read_text() == "# user config\n"
    assert config.stat().st_mtime_ns == before
    assert other.read_text() == "# unrelated config\n"
    assert (diagnose_home / "cases").is_dir()


def test_init_migrates_only_config_and_preserves_legacy_files(
    diagnose_home, tmp_path, monkeypatch
) -> None:
    project = tmp_path / "old project"
    source = project / "config/config.toml"
    source.parent.mkdir(parents=True)
    old_cases = project / "data/cases/old-case"
    old_cases.mkdir(parents=True)
    marker = old_cases / "state.json"
    marker.write_text('{"legacy": true}', encoding="utf-8")
    repo = project / "relative repo"
    skill = repo / ".agents/skills/bugfix/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("test skill", encoding="utf-8")
    original = '''# keep this comment
[defaults]
case_dir = "data/cases" # case location
max_messages = 100
[repositories.demo]
path = "relative repo" # repository comment
diagnosis_skill = "bugfix"
allowed_skills = ["bugfix", "performance"]
[codex]
bin = "./tools/codex"
model = "custom-model"
[feishu]
lark_cli_bin = "lark-cli"
[video_tool]
model = "video-model"
[extension]
unknown = "keep me"
'''
    source.write_text(original, encoding="utf-8")
    monkeypatch.chdir(project)
    result = CliRunner().invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    assert "旧案例未迁移" in result.output
    migrated = (diagnose_home / "config.toml").read_text()
    assert "# keep this comment" in migrated
    assert "# repository comment" in migrated
    assert tomllib.loads(migrated)["extension"]["unknown"] == "keep me"
    settings = load_settings()
    assert settings.defaults.case_dir == diagnose_home / "cases"
    assert settings.defaults.max_messages == 100
    assert settings.repository("demo").path == repo
    assert settings.diagnosis_skill("demo").name == "bugfix"
    assert settings.repositories["demo"].allowed_skills == ["bugfix", "performance"]
    assert settings.codex.bin == str(project / "tools/codex")
    assert settings.codex.model == "custom-model"
    assert settings.video_tool.model == "video-model"
    assert settings.feishu.lark_cli_bin == "lark-cli"
    assert not list((diagnose_home / "cases").iterdir())
    assert source.read_text() == original
    assert marker.read_text() == '{"legacy": true}'


@pytest.mark.parametrize("text", [
    "not valid toml", '[repositories.demo]\npath = 12', '[defaults]\ncase_dir = 12',
])
def test_invalid_legacy_config_does_not_create_template(
    diagnose_home, tmp_path, monkeypatch, text
) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "config/config.toml"
    source.parent.mkdir()
    source.write_text(text, encoding="utf-8")
    result = CliRunner().invoke(app, ["init"])
    assert result.exit_code == 1
    assert "Cannot migrate configuration" in result.output
    assert not (diagnose_home / "config.toml").exists()
    assert source.read_text() == text


def test_init_does_not_search_parent_directories(diagnose_home, tmp_path, monkeypatch) -> None:
    source = tmp_path / "config/config.toml"
    source.parent.mkdir()
    source.write_text("invalid legacy config", encoding="utf-8")
    child = tmp_path / "child"
    child.mkdir()
    monkeypatch.chdir(child)
    assert initialize().status == "created"
    assert load_settings().repositories == {}


def test_concurrent_init_publishes_one_complete_config(
    diagnose_home, tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: initialize(), range(4)))
    assert sum(result.status == "created" for result in results) == 1
    assert sum(result.status == "existing" for result in results) == 3
    assert load_settings().defaults.case_dir == diagnose_home / "cases"
    assert not list(diagnose_home.glob(".config-*"))
