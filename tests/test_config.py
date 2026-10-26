from pathlib import Path

import pytest

from feishu_chat_diagnose.config import Settings, load_settings, resolve_config_path
from feishu_chat_diagnose.errors import ConfigurationError
from feishu_chat_diagnose.models import RepositorySettings


def test_load_settings_resolves_allowlisted_repository(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        f'[repositories.canvas-editor]\npath = "{repo}"\ndescription = "Canvas editor"\n',
        encoding="utf-8",
    )

    settings = load_settings(config)

    assert settings.repository("canvas-editor").path == repo.resolve()


def test_repository_rejects_unknown_alias(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")
    settings = load_settings(config)

    with pytest.raises(ConfigurationError, match="Unknown repository alias"):
        settings.repository("missing")


def test_repository_rejects_missing_directory(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        f'[repositories.missing]\npath = "{tmp_path / "missing"}"\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="does not exist"):
        load_settings(config)


def test_repository_uses_default_skill_and_allows_override(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    for name in ("project-bugfix", "canvas-bugfix"):
        skill = repo / ".agents" / "skills" / name / "SKILL.md"
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text(f"---\nname: {name}\ndescription: Test\n---\n", encoding="utf-8")
    config = tmp_path / "config.toml"
    config.write_text(
        f'''[repositories.project]
path = "{repo}"
diagnosis_skill = "project-bugfix"
allowed_skills = ["canvas-bugfix"]
''',
        encoding="utf-8",
    )

    settings = load_settings(config)

    default = settings.diagnosis_skill("project")
    override = settings.diagnosis_skill("project", "canvas-bugfix")
    assert default is not None and default.name == "project-bugfix"
    assert default.path == (repo / ".agents/skills/project-bugfix/SKILL.md").resolve()
    assert override is not None and override.name == "canvas-bugfix"


def test_repository_rejects_skill_outside_allowlist(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        f'[repositories.project]\npath = "{repo}"\nallowed_skills = ["known"]\n',
        encoding="utf-8",
    )
    settings = load_settings(config)

    with pytest.raises(ConfigurationError, match="not allowed"):
        settings.diagnosis_skill("project", "other")


def test_repository_rejects_missing_default_skill_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        f'[repositories.project]\npath = "{repo}"\ndiagnosis_skill = "missing"\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="Skill file does not exist"):
        load_settings(config)


def test_config_selection_priority_and_expansion(diagnose_home, tmp_path, monkeypatch) -> None:
    assert resolve_config_path() == diagnose_home / "config.toml"
    monkeypatch.setenv("FEISHU_DIAGNOSE_CONFIG", "~/custom/config.toml")
    assert resolve_config_path() == diagnose_home.parent / "custom/config.toml"
    monkeypatch.chdir(tmp_path)
    assert resolve_config_path(Path("explicit.toml")) == tmp_path / "explicit.toml"
    monkeypatch.setenv("FEISHU_DIAGNOSE_CONFIG", "relative.toml")
    assert resolve_config_path() == tmp_path / "relative.toml"


def test_paths_are_relative_to_config_in_any_working_directory(
    diagnose_home, tmp_path, monkeypatch
) -> None:
    diagnose_home.mkdir()
    repo = diagnose_home / "repo with spaces"
    repo.mkdir()
    (diagnose_home / "config.toml").write_text('''
[defaults]
case_dir = "cases"
[repositories.demo]
path = "repo with spaces"
[codex]
bin = "./bin/codex"
[feishu]
lark_cli_bin = "lark-cli"
''', encoding="utf-8")
    for cwd in (tmp_path, repo):
        monkeypatch.chdir(cwd)
        settings = load_settings()
        assert settings.repository("demo").path == repo
        assert settings.defaults.case_dir == diagnose_home / "cases"
        assert settings.codex.bin == str(diagnose_home / "bin/codex")
        assert settings.feishu.lark_cli_bin == "lark-cli"


def test_default_cases_are_global_even_with_custom_config(
    diagnose_home, tmp_path
) -> None:
    config = tmp_path / "custom.toml"
    config.write_text("", encoding="utf-8")
    assert load_settings(config).defaults.case_dir == diagnose_home / "cases"
    assert Settings().defaults.case_dir == diagnose_home / "cases"


def test_home_paths_and_absolute_case_override(diagnose_home, tmp_path) -> None:
    repo = diagnose_home.parent / "repo"
    repo.mkdir()
    case_dir = tmp_path / "custom cases"
    config = tmp_path / "custom.toml"
    config.write_text(f'''
[defaults]
case_dir = "{case_dir}"
[repositories.demo]
path = "~/repo"
[feishu]
lark_cli_bin = "~/bin/lark-cli"
''', encoding="utf-8")
    settings = load_settings(config)
    assert settings.defaults.case_dir == case_dir
    assert settings.repository("demo").path == repo
    assert settings.feishu.lark_cli_bin == str(repo.parent / "bin/lark-cli")
    assert settings.codex.bin == "codex"


def test_local_config_is_not_implicitly_loaded(diagnose_home, tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    legacy = tmp_path / "config/config.toml"
    legacy.parent.mkdir()
    legacy.write_text("", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="feishu-diagnose init"):
        load_settings()


@pytest.mark.parametrize("linked_path", [".agents", ".agents/skills", ".agents/skills/bugfix", ".agents/skills/bugfix/SKILL.md"])
def test_skill_rejects_symlinks_outside_repository(tmp_path, linked_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = repo / linked_path
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.name == "SKILL.md":
        target = outside / "SKILL.md"
    else:
        target = outside
    suffix = Path(".agents/skills/bugfix/SKILL.md").relative_to(linked_path)
    skill = target / suffix if str(suffix) != "." else target
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text("Test skill", encoding="utf-8")
    link.symlink_to(target, target_is_directory=target.is_dir())
    settings = Settings(repositories={"repo": RepositorySettings(path=repo, diagnosis_skill="bugfix")})

    with pytest.raises(ConfigurationError, match="escapes repository skill root"):
        settings.diagnosis_skill("repo")


def test_skill_allows_symlinks_contained_in_repository(tmp_path) -> None:
    repo = tmp_path / "repo"
    skill = repo / "agent-config/skills/bugfix/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("Test skill", encoding="utf-8")
    (repo / ".agents").symlink_to(repo / "agent-config", target_is_directory=True)
    settings = Settings(repositories={"repo": RepositorySettings(path=repo, diagnosis_skill="bugfix")})

    assert settings.diagnosis_skill("repo").path == skill.resolve()
