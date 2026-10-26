from __future__ import annotations

import os
from pathlib import Path
import re
import tomllib

from pydantic import BaseModel, Field

from .errors import ConfigurationError
from .models import DiagnosisSkill, RepositorySettings


SKILL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def global_directory() -> Path:
    return Path.home() / ".feishu-diagnose"


def default_case_directory() -> Path:
    return global_directory() / "cases"


class DefaultsSettings(BaseModel):
    message_window: str = "2h"
    case_dir: Path = Field(default_factory=default_case_directory)
    max_messages: int = 300
    max_text_attachment_bytes: int = 1_048_576


class FeishuSettings(BaseModel):
    lark_cli_bin: str = "lark-cli"


class CodexSettings(BaseModel):
    bin: str = "codex"
    model: str = ""
    timeout_seconds: float = 600.0


class VideoToolSettings(BaseModel):
    provider: str = "gemini"
    model: str = ""
    timeout_seconds: float = 300.0


class Settings(BaseModel):
    defaults: DefaultsSettings = Field(default_factory=DefaultsSettings)
    feishu: FeishuSettings = Field(default_factory=FeishuSettings)
    codex: CodexSettings = Field(default_factory=CodexSettings)
    video_tool: VideoToolSettings = Field(default_factory=VideoToolSettings)
    repositories: dict[str, RepositorySettings] = Field(default_factory=dict)

    def repository(self, alias: str) -> RepositorySettings:
        repository = self.repositories.get(alias)
        if repository is None:
            raise ConfigurationError(f"Unknown repository alias: {alias}")
        path = repository.path.expanduser().resolve()
        if not path.exists() or not path.is_dir():
            raise ConfigurationError(f"Repository path does not exist or is not a directory: {path}")
        return repository.model_copy(update={"path": path})

    def diagnosis_skill(self, alias: str, override: str = "") -> DiagnosisSkill | None:
        repository = self.repository(alias)
        selected = override or repository.diagnosis_skill
        if not selected:
            return None
        if override and selected != repository.diagnosis_skill and selected not in repository.allowed_skills:
            raise ConfigurationError(
                f"Diagnosis skill '{selected}' is not allowed for repository '{alias}'"
            )
        if not SKILL_NAME_PATTERN.fullmatch(selected) or selected in {".", ".."}:
            raise ConfigurationError(f"Invalid diagnosis skill name: {selected}")
        skills_root = (repository.path / ".agents" / "skills").resolve()
        skill_path = (skills_root / selected / "SKILL.md").resolve()
        try:
            skill_path.relative_to(skills_root)
        except ValueError as exc:
            raise ConfigurationError(
                f"Diagnosis skill escapes repository skill root: {selected}"
            ) from exc
        if not skill_path.is_file():
            raise ConfigurationError(f"Skill file does not exist: {skill_path}")
        return DiagnosisSkill(name=selected, path=skill_path)


def resolve_config_path(path: Path | None = None) -> Path:
    configured = os.environ.get("FEISHU_DIAGNOSE_CONFIG")
    selected = path if path is not None else (
        Path(configured) if configured else global_directory() / "config.toml"
    )
    return selected.expanduser().resolve()


def resolve_path(path: Path, base: Path) -> Path:
    expanded = path.expanduser()
    return (expanded if expanded.is_absolute() else base / expanded).resolve()


def resolve_executable(command: str, base: Path) -> str:
    # Bare executable names must remain available for PATH lookup.
    if "/" in command or "\\" in command or command.startswith(("~", ".")):
        return str(resolve_path(Path(command), base))
    return command


def normalize_paths(settings: Settings, base: Path) -> Settings:
    settings.defaults.case_dir = resolve_path(settings.defaults.case_dir, base)
    for repository in settings.repositories.values():
        repository.path = resolve_path(repository.path, base)
    settings.feishu.lark_cli_bin = resolve_executable(settings.feishu.lark_cli_bin, base)
    settings.codex.bin = resolve_executable(settings.codex.bin, base)
    return settings


def load_settings(path: Path | None = None) -> Settings:
    config_path = resolve_config_path(path)
    if not config_path.exists():
        raise ConfigurationError(
            f"Configuration file does not exist: {config_path}. "
            "Run 'feishu-diagnose init' to initialize the global configuration."
        )
    try:
        payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
        settings = normalize_paths(Settings.model_validate(payload), config_path.parent)
    except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
        raise ConfigurationError(f"Invalid configuration: {exc}") from exc
    for alias in settings.repositories:
        settings.repository(alias)
        settings.diagnosis_skill(alias)
    return settings
