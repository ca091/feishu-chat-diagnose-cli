# Repository Guidelines

## Project Structure & Module Organization

- `src/feishu_chat_diagnose/` contains the Python CLI. `cli.py` defines commands; `orchestrator.py` coordinates diagnosis; `feishu.py` and `lark_runner.py` handle Feishu access; `codex_app_server.py` and `video_mcp.py` integrate diagnosis and video tools.
- `models.py`, `config.py`, `case_store.py`, and `report.py` define schemas, settings, persisted cases, and reports.
- `tests/` mirrors source modules; `conftest.py` provides shared fixtures and a fake Lark runner.
- `src/feishu_chat_diagnose/config.example.toml` is the sole configuration template, bundled with the package. Runtime cases default to `~/.feishu-diagnose/cases/`; downloaded media is temporary.
- When `.codegraph/` exists, use CodeGraph exploration before searching or reading code to locate relevant symbols and call paths.

## Build, Test, and Development Commands

Use Python 3.12 or newer, running commands from the repository root:

- `uv sync --python 3.12 --extra dev` installs runtime and development dependencies.
- `uv run feishu-diagnose init` creates `~/.feishu-diagnose/config.toml` from the bundled template, or migrates the current directory's legacy `config/config.toml`. Configure repository paths, skills, and the video model there.
- `uv run feishu-diagnose --help` lists CLI commands.
- `uv run feishu-diagnose doctor` checks configuration, external tools, authorization, and credentials.
- `uv run feishu-diagnose analyze --group "Test group" --repo example --since 2h` runs an interactive diagnosis.
- `uv run pytest` runs the suite; `uv run pytest tests/test_orchestrator.py` targets one module.
- `uv build` builds distribution artifacts using setuptools.

## Coding Style & Naming Conventions

Use four-space indentation, type annotations, `snake_case` functions and modules, `PascalCase` classes, and `UPPER_CASE` constants. Follow nearby code and keep external integrations separate from orchestration. Use Pydantic models to validate external inputs, configuration, and model responses. Internal data objects may use dataclasses; use `frozen=True` when they should be immutable. Use `pathlib.Path` for paths. No formatter or linter is currently configured.

## Testing Guidelines

Use pytest and pytest-mock. Name files `test_<module>.py` and functions `test_<behavior>`. Mock external services and subprocesses; use `tmp_path` for filesystem fixtures. Cover changed behavior, failure handling, and cleanup. No coverage threshold is configured. For integration changes, follow the README's manual smoke test with a test chat, screenshot, and MP4.

## Commit & Pull Request Guidelines

History currently contains `init` and `fix: config`; prefer concise, imperative messages with a suitable prefix such as `fix:` or `feat:`. PRs should explain the behavior change, link relevant issues, and report automated or manual validation. Include CLI output when it clarifies user-facing changes.

## Security & Configuration

Keep `GEMINI_API_KEY` in the environment. Never commit credentials, local configuration, chat checkpoints, or downloaded media. Preserve read-only diagnosis, repository and skill allowlists, confirmation prompts, explicit resume behavior, and temporary-resource cleanup.
