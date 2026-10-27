from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, model_validator

from .attachments import AttachmentGateway, AttachmentManager, MaterializedBatch
from .case_store import CaseStore
from .config import Settings
from .conversation import ConversationAssembler
from .errors import ConfigurationError, ModelOutputError
from .models import (
    AttachmentRef, CaseState, ConversationCandidate, ConversationIdentity, Diagnosis,
    DiagnosisSkill, DiscussionTriage, FetchMetadata, MessageBundle, NormalizedMessage,
    SkillAssessment, TimeRange, TriageCheckpoint,
)
from .report import render_report
from .progress import CaseProgressLogger, ProgressEvent, ProgressSink, redact_secrets, sanitize_message


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


class TriagePort(Protocol):
    def classify(self, repo_alias: str, description: str, messages: list[NormalizedMessage]) -> DiscussionTriage: ...
    def assess_skill(self, triage: DiscussionTriage, skill_text: str) -> SkillAssessment: ...


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
        *, classifier: TriagePort | None = None,
        triage_sink: Callable[[DiscussionTriage], None] | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.feishu = feishu
        self.codex_factory = codex_factory
        self.progress_sink = progress_sink
        self.classifier = classifier
        self.triage_sink = triage_sink
        self._loggers: dict[str, CaseProgressLogger] = {}

    def analyze(
        self,
        request: AnalyzeRequest,
        choose: ChooseCandidate,
        confirm: Confirm,
    ) -> CaseState | DiscussionTriage | None:
        # Fetch, triage, validation, confirmations and attachments are transient.
        # Only entering code diagnosis creates a case or a case progress logger.
        repository_info = self.settings.repositories.get(request.repo_alias)
        if repository_info is None:
            raise ConfigurationError(f"Unknown repository alias: {request.repo_alias}")
        self._progress("conversation.resolving", "正在查找飞书会话")
        conversation = self._resolve(request, choose)
        self._progress("messages.fetching", "正在拉取消息并补全引用与话题上下文")
        raw_messages = self.feishu.fetch_messages(conversation.chat_id, request.time_range)
        if not raw_messages:
            raise RuntimeError("No messages found in the selected time range")
        candidates = ConversationAssembler(self.settings.defaults.max_messages).assemble(
            raw_messages, request.keyword, check_limit=False,
        )
        if not candidates:
            raise RuntimeError("No messages match the requested context filter")
        if self.classifier is None:
            raise RuntimeError("Discussion classifier is not configured")
        self._progress("triage.started", "正在根据聊天正文判断讨论类型和代码分析必要性")
        triage = self.classifier.classify(request.repo_alias, repository_info.description, candidates)
        if not triage.code_message_ids:
            return triage
        if self.triage_sink is not None:
            self.triage_sink(triage)
        # Retain necessary quote/thread context, but no independent summary-only
        # topics, in diagnostic checkpoints and attachments.
        selected = ConversationAssembler(self.settings.defaults.max_messages).assemble(
            candidates, seed_ids=triage.code_message_ids, check_limit=False,
        )
        references = {value for message in selected for value in (
            message.message_id, message.reply_to_message_id, message.thread_root_id,
        ) if value}
        metadata = FetchMetadata(missing_message_ids=[
            value for value in getattr(self.feishu, "last_missing_message_ids", []) if value in references
        ])
        checkpoint = TriageCheckpoint(
            triage=DiscussionTriage(topics=[topic for topic in triage.topics if topic.needs_code]),
            requested_skill=request.skill or repository_info.diagnosis_skill,
        )
        repository = self.settings.repository(request.repo_alias)
        bundle = self._assemble_bundle(request, conversation, selected, metadata, checkpoint)
        skill = self._resolve_skill(request, checkpoint)
        self._progress("context.ready", f"诊断上下文已就绪：{len(bundle.messages)} 条消息")
        if not confirm(self._preview(bundle, checkpoint)):
            return None
        self._progress("confirmation.accepted", "用户已确认，正在准备只读诊断")
        case_id: str | None = None
        try:
            with tempfile.TemporaryDirectory(prefix="feishu-diagnose-") as directory:
                media_root = Path(directory)
                manager = AttachmentManager(
                    self.feishu, media_root, self.settings.defaults.max_text_attachment_bytes,
                    progress=self._transient_progress,
                )
                with manager.materialize(_attachments(bundle)) as batch:
                    if batch.failed and not confirm(self._failure_preview(batch)):
                        return None
                    state = self.store.create({**request.model_dump(mode="json"), "triage_required": True})
                    case_id = state.case_id
                    self.store.transition(case_id, "diagnosing_repository")
                    self._write_triage_checkpoint(case_id, checkpoint)
                    self.store.write_json(case_id, "conversation.json", conversation.model_dump(mode="json"))
                    self._write_fetch_checkpoint(case_id, selected, metadata)
                    self.store.write_json(case_id, "messages.json", bundle.model_dump(mode="json"))
                    logger = self._logger(case_id)
                    logger.info(
                        "case.created",
                        f"诊断 case 已创建：{case_id}；执行日志：{self.store.case_dir(case_id) / 'execution.log'}",
                    )
                    if checkpoint.skill_assessment is not None and not checkpoint.skill_assessment.applicable:
                        logger.warning(
                            "skill.skipped", f"跳过 Skill {checkpoint.requested_skill}；使用通用只读代码分析",
                        )
                    result = self._run_codex(case_id, repository.path, bundle, batch, media_root, skill)
            self._logger(case_id).info("case.completed", "诊断完成，报告已保存")
            return result
        except Exception as error:
            if case_id is None:
                raise
            return self._record_failure(case_id, error)

    def _assemble_bundle(
        self, request: AnalyzeRequest, conversation: ConversationIdentity,
        raw: list[NormalizedMessage], fetch_metadata: FetchMetadata,
        checkpoint: TriageCheckpoint | None = None,
    ) -> MessageBundle:
        messages = ConversationAssembler(self.settings.defaults.max_messages).assemble(
            raw, request.keyword if checkpoint is None else "",
            seed_ids=checkpoint.triage.code_message_ids if checkpoint is not None else None,
        )
        if not messages:
            raise RuntimeError("No messages match the requested context filter")
        return MessageBundle(
            conversation=conversation, time_range=request.time_range, messages=messages,
            missing_message_ids=fetch_metadata.missing_message_ids,
            analysis_message_ids=sorted(checkpoint.triage.code_message_ids) if checkpoint else None,
            analysis_kind=("code" if checkpoint is not None and any(
                topic.kind != "bug" for topic in checkpoint.triage.topics
            ) else "bug"),
        )

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
        resumed = self.store.prepare_resume(case_id)
        self._logger(case_id).info(
            "resume.started", f"用户显式恢复失败阶段：{resumed.stage}"
        )
        try:
            repository = self.settings.repository(request.repo_alias)
            checkpoint = self._triage_checkpoint(case_id)
            skill = None
            if checkpoint is None:
                skill = self.settings.diagnosis_skill(request.repo_alias, request.skill)
            elif resumed.stage != "assembling_context":
                skill = self._resolve_skill(request, checkpoint, case_id)
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
                    if checkpoint is not None:
                        raw = ConversationAssembler(self.settings.defaults.max_messages).assemble(
                            raw, seed_ids=checkpoint.triage.code_message_ids, check_limit=False,
                        )
                    fetch_metadata = self._write_fetch_checkpoint(case_id, raw)
                return self._resume_assemble(
                    case_id, request, repository.path, conversation, raw, skill, confirm,
                    fetch_metadata, enter_stage=False,
                )
            bundle = MessageBundle.model_validate(self.store.read_json(case_id, "messages.json"))
            if checkpoint is not None:
                bundle.messages = ConversationAssembler(self.settings.defaults.max_messages).assemble(
                    bundle.messages, seed_ids=checkpoint.triage.code_message_ids,
                )
                if not bundle.messages:
                    raise RuntimeError("No messages match the requested context filter")
                bundle.analysis_message_ids = sorted(checkpoint.triage.code_message_ids)
                self.store.write_json(case_id, "messages.json", bundle.model_dump(mode="json"))
            if resumed.stage == "awaiting_confirmation":
                confirmer = confirm or _confirmation_required
                if not confirmer(self._preview(bundle, checkpoint)):
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
        checkpoint = self._triage_checkpoint(case_id)
        bundle = self._assemble_bundle(request, conversation, raw, fetch_metadata, checkpoint)
        self.store.write_json(case_id, "messages.json", bundle.model_dump(mode="json"))
        if checkpoint is not None:
            skill = self._resolve_skill(request, checkpoint, case_id)
        self._logger(case_id).info("context.ready", f"诊断上下文已就绪：{len(bundle.messages)} 条消息")
        self.store.transition(case_id, "awaiting_confirmation")
        confirmer = confirm or _confirmation_required
        if not confirmer(self._preview(bundle, checkpoint)):
            return self.store.transition(case_id, "cancelled")
        self._logger(case_id).info("confirmation.accepted", "用户已确认，开始只读诊断")
        self.store.transition(case_id, "preparing_attachments")
        return self._materialize_and_diagnose(
            case_id, repository, bundle, skill, confirmer
        )

    def _write_fetch_checkpoint(
        self, case_id: str, messages: list[NormalizedMessage], metadata: FetchMetadata | None = None,
    ) -> FetchMetadata:
        metadata = metadata if metadata is not None else FetchMetadata(
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
        self.store.write_text(case_id, "report.md", render_report(diagnosis, analysis_kind=bundle.analysis_kind))
        return self.store.transition(case_id, "completed")

    def _record_failure(self, case_id: str, error: Exception) -> CaseState:
        state = self.store.read_state(case_id)
        stage = state.stage if state.stage != "failed" else state.failed_stage
        assert stage is not None
        secrets = (self.settings.video_tool.api_key.get_secret_value(),)
        message = redact_secrets(str(error), secrets)
        self._logger(case_id).error("case.failed", f"诊断失败，阶段：{stage}")
        self.store.write_json(case_id, "failure.json", {"stage": stage, "error": message})
        if isinstance(error, ModelOutputError):
            self.store.write_text(case_id, "model-response.txt", redact_secrets(error.raw_response, secrets))
        return self.store.fail(case_id, stage, message)

    def _preview(self, bundle: MessageBundle, checkpoint: TriageCheckpoint | None = None) -> dict[str, Any]:
        refs = _attachments(bundle)
        secrets = (self.settings.video_tool.api_key.get_secret_value(),)
        assessment = checkpoint.skill_assessment if checkpoint else None
        if assessment is not None:
            assessment = assessment.model_copy(update={"reason": redact_secrets(assessment.reason, secrets)})
        return {
            "kind": "messages", "messages": len(bundle.messages),
            "images": sum(item.kind == "image" for item in refs),
            "videos": sum(item.kind == "video" for item in refs),
            "files": sum(item.kind in {"text", "unsupported"} for item in refs),
            "missing": bundle.missing_message_ids,
            "topics": [redact_secrets(topic.summary, secrets) for topic in checkpoint.triage.topics] if checkpoint else [],
            "skill": checkpoint.requested_skill if checkpoint else "",
            "skill_assessment": assessment,
        }

    def _triage_checkpoint(self, case_id: str) -> TriageCheckpoint | None:
        path = self.store.case_dir(case_id) / "triage.json"
        if path.exists():
            return TriageCheckpoint.model_validate(self.store.read_json(case_id, "triage.json"))
        if self.store.read_json(case_id, "request.json").get("triage_required"):
            raise RuntimeError("分流检查点不完整，请重新执行 analyze；不能绕过讨论判断")
        return None

    def _write_triage_checkpoint(self, case_id: str, checkpoint: TriageCheckpoint) -> None:
        secrets = (self.settings.video_tool.api_key.get_secret_value(),)
        for topic in checkpoint.triage.topics:
            topic.summary = redact_secrets(topic.summary, secrets)
            topic.reason = redact_secrets(topic.reason, secrets)
            topic.missing_information = [redact_secrets(item, secrets) for item in topic.missing_information]
        if checkpoint.skill_assessment is not None:
            checkpoint.skill_assessment.reason = redact_secrets(checkpoint.skill_assessment.reason, secrets)
        self.store.write_json(case_id, "triage.json", checkpoint.model_dump(mode="json"))

    def _resolve_skill(
        self, request: AnalyzeRequest, checkpoint: TriageCheckpoint, case_id: str | None = None,
    ) -> DiagnosisSkill | None:
        if not checkpoint.requested_skill:
            return None
        assessment = checkpoint.skill_assessment
        progress = self._logger(case_id).forward if case_id is not None else self._transient_progress
        if assessment is not None and not assessment.applicable:
            progress(ProgressEvent("WARNING", "skill.skipped", f"跳过 Skill {checkpoint.requested_skill}；使用通用只读代码分析"))
            return None
        skill = self.settings.diagnosis_skill(request.repo_alias, checkpoint.requested_skill)
        assert skill is not None
        if assessment is None:
            if self.classifier is None:
                raise RuntimeError("Discussion classifier is not configured")
            progress(ProgressEvent("INFO", "skill.assessing", f"正在判断 Skill {skill.name} 是否适合当前话题"))
            assessment = self.classifier.assess_skill(checkpoint.triage, skill.path.read_text(encoding="utf-8"))
            checkpoint.skill_assessment = assessment
            if case_id is not None:
                self._write_triage_checkpoint(case_id, checkpoint)
        if not assessment.applicable:
            progress(ProgressEvent("WARNING", "skill.skipped", f"跳过 Skill {skill.name}；使用通用只读代码分析"))
            return None
        return skill

    def _progress(self, name: str, message: str) -> None:
        self._transient_progress(ProgressEvent("INFO", name, message))

    def _transient_progress(self, event: ProgressEvent) -> None:
        if self.progress_sink is not None:
            secrets = (self.settings.video_tool.api_key.get_secret_value(),)
            self.progress_sink(ProgressEvent(event.level, event.name, sanitize_message(event.message, secrets), event.timestamp))

    @staticmethod
    def _failure_preview(batch: MaterializedBatch) -> dict[str, Any]:
        return {"kind": "attachment_failures", "failed": batch.failed}

    def _logger(self, case_id: str) -> CaseProgressLogger:
        logger = self._loggers.get(case_id)
        if logger is None:
            logger = CaseProgressLogger(
                self.store, case_id, self.progress_sink,
                secrets=(self.settings.video_tool.api_key.get_secret_value(),),
            )
            self._loggers[case_id] = logger
        return logger


def _attachments(bundle: MessageBundle) -> list[AttachmentRef]:
    return [attachment for message in bundle.messages for attachment in message.attachments
            if attachment.kind != "unsupported"]


def _choose_single(candidates: list[ConversationCandidate]) -> ConversationCandidate:
    if len(candidates) != 1:
        raise RuntimeError("Resume needs interactive candidate selection")
    return candidates[0]


def _confirmation_required(preview: dict[str, Any]) -> bool:
    raise RuntimeError("Resume needs interactive confirmation")
