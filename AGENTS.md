# Repository Guidelines

## Project Structure & Module Organization

- `src/feishu_chat_diagnose/` contains the Python CLI. `cli.py` defines commands; `orchestrator.py` coordinates diagnosis; `triage.py` runs isolated discussion classification and Skill applicability assessment; `feishu.py` and `lark_runner.py` handle Feishu access. `codex_app_server.py` exposes the client video tool; `video_tool.py` enforces local-first routing and caching; `video_local.py` extracts frames and runs isolated Codex perception; `video_mcp.py` provides the lazy MCP fallback.
- `models.py`, `config.py`, `case_store.py`, and `report.py` define schemas, settings, persisted cases, and reports.
- `codex_protocol.py` owns shared JSON-RPC transport, request/session deadlines, and strict output schemas. Keep classification isolation and diagnostic event policies in their respective callers.
- `codex_session.py` shares the read-only session lifecycle and tool-free configuration baseline. Classification and video callers retain their effective isolation settings, event validation, and error messages.
- `tests/` mirrors source modules; `conftest.py` provides shared fixtures and a fake Lark runner.
- `src/feishu_chat_diagnose/config.example.toml` is the sole configuration template, bundled with the package. Runtime cases default to `~/.feishu-diagnose/cases/`; downloaded media is temporary.
- New `analyze` runs create a case and execution log only when entering code diagnosis, after classification, validation, confirmations, and attachment preparation. Earlier failures, summary-only results, and cancellations leave no case artifacts. `init` may create the empty cases root directory.
- Persist routing and Skill decisions for cases that enter diagnosis. `resume` reuses the existing case and those decisions; preserve recovery support for legacy cases that failed in earlier stages.
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

Use pytest and pytest-mock. Name files `test_<module>.py` and functions `test_<behavior>`. Mock external services and subprocesses; use `tmp_path` for filesystem fixtures. Cover changed behavior, failure handling, and cleanup, including the no-case boundary before diagnosis and exclusion of unrelated messages and attachments. No coverage threshold is configured. For integration changes, follow the README's manual smoke test with a test chat, screenshot, and MP4.

## Commit & Pull Request Guidelines

Prefer concise, imperative commit messages with a suitable prefix such as `fix:` or `feat:`. PRs should explain the behavior change, link relevant issues, and report automated or manual validation. Include CLI output when it clarifies user-facing changes.

## Security & Configuration

Prefer `DASHSCOPE_API_KEY` in the environment; a nonempty value overrides `video_tool.api_key` in local configuration. Never commit credentials, local configuration, chat checkpoints, or downloaded media. Preserve read-only diagnosis, repository and skill allowlists, confirmation prompts, explicit resume behavior, and temporary-resource cleanup. Classification must use only the configured project alias/description, chat text, and message relationships, without tools, Skills, repository inspection, or attachment access. Execution logs must not contain chat text or model-generated Skill assessment reasons; keep detailed reasons in the preview and case checkpoint.
