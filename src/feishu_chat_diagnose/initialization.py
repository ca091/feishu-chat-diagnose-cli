from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
import os
from pathlib import Path
import tempfile
from typing import Literal

import tomlkit

from .config import Settings, global_directory, normalize_paths
from .errors import ConfigurationError


@dataclass(frozen=True)
class InitializationResult:
    config_path: Path
    case_directory: Path
    status: Literal["created", "migrated", "existing"]
    source: Path | None = None


def _migrate_configuration(source: Path) -> str:
    try:
        document = tomlkit.parse(source.read_text(encoding="utf-8"))
        # Old relative paths were resolved from the invoking project directory.
        settings = normalize_paths(Settings.model_validate(document.unwrap()), source.parent.parent)
        for alias, repository in settings.repositories.items():
            document["repositories"][alias]["path"] = str(repository.path)
        for section, key, command in (
            ("feishu", "lark_cli_bin", settings.feishu.lark_cli_bin),
            ("codex", "bin", settings.codex.bin),
            ("video_local", "ffmpeg_bin", settings.video_local.ffmpeg_bin),
            ("video_local", "ffprobe_bin", settings.video_local.ffprobe_bin),
        ):
            if section in document and key in document[section]:
                document[section][key] = command
        if "defaults" not in document:
            document["defaults"] = tomlkit.table()
        document["defaults"]["case_dir"] = "cases"
        return tomlkit.dumps(document)
    except (OSError, ValueError, TypeError) as exc:
        raise ConfigurationError(f"Cannot migrate configuration {source}: {exc}") from exc


def _publish_configuration(path: Path, content: str) -> bool:
    """Publish a complete file atomically, without replacing a concurrent init's file."""
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=".config-", delete=False
    ) as temporary:
        temporary_path = Path(temporary.name)
        try:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        except BaseException:
            temporary_path.unlink()
            raise
    try:
        os.link(temporary_path, path)
        return True
    except FileExistsError:
        return False
    finally:
        temporary_path.unlink()


def initialize() -> InitializationResult:
    home = global_directory()
    config_path = home / "config.toml"
    case_directory = home / "cases"
    source = Path.cwd() / "config" / "config.toml"
    status: Literal["created", "migrated"]
    try:
        if os.path.lexists(config_path):
            if not config_path.is_file():
                raise ConfigurationError(f"Configuration path is not a file: {config_path}")
            case_directory.mkdir(parents=True, exist_ok=True)
            return InitializationResult(config_path, case_directory, "existing")

        if os.path.lexists(source):
            content = _migrate_configuration(source)
            status = "migrated"
        else:
            content = files("feishu_chat_diagnose").joinpath("config.example.toml").read_text(
                encoding="utf-8"
            )
            status = "created"
        case_directory.mkdir(parents=True, exist_ok=True)
        if not _publish_configuration(config_path, content):
            return InitializationResult(config_path, case_directory, "existing")
        return InitializationResult(
            config_path, case_directory, status, source if status == "migrated" else None
        )
    except OSError as exc:
        raise ConfigurationError(f"Cannot initialize {home}: {exc}") from exc
