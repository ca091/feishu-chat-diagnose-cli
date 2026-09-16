from pathlib import Path

import pytest

from feishu_chat_diagnose.config import load_settings
from feishu_chat_diagnose.errors import ConfigurationError


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
