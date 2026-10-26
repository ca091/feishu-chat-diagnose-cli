from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, model_validator

from .attachments import AttachmentGateway, AttachmentManager, MaterializedBatch
from .case_store import CaseStore
from .config import Settings
from .conversation import ConversationAssembler
from .errors import ModelOutputError
from .models import (
    AttachmentRef, CaseState, ConversationCandidate, ConversationIdentity, Diagnosis,
    DiagnosisSkill, FetchMetadata, MessageBundle, NormalizedMessage, TimeRange,
)
from .report import render_report
from .progress import CaseProgressLogger, ProgressSink


class AnalyzeRequest(BaseModel):
    group_name: str = ""
    person_name: str = ""
    repo_alias: str
    time_range: TimeRange
    keyword: str = ""
    skill: str = ""

    @model_validator(mode="after")
    def validate_conversation_selector(self) -> AnalyzeRequest:
        if bool(self.group_name) == bool(self.person_name):
            raise ValueError("--group and --person are mutually exclusive; specify exactly one")
        return self


class FeishuPort(AttachmentGateway, Protocol):
    def search_groups(self, name: str) -> list[ConversationCandidate]: ...
    def search_people(self, name: str) -> list[ConversationCandidate]: ...
    def resolve_private_chat(self, name: str, participant_open_id: str) -> ConversationIdentity: ...
    def fetch_messages(self, chat_id: str, time_range: TimeRange) -> list[NormalizedMessage]: ...


class CodexPort(Protocol):
    def diagnose(
        self, repository: Path, bundle: MessageBundle, attachments: list[Any],
        skill: DiagnosisSkill | None = None,
    ) -> Diagnosis: ...
    def close(self) -> None: ...


ChooseCandidate = Callable[[list[ConversationCandidate]], ConversationCandidate]
Confirm = Callable[[dict[str, Any]], bool]


class DiagnosisOrchestrator:
    def __init__(
        self,
        settings: Settings,
        store: CaseStore,
        feishu: FeishuPort,
        codex_factory: Callable[[Path], CodexPort],
        progress_sink: ProgressSink | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.feishu = feishu
        self.codex_factory = codex_factory
        self.progress_sink = progress_sink
        self._loggers: dict[str, CaseProgressLogger] = {}

    def analyze(
        self,
        request: AnalyzeRequest,
        choose: ChooseCandidate,
        confirm: Confirm,
    ) -> CaseState:
        repository = self.settings.repository(request.repo_alias)
        skill = self.settings.diagnosis_skill(request.repo_alias, request.skill)
        state = self.store.create(request.model_dump(mode="json"))
        case_id = state.case_id
        logger = self._logger(case_id)
        logger.info(
            "case.created",
            f"诊断 case 已创建：{case_id}；执行日志：{self.store.case_dir(case_id) / 'execution.log'}",
        )
        if skill is not None:
            logger.info("skill.configured", f"本次诊断配置 Skill：{skill.name}")
        try:
            logger.info("conversation.resolving", "正在查找飞书会话")
            self.store.transition(case_id, "resolving_conversation")
            conversation = self._resolve(request, choose)
            self.store.write_json(case_id, "conversation.json", conversation.model_dump(mode="json"))
            logger.info("conversation.resolved", "飞书会话已确认")

            logger.info("messages.fetching", "正在拉取消息并补全引用与话题上下文")
            self.store.transition(case_id, "fetching_messages")
            raw_messages = self.feishu.fetch_messages(conversation.chat_id, request.time_range)
            if not raw_messages:
                raise RuntimeError("No messages found in the selected time range")
            fetch_metadata = self._write_fetch_checkpoint(case_id, raw_messages)
            logger.info("messages.fetched", f"消息拉取完成：{len(raw_messages)} 条")

            logger.info("context.assembling", "正在组装诊断上下文")
            self.store.transition(case_id, "assembling_context")
            messages = ConversationAssembler(self.settings.defaults.max_messages).assemble(
                raw_messages, request.keyword
            )
            if not messages:
                raise RuntimeError("No messages match the requested context filter")
            bundle = MessageBundle(
                conversation=conversation, time_range=request.time_range, messages=messages,
                missing_message_ids=fetch_metadata.missing_message_ids,
            )
            self.store.write_json(case_id, "messages.json", bundle.model_dump(mode="json"))
            logger.info("context.ready", f"诊断上下文已就绪：{len(messages)} 条消息")

            self.store.transition(case_id, "awaiting_confirmation")
            if not confirm(self._preview(bundle)):
                logger.info("confirmation.cancelled", "用户取消诊断")
                return self.store.transition(case_id, "cancelled")

            logger.info("confirmation.accepted", "用户已确认，开始只读诊断")
            self.store.transition(case_id, "preparing_attachments")
            return self._materialize_and_diagnose(
                case_id, repository.path, bundle, skill, confirm_partial=confirm
            )
        except Exception as error:
            return self._record_failure(case_id, error)

    def resume(
        self,
        case_id: str,
        choose: ChooseCandidate | None = None,
        confirm: Confirm | None = None,
    ) -> CaseState:
        state = self.store.read_state(case_id)
        if state.failed_stage is None:
            raise RuntimeError(f"Case {case_id} has no failed stage to resume")
        request = AnalyzeRequest.model_validate(self.store.read_json(case_id, "request.json"))
        repository = self.settings.repository(request.repo_alias)
        skill = self.settings.diagnosis_skill(request.repo_alias, request.skill)
        resumed = self.store.prepare_resume(case_id)
        self._logger(case_id).info(
            "resume.started", f"用户显式恢复失败阶段：{resumed.stage}"
        )
        try:
            if resumed.stage == "resolving_conversation":
                selector = choose or _choose_single
                conversation = self._resolve(request, selector)
                self.store.write_json(
                    case_id, "conversation.json", conversation.model_dump(mode="json")
                )
                return self._resume_fetch(
                    case_id, request, repository.path, conversation, skill, confirm
                )
            if resumed.stage == "fetching_messages":
                conversation = ConversationIdentity.model_validate(
                    self.store.read_json(case_id, "conversation.json")
                )
                return self._resume_fetch(
                    case_id, request, repository.path, conversation, skill, confirm,
                    enter_stage=False,
                )
            if resumed.stage == "assembling_context":
                conversation = ConversationIdentity.model_validate(
                    self.store.read_json(case_id, "conversation.json")
                )
                metadata_path = self.store.case_dir(case_id) / "fetch_metadata.json"
                if metadata_path.exists():
                    raw = [
                        NormalizedMessage.model_validate(item)
                        for item in self.store.read_json(case_id, "raw_messages.json")
                    ]
                    fetch_metadata = FetchMetadata.model_validate(
                        self.store.read_json(case_id, "fetch_metadata.json")
                    )
                else:
                    self._logger(case_id).warning(
                        "resume.legacy_checkpoint",
                        "旧案例缺少缺失上下文记录，正在重新拉取消息以恢复完整状态",
                    )
                    raw = self.feishu.fetch_messages(conversation.chat_id, request.time_range)
                    if not raw:
                        raise RuntimeError("No messages found in the selected time range")
                    fetch_metadata = self._write_fetch_checkpoint(case_id, raw)
                return self._resume_assemble(
                    case_id, request, repository.path, conversation, raw, skill, confirm,
                    fetch_metadata, enter_stage=False,
                )
            bundle = MessageBundle.model_validate(self.store.read_json(case_id, "messages.json"))
            if resumed.stage == "awaiting_confirmation":
                confirmer = confirm or _confirmation_required
                if not confirmer(self._preview(bundle)):
                    return self.store.transition(case_id, "cancelled")
                self.store.transition(case_id, "preparing_attachments")
                return self._materialize_and_diagnose(
                    case_id, repository.path, bundle, skill, confirmer,
                )
            if resumed.stage in {"preparing_attachments", "diagnosing_repository"}:
                return self._materialize_and_diagnose(
                    case_id, repository.path, bundle, skill,
                    confirm_partial=confirm or _confirmation_required,
                )
            raise RuntimeError(f"Unsupported failed stage: {resumed.stage}")
        except Exception as error:
            return self._record_failure(case_id, error)

    def _resume_fetch(
        self, case_id: str, request: AnalyzeRequest, repository: Path,
        conversation: ConversationIdentity, skill: DiagnosisSkill | None,
        confirm: Confirm | None,
        *, enter_stage: bool = True,
    ) -> CaseState:
        if enter_stage:
            self.store.transition(case_id, "fetching_messages")
        raw = self.feishu.fetch_messages(conversation.chat_id, request.time_range)
        if not raw:
            raise RuntimeError("No messages found in the selected time range")
        fetch_metadata = self._write_fetch_checkpoint(case_id, raw)
        return self._resume_assemble(
            case_id, request, repository, conversation, raw, skill, confirm, fetch_metadata
        )

    def _resume_assemble(
        self, case_id: str, request: AnalyzeRequest, repository: Path,
        conversation: ConversationIdentity, raw: list[NormalizedMessage],
        skill: DiagnosisSkill | None, confirm: Confirm | None,
        fetch_metadata: FetchMetadata,
        *, enter_stage: bool = True,
    ) -> CaseState:
        if enter_stage:
            self.store.transition(case_id, "assembling_context")
        messages = ConversationAssembler(self.settings.defaults.max_messages).assemble(
            raw, request.keyword
        )
        if not messages:
            raise RuntimeError("No messages match the requested context filter")
        bundle = MessageBundle(
            conversation=conversation, time_range=request.time_range, messages=messages,
            missing_message_ids=fetch_metadata.missing_message_ids,
        )
        self.store.write_json(case_id, "messages.json", bundle.model_dump(mode="json"))
        self.store.transition(case_id, "awaiting_confirmation")
        confirmer = confirm or _confirmation_required
        if not confirmer(self._preview(bundle)):
            return self.store.transition(case_id, "cancelled")
        self.store.transition(case_id, "preparing_attachments")
        return self._materialize_and_diagnose(
            case_id, repository, bundle, skill, confirmer
        )

    def _write_fetch_checkpoint(
        self, case_id: str, messages: list[NormalizedMessage]
    ) -> FetchMetadata:
        metadata = FetchMetadata(
            missing_message_ids=list(getattr(self.feishu, "last_missing_message_ids", []))
        )
        # Publish metadata last so an interrupted write cannot pair new metadata
        # with stale messages. An incomplete checkpoint will be fetched again.
        (self.store.case_dir(case_id) / "fetch_metadata.json").unlink(missing_ok=True)
        self.store.write_json(
            case_id, "raw_messages.json", [item.model_dump(mode="json") for item in messages]
        )
        self.store.write_json(case_id, "fetch_metadata.json", metadata.model_dump(mode="json"))
        return metadata

    def _resolve(self, request: AnalyzeRequest, choose: ChooseCandidate) -> ConversationIdentity:
        candidates = (
            self.feishu.search_groups(request.group_name)
            if request.group_name
            else self.feishu.search_people(request.person_name)
        )
        if not candidates:
            raise RuntimeError("No matching visible conversation found")
        selected = choose(candidates)
        if selected.conversation_type == "p2p":
            return self.feishu.resolve_private_chat(selected.name, selected.participant_open_id)
        return ConversationIdentity.model_validate(selected.model_dump())

    def _materialize_and_diagnose(
        self, case_id: str, repository: Path, bundle: MessageBundle,
        skill: DiagnosisSkill | None, confirm_partial: Confirm,
    ) -> CaseState:
        refs = _attachments(bundle)
        with tempfile.TemporaryDirectory(prefix="feishu-diagnose-") as directory:
            manager = AttachmentManager(
                self.feishu, Path(directory), self.settings.defaults.max_text_attachment_bytes,
                progress=self._logger(case_id).forward,
            )
            with manager.materialize(refs) as batch:
                if batch.failed and not confirm_partial(self._failure_preview(batch)):
                    result = self.store.transition(case_id, "cancelled")
                else:
                    if self.store.read_state(case_id).stage != "diagnosing_repository":
                        self.store.transition(case_id, "diagnosing_repository")
                    result = self._run_codex(
                        case_id, repository, bundle, batch, Path(directory), skill
                    )
        if result.stage == "completed":
            self._logger(case_id).info("case.completed", "诊断完成，报告已保存")
        return result

    def _run_codex(
        self, case_id: str, repository: Path, bundle: MessageBundle,
        batch: MaterializedBatch, media_root: Path, skill: DiagnosisSkill | None,
    ) -> CaseState:
        codex = self.codex_factory(media_root)
        logger = self._logger(case_id)
        set_progress = getattr(codex, "set_progress", None)
        if callable(set_progress):
            set_progress(logger.forward)
        logger.info("codex.started", "Codex 已启动，正在分析仓库")
        try:
            diagnosis = codex.diagnose(repository, bundle, batch.available, skill)
        finally:
            codex.close()
        logger.info("report.saving", "Codex 分析完成，正在保存诊断报告")
        self.store.write_json(case_id, "diagnosis.json", diagnosis.model_dump(mode="json"))
        self.store.write_text(case_id, "report.md", render_report(diagnosis))
        return self.store.transition(case_id, "completed")

    def _record_failure(self, case_id: str, error: Exception) -> CaseState:
        state = self.store.read_state(case_id)
        stage = state.stage if state.stage != "failed" else state.failed_stage
        assert stage is not None
        message = _redact(str(error))
        self._logger(case_id).error("case.failed", f"诊断失败，阶段：{stage}")
        self.store.write_json(case_id, "failure.json", {"stage": stage, "error": message})
        if isinstance(error, ModelOutputError):
            self.store.write_text(case_id, "model-response.txt", _redact(error.raw_response))
        return self.store.fail(case_id, stage, message)

    @staticmethod
    def _preview(bundle: MessageBundle) -> dict[str, Any]:
        refs = _attachments(bundle)
        return {
            "kind": "messages", "messages": len(bundle.messages),
            "images": sum(item.kind == "image" for item in refs),
            "videos": sum(item.kind == "video" for item in refs),
            "files": sum(item.kind in {"text", "unsupported"} for item in refs),
            "missing": bundle.missing_message_ids,
        }

    @staticmethod
    def _failure_preview(batch: MaterializedBatch) -> dict[str, Any]:
        return {"kind": "attachment_failures", "failed": batch.failed}

    def _logger(self, case_id: str) -> CaseProgressLogger:
        logger = self._loggers.get(case_id)
        if logger is None:
            logger = CaseProgressLogger(self.store, case_id, self.progress_sink)
            self._loggers[case_id] = logger
        return logger


def _attachments(bundle: MessageBundle) -> list[AttachmentRef]:
    return [attachment for message in bundle.messages for attachment in message.attachments
            if attachment.kind != "unsupported"]


def _redact(value: str) -> str:
    for name, secret in os.environ.items():
        if secret and any(token in name.upper() for token in ("TOKEN", "SECRET", "API_KEY", "PASSWORD")):
            value = value.replace(secret, "[REDACTED]")
    return value


def _choose_single(candidates: list[ConversationCandidate]) -> ConversationCandidate:
    if len(candidates) != 1:
        raise RuntimeError("Resume needs interactive candidate selection")
    return candidates[0]


def _confirmation_required(preview: dict[str, Any]) -> bool:
    raise RuntimeError("Resume needs interactive confirmation")
