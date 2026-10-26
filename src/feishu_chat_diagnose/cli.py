from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Annotated

import typer

from .case_store import CaseStore
from .codex_app_server import CodexAppServerDiagnosis
from .config import Settings, load_settings
from .errors import ConfigurationError
from .feishu import FeishuGateway
from .initialization import initialize
from .lark_runner import LarkCliRunner
from .models import ConversationCandidate
from .orchestrator import AnalyzeRequest, DiagnosisOrchestrator
from .progress import ProgressEvent, ProgressSink
from .time_range import resolve_time_range

app = typer.Typer(no_args_is_help=True, help="Diagnose Feishu discussions against a local repository.")
REQUIRED_FEISHU_SCOPES = (
    "im:message:readonly",
    "im:chat:read",
    "im:message.group_msg:get_as_user",
    "im:message.p2p_msg:get_as_user",
    "contact:user.base:readonly",
)


def _load_settings(config: Path | None) -> Settings:
    try:
        return load_settings(config)
    except ConfigurationError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@app.command()
def init() -> None:
    """初始化 ~/.feishu-diagnose；发现当前目录的旧配置时仅迁移配置。"""
    try:
        result = initialize()
    except ConfigurationError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if result.status == "existing":
        typer.echo("全局配置已存在，保留原文件。")
    elif result.status == "migrated":
        typer.echo(f"已迁移配置：{result.source}（原文件保留）")
        typer.echo("案例目录已改为全局位置；旧案例未迁移，仍保留在原处。")
    else:
        typer.echo("已生成带注释的配置模板。")
    typer.echo(f"配置文件：{result.config_path}\n默认案例目录：{result.case_directory}")
    typer.echo("请编辑配置中的仓库、Skill 和视频模型，完成授权并设置 GEMINI_API_KEY 后运行 feishu-diagnose doctor。")
    if os.environ.get("FEISHU_DIAGNOSE_CONFIG"):
        typer.echo("提示：FEISHU_DIAGNOSE_CONFIG 已设置，普通命令仍优先使用它；init 始终初始化全局位置。")


def build_runtime(
    settings: Settings, progress_sink: ProgressSink | None = None
) -> DiagnosisOrchestrator:
    feishu = FeishuGateway(LarkCliRunner(settings.feishu.lark_cli_bin))
    store = CaseStore(settings.defaults.case_dir)

    def codex_factory(media_root: Path) -> CodexAppServerDiagnosis:
        return CodexAppServerDiagnosis.launch(
            media_root, codex_bin=settings.codex.bin, model=settings.codex.model,
            video_model=settings.video_tool.model,
            video_timeout_seconds=settings.video_tool.timeout_seconds,
            timeout_seconds=settings.codex.timeout_seconds,
        )

    return DiagnosisOrchestrator(
        settings, store, feishu, codex_factory, progress_sink=progress_sink
    )


@app.command()
def analyze(
    repo: Annotated[str, typer.Option("--repo", help="Configured repository alias")],
    group: Annotated[str, typer.Option("--group")] = "",
    person: Annotated[str, typer.Option("--person")] = "",
    since: Annotated[str, typer.Option("--since")] = "",
    start: Annotated[str, typer.Option("--start")] = "",
    end: Annotated[str, typer.Option("--end")] = "",
    keyword: Annotated[str, typer.Option("--keyword")] = "",
    skill: Annotated[str, typer.Option("--skill", help="Override repository diagnosis Skill")] = "",
    config: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    if bool(group) == bool(person):
        raise typer.BadParameter("--group and --person are mutually exclusive; specify exactly one")
    settings = _load_settings(config)
    time_range = resolve_time_range(
        since=since or (settings.defaults.message_window if not start and not end else None),
        start=start or None, end=end or None,
    )
    request = AnalyzeRequest(group_name=group, person_name=person, repo_alias=repo,
                             time_range=time_range, keyword=keyword, skill=skill)
    state = build_runtime(settings, _render_progress).analyze(
        request, _choose_candidate, _confirm
    )
    typer.echo(f"case: {state.case_id}\nstage: {state.stage}")
    if state.error:
        typer.echo(f"error: {state.error}", err=True)


@app.command("list")
def list_cases(config: Annotated[Path | None, typer.Option("--config")] = None) -> None:
    store = CaseStore(_load_settings(config).defaults.case_dir)
    for state in store.list_states():
        typer.echo(f"{state.case_id}\t{state.stage}\t{state.updated_at.isoformat()}")


@app.command()
def show(case_id: str, config: Annotated[Path | None, typer.Option("--config")] = None) -> None:
    store = CaseStore(_load_settings(config).defaults.case_dir)
    typer.echo(store.read_state(case_id).model_dump_json(indent=2))
    for name in ("report.md", "failure.json"):
        path = store.case_dir(case_id) / name
        if path.exists():
            typer.echo("\n" + path.read_text(encoding="utf-8"))
            break
    log_path = store.case_dir(case_id) / "execution.log"
    if log_path.exists():
        lines = log_path.read_text(encoding="utf-8").splitlines()
        typer.echo("\n最近执行日志（最多 30 行）：")
        typer.echo("\n".join(lines[-30:]))


@app.command()
def resume(case_id: str, config: Annotated[Path | None, typer.Option("--config")] = None) -> None:
    runtime = build_runtime(_load_settings(config), _render_progress)
    before = runtime.store.read_state(case_id)
    typer.echo(f"Resuming failed stage: {before.failed_stage}")
    state = runtime.resume(case_id, choose=_choose_candidate, confirm=_confirm)
    typer.echo(f"case: {state.case_id}\nstage: {state.stage}")
    if state.error:
        typer.echo(f"error: {state.error}", err=True)


@app.command()
def doctor(config: Annotated[Path | None, typer.Option("--config")] = None) -> None:
    settings = _load_settings(config)
    typer.echo("configuration: ok")
    for alias in settings.repositories:
        settings.repository(alias)
        repository = settings.repositories[alias]
        for skill_name in dict.fromkeys(
            [repository.diagnosis_skill, *repository.allowed_skills]
        ):
            if skill_name:
                settings.diagnosis_skill(alias, skill_name)
    typer.echo("repositories: ok")
    subprocess.run([settings.feishu.lark_cli_bin, "--version"], check=True, capture_output=True,
                   text=True, shell=False)
    typer.echo("lark-cli: ok")
    LarkCliRunner(settings.feishu.lark_cli_bin).run([
        "auth", "check", "--scope",
        " ".join(REQUIRED_FEISHU_SCOPES), "--json",
    ])
    typer.echo("feishu authorization: ok")
    if not os.environ.get("GEMINI_API_KEY"):
        typer.echo("Error: GEMINI_API_KEY is not set", err=True)
        raise typer.Exit(code=1)
    if not settings.video_tool.model:
        typer.echo("Error: video_tool.model is not configured", err=True)
        raise typer.Exit(code=1)
    typer.echo("video provider credentials: present")
    with tempfile.TemporaryDirectory(prefix="feishu-doctor-") as directory:
        codex = CodexAppServerDiagnosis.launch(
            Path(directory), codex_bin=settings.codex.bin, model=settings.codex.model,
            video_model=settings.video_tool.model,
            video_timeout_seconds=settings.video_tool.timeout_seconds,
            timeout_seconds=settings.codex.timeout_seconds,
        )
        try:
            codex.transport.request(
                "initialize", {"clientInfo": {"name": "feishu-diagnose-doctor", "version": "0.1.0"}}
            )
            codex.transport.notify("initialized", {})
        finally:
            codex.close()
    typer.echo("codex app-server: ok")


def _choose_candidate(candidates: list[ConversationCandidate]) -> ConversationCandidate:
    if len(candidates) == 1:
        return candidates[0]
    typer.echo("找到多个会话：")
    for index, candidate in enumerate(candidates, start=1):
        typer.echo(f"{index}. {candidate.name} {candidate.detail} {candidate.chat_id}")
    selected = typer.prompt("请选择序号", type=int)
    if selected < 1 or selected > len(candidates):
        raise typer.BadParameter("candidate number is out of range")
    return candidates[selected - 1]


def _confirm(preview: dict) -> bool:
    if preview["kind"] == "messages":
        typer.echo(f"消息：{preview['messages']}，图片：{preview['images']}，视频：{preview['videos']}，文件：{preview['files']}")
        if preview["missing"]:
            typer.echo(f"缺失上下文：{', '.join(preview['missing'])}")
        return typer.confirm("确认使用以上内容进行只读诊断？", default=False)
    typer.echo("以下附件下载失败：")
    for failure in preview["failed"]:
        typer.echo(f"- {failure.attachment.name}: {failure.reason}")
    return typer.confirm("确认在附件不完整的情况下继续？", default=False)


def _render_progress(event: ProgressEvent) -> None:
    prefix = {"INFO": "→", "WARNING": "!", "ERROR": "×"}[event.level]
    typer.echo(f"{prefix} {event.message}", err=True)


if __name__ == "__main__":
    app()
