from datetime import datetime
from pathlib import Path

import pytest

from feishu_chat_diagnose.case_store import CaseStore
from feishu_chat_diagnose.config import Settings
from feishu_chat_diagnose.errors import ConfigurationError, ContextTooLarge
from feishu_chat_diagnose.models import (
    AttachmentRef, CodeEvidence, ConversationCandidate, Diagnosis, NormalizedMessage,
    RepositorySettings, Sender, TimeRange, DiscussionTriage, DiscussionTopic, SkillAssessment, FetchResult,
)
from feishu_chat_diagnose.orchestrator import AnalyzeRequest, DiagnosisOrchestrator


class FakeFeishu:
    def __init__(self, message: NormalizedMessage) -> None:
        self.message = message
        self.fetch_count = 0
        self.fail_fetch_once = False
        self.missing_message_ids = []

    def search_groups(self, name):
        return [ConversationCandidate(chat_id="oc_1", name=name, conversation_type="group")]
    def search_people(self, name): return []
    def resolve_private_chat(self, name, participant_open_id): raise AssertionError
    def fetch_messages(self, chat_id, time_range):
        self.fetch_count += 1
        if self.fail_fetch_once:
            self.fail_fetch_once = False
            raise RuntimeError("temporary fetch failure")
        return FetchResult([self.message], list(self.missing_message_ids))
    def download_attachment(self, attachment, root, output_name): raise AssertionError


class FakeCodex:
    def __init__(self, calls): self.calls = calls
    def diagnose(self, repository, bundle, attachments, skill=None):
        self.calls.append((repository, skill))
        return Diagnosis(problem_summary="Issue", facts=[], root_cause="Cause", confidence=80,
            code_evidence=[CodeEvidence(path="src/a.ts", explanation="Evidence")],
            alternative_hypotheses=[], missing_information=[], recommended_fix_direction=[],
            recommended_verification=[], skills_used=[skill.name] if skill else [])
    def close(self): pass


class FakeClassifier:
    def classify(self, repo_alias, description, messages):
        return DiscussionTriage(topics=[DiscussionTopic(
            kind="bug", summary="导出问题", project_related="yes", needs_code=True,
            reason="需要检查导出实现", message_ids=[item.message_id for item in messages],
            missing_information=[],
        )])

    def assess_skill(self, triage, skill_text):
        return SkillAssessment(applicable=True, reason="适合当前问题")


def runtime(tmp_path: Path, events=None):
    repo = tmp_path / "repo"; repo.mkdir()
    now = datetime.fromisoformat("2026-07-15T14:00:00+08:00")
    message = NormalizedMessage(message_id="om_1", sender=Sender(open_id="ou_1", name="User"),
                                created_at=now, text="Export issue")
    settings = Settings(repositories={"repo": RepositorySettings(path=repo)})
    calls = []
    orchestrator = DiagnosisOrchestrator(
        settings, CaseStore(tmp_path / "cases"), FakeFeishu(message),
        lambda root: FakeCodex(calls), progress_sink=(events.append if events is not None else None),
        classifier=FakeClassifier(),
    )
    request = AnalyzeRequest(group_name="Project", repo_alias="repo", time_range=TimeRange(start=now, end=now))
    return orchestrator, request, calls


def test_cancel_at_preview_never_starts_codex(tmp_path: Path) -> None:
    orchestrator, request, calls = runtime(tmp_path)
    state = orchestrator.analyze(request, lambda items: items[0], lambda preview: False)
    assert state is None
    assert not orchestrator.store.root.exists()
    assert not orchestrator._loggers
    assert calls == []


def test_success_persists_diagnosis_and_report(tmp_path: Path) -> None:
    events = []
    orchestrator, request, calls = runtime(tmp_path, events)
    state = orchestrator.analyze(request, lambda items: items[0], lambda preview: True)
    assert state.stage == "completed"
    assert len(calls) == 1
    case_dir = orchestrator.store.case_dir(state.case_id)
    assert (case_dir / "messages.json").exists()
    assert (case_dir / "diagnosis.json").exists()
    assert (case_dir / "report.md").exists()
    assert (case_dir / "execution.log").exists()
    names = [event.name for event in events]
    assert names.index("confirmation.accepted") < names.index("attachments.started")
    assert names.index("attachments.completed") < names.index("codex.started")
    assert names[-1] == "case.completed"


def legacy_fetch_case(orchestrator, request):
    state = orchestrator.store.create(request.model_dump(mode="json"))
    orchestrator.store.transition(state.case_id, "resolving_conversation")
    conversation = orchestrator._resolve(request, lambda items: items[0])
    orchestrator.store.write_json(state.case_id, "conversation.json", conversation.model_dump(mode="json"))
    orchestrator.store.transition(state.case_id, "fetching_messages")
    return orchestrator.store.fail(state.case_id, "fetching_messages", "temporary fetch failure")


def legacy_assembly_case(orchestrator, request):
    failed = legacy_fetch_case(orchestrator, request)
    return orchestrator.resume(failed.case_id, confirm=lambda preview: True)


def test_legacy_fetch_failure_waits_for_explicit_resume(tmp_path: Path) -> None:
    events = []
    orchestrator, request, calls = runtime(tmp_path, events)
    failed = legacy_fetch_case(orchestrator, request)
    assert failed.stage == "failed" and failed.failed_stage == "fetching_messages"
    assert orchestrator.feishu.fetch_count == 0
    assert calls == []
    completed = orchestrator.resume(failed.case_id, confirm=lambda preview: True)
    assert completed.stage == "completed"
    assert orchestrator.feishu.fetch_count == 1
    assert len(calls) == 1
    assert "resume.started" in [event.name for event in events]


def test_configured_key_is_redacted_from_case_artifacts_and_progress(tmp_path, monkeypatch, mocker) -> None:
    from pydantic import SecretStr

    from feishu_chat_diagnose.errors import ModelOutputError

    monkeypatch.setenv("DASHSCOPE_API_KEY", "environment-video-key")
    events = []
    orchestrator, request, _ = runtime(tmp_path, events)
    orchestrator.settings.video_tool.api_key = SecretStr("configured-video-key")
    triage = orchestrator.classifier.classify("repo", "", [orchestrator.feishu.message])
    triage.topics[0].summary = "问题 configured-video-key"
    triage.topics[0].reason = "原因 environment-video-key"
    mocker.patch.object(orchestrator.classifier, "classify", return_value=triage)

    class FailingCodex(FakeCodex):
        def set_progress(self, progress):
            self.progress = progress

        def diagnose(self, *args):
            from feishu_chat_diagnose.progress import ProgressEvent

            detail = "configured-video-key and environment-video-key"
            self.progress(ProgressEvent("ERROR", "video.failed", detail))
            raise ModelOutputError(f"first line\n{detail}", detail)

    orchestrator.codex_factory = lambda root: FailingCodex([])
    previews = []
    state = orchestrator.analyze(request, lambda items: items[0], lambda preview: previews.append(preview) or True)

    assert state.stage == "failed"
    for path in orchestrator.store.case_dir(state.case_id).iterdir():
        text = path.read_text(encoding="utf-8")
        assert "configured-video-key" not in text
        assert "environment-video-key" not in text
    assert "[REDACTED]" in state.error
    assert previews[0]["topics"] == ["问题 [REDACTED]"]
    assert "\n" in (orchestrator.store.case_dir(state.case_id) / "model-response.txt").read_text()
    assert next(event for event in events if event.name == "video.failed").message == (
        "[REDACTED] and [REDACTED]"
    )


def test_default_skill_is_passed_to_codex(tmp_path: Path) -> None:
    orchestrator, request, calls = runtime(tmp_path)
    repo = orchestrator.settings.repositories["repo"].path
    skill_path = repo / ".agents/skills/project-bugfix/SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("---\nname: project-bugfix\ndescription: Test\n---\n", encoding="utf-8")
    orchestrator.settings.repositories["repo"].diagnosis_skill = "project-bugfix"

    state = orchestrator.analyze(request, lambda items: items[0], lambda preview: True)

    assert state.stage == "completed"
    assert calls[0][1].name == "project-bugfix"


class PartialAttachmentFeishu(FakeFeishu):
    def __init__(self, message):
        super().__init__(message)
        self.downloaded = []

    def download_attachment(self, attachment, root, output_name):
        if attachment.file_key == "missing":
            raise RuntimeError("attachment unavailable")
        path = root / output_name
        path.write_text("Diagnostic log", encoding="utf-8")
        self.downloaded.append(path)
        return path


@pytest.fixture
def partial_runtime(tmp_path):
    events = []
    orchestrator, request, calls = runtime(tmp_path, events)
    message = orchestrator.feishu.message
    message.attachments = [
        AttachmentRef(source_message_id=message.message_id, file_key=key, resource_type="file",
                      media_type="text/plain", name=f"{key}.txt", kind="text")
        for key in ("available", "missing")
    ]
    orchestrator.feishu = PartialAttachmentFeishu(message)
    return orchestrator, request, calls, events


@pytest.mark.parametrize("answer,expected_stage", [
    (True, "completed"), (False, "cancelled"), (None, "failed"),
])
def test_resume_reconfirms_partial_attachments_and_cleans_up(partial_runtime, mocker, answer, expected_stage) -> None:
    orchestrator, request, calls, events = partial_runtime
    first_gateway = orchestrator.feishu

    def diagnose(repository, bundle, attachments, skill=None):
        assert len(attachments) == 1
        assert attachments[0].local_path.is_file()
        if not calls:
            calls.append((repository, skill))
            raise RuntimeError("temporary Codex failure")
        return FakeCodex(calls).diagnose(repository, bundle, attachments, skill)

    client = mocker.Mock(diagnose=diagnose)
    factory = mocker.Mock(return_value=client)
    orchestrator.codex_factory = factory
    failed = orchestrator.analyze(request, lambda items: items[0], lambda preview: True)

    assert failed.stage == "failed" and failed.failed_stage == "diagnosing_repository"
    assert len(calls) == 1
    assert first_gateway.downloaded
    assert all(not path.parent.exists() for path in first_gateway.downloaded)
    assert client.close.call_count == 1

    gateway = PartialAttachmentFeishu(first_gateway.message)
    resumed_runtime = DiagnosisOrchestrator(
        orchestrator.settings, orchestrator.store, gateway, factory, progress_sink=events.append,
    )
    previews = []

    def confirm(preview):
        previews.append(preview)
        return answer

    state = resumed_runtime.resume(failed.case_id, confirm=confirm if answer is not None else None)

    assert state.stage == expected_stage
    assert gateway.fetch_count == 0
    assert gateway.downloaded
    assert all(not path.parent.exists() for path in gateway.downloaded)
    assert len(calls) == (2 if answer is True else 1)
    assert client.close.call_count == len(calls)
    if answer is not None:
        assert len(previews) == 1
        assert previews[0]["kind"] == "attachment_failures"
        assert previews[0]["failed"][0].attachment.file_key == "missing"
    else:
        assert state.failed_stage == "diagnosing_repository"
        assert "interactive confirmation" in state.error
    assert ("case.completed" in [event.name for event in events]) == (answer is True)


def test_initial_partial_attachment_decline_cleans_up_without_starting_codex(partial_runtime, mocker) -> None:
    orchestrator, request, calls, events = partial_runtime
    factory = mocker.Mock()
    orchestrator.codex_factory = factory

    cancelled = orchestrator.analyze(
        request, lambda items: items[0], lambda preview: preview["kind"] == "messages",
    )

    assert cancelled is None
    assert not orchestrator.store.root.exists()
    assert not orchestrator._loggers
    factory.assert_not_called()
    assert calls == []
    assert orchestrator.feishu.downloaded
    assert all(not path.parent.exists() for path in orchestrator.feishu.downloaded)
    assert "case.completed" not in [event.name for event in events]


@pytest.mark.parametrize("answer,expected_stage", [
    (True, "completed"), (False, "cancelled"), (None, "failed"),
])
def test_resume_from_preparing_attachments_uses_shared_confirmation_and_cleanup(
    partial_runtime, answer, expected_stage,
) -> None:
    orchestrator, request, calls, events = partial_runtime

    def interrupted_confirmation(preview):
        if preview["kind"] == "attachment_failures":
            raise RuntimeError("confirmation interrupted")
        return True

    failed = orchestrator.resume(
        legacy_fetch_case(orchestrator, request).case_id, confirm=interrupted_confirmation,
    )
    assert failed.stage == "failed" and failed.failed_stage == "preparing_attachments"
    assert calls == []
    assert all(not path.parent.exists() for path in orchestrator.feishu.downloaded)
    previews = []

    def confirm(preview):
        previews.append(preview)
        return answer

    result = orchestrator.resume(failed.case_id, confirm=confirm if answer is not None else None)

    assert result.stage == expected_stage
    assert orchestrator.feishu.fetch_count == 1
    assert len(orchestrator.feishu.downloaded) == 2
    assert all(not path.parent.exists() for path in orchestrator.feishu.downloaded)
    assert len(calls) == (1 if answer is True else 0)
    if answer is not None:
        assert len(previews) == 1 and previews[0]["kind"] == "attachment_failures"
    else:
        assert result.failed_stage == "preparing_attachments"
        assert "interactive confirmation" in result.error
    assert ("case.completed" in [event.name for event in events]) == (answer is True)


@pytest.mark.parametrize("legacy", [False, True])
def test_resume_assembly_preserves_missing_context_across_processes(tmp_path, legacy) -> None:
    orchestrator, request, calls = runtime(tmp_path)
    orchestrator.feishu.missing_message_ids = ["missing_parent"]
    orchestrator.feishu.message.reply_to_message_id = "missing_parent"
    orchestrator.settings.defaults.max_messages = 0
    failed = legacy_assembly_case(orchestrator, request)
    assert failed.stage == "failed" and failed.failed_stage == "assembling_context"
    assert calls == []
    store = orchestrator.store
    assert store.read_json(failed.case_id, "fetch_metadata.json") == {
        "missing_message_ids": ["missing_parent"]
    }
    assert isinstance(store.read_json(failed.case_id, "raw_messages.json"), list)
    if legacy:
        (store.case_dir(failed.case_id) / "fetch_metadata.json").unlink()

    gateway = FakeFeishu(orchestrator.feishu.message)
    gateway.missing_message_ids = ["refetched_parent"] if legacy else []
    settings = orchestrator.settings.model_copy(deep=True)
    settings.defaults.max_messages = 300
    events, previews = [], []
    resumed_runtime = DiagnosisOrchestrator(
        settings, store, gateway, lambda root: FakeCodex(calls), progress_sink=events.append,
    )
    state = resumed_runtime.resume(
        failed.case_id, confirm=lambda preview: previews.append(preview) or False,
    )

    assert state.stage == "cancelled"
    assert gateway.fetch_count == int(legacy)
    expected = ["refetched_parent"] if legacy else ["missing_parent"]
    assert previews[0]["missing"] == expected
    assert store.read_json(failed.case_id, "messages.json")["missing_message_ids"] == expected
    assert store.read_json(failed.case_id, "fetch_metadata.json")["missing_message_ids"] == expected
    assert ("resume.legacy_checkpoint" in [event.name for event in events]) == legacy
    assert calls == []


def test_legacy_assembly_refetch_failure_can_be_resumed_again(tmp_path) -> None:
    orchestrator, request, calls = runtime(tmp_path)
    orchestrator.settings.defaults.max_messages = 0
    failed = legacy_assembly_case(orchestrator, request)
    (orchestrator.store.case_dir(failed.case_id) / "fetch_metadata.json").unlink()
    orchestrator.settings.defaults.max_messages = 300
    gateway = FakeFeishu(orchestrator.feishu.message)
    gateway.fail_fetch_once = True
    gateway.missing_message_ids = ["missing_parent"]
    resumed_runtime = DiagnosisOrchestrator(
        orchestrator.settings, orchestrator.store, gateway, lambda root: FakeCodex(calls),
    )

    failed_again = resumed_runtime.resume(failed.case_id, confirm=lambda preview: True)
    assert failed_again.stage == "failed" and failed_again.failed_stage == "assembling_context"
    assert not (orchestrator.store.case_dir(failed.case_id) / "fetch_metadata.json").exists()

    completed = resumed_runtime.resume(failed.case_id, confirm=lambda preview: True)
    assert completed.stage == "completed"
    assert gateway.fetch_count == 2
    assert orchestrator.store.read_json(failed.case_id, "messages.json")["missing_message_ids"] == ["missing_parent"]


def test_resumed_fetch_checkpoints_missing_context_before_assembly_failure(tmp_path) -> None:
    orchestrator, request, _ = runtime(tmp_path)
    failed = legacy_fetch_case(orchestrator, request)
    orchestrator.settings.defaults.max_messages = 0
    orchestrator.feishu.missing_message_ids = ["missing_parent"]

    failed_assembly = orchestrator.resume(failed.case_id, confirm=lambda preview: True)

    assert failed_assembly.stage == "failed" and failed_assembly.failed_stage == "assembling_context"
    assert orchestrator.store.read_json(failed.case_id, "fetch_metadata.json") == {
        "missing_message_ids": ["missing_parent"]
    }


@pytest.mark.parametrize("failed_file", ["raw_messages.json", "fetch_metadata.json"])
def test_interrupted_legacy_checkpoint_is_refetched_on_next_resume(tmp_path, mocker, failed_file) -> None:
    orchestrator, request, _ = runtime(tmp_path)
    orchestrator.settings.defaults.max_messages = 0
    failed = legacy_assembly_case(orchestrator, request)
    store = orchestrator.store
    metadata = store.case_dir(failed.case_id) / "fetch_metadata.json"
    metadata.unlink()
    orchestrator.settings.defaults.max_messages = 300
    write_json = store.write_json

    def interrupted_write(case_id, name, payload):
        if name == failed_file:
            raise OSError("checkpoint write interrupted")
        write_json(case_id, name, payload)

    writer = mocker.patch.object(store, "write_json", side_effect=interrupted_write)
    failed_again = orchestrator.resume(failed.case_id, confirm=lambda preview: True)
    assert failed_again.stage == "failed" and failed_again.failed_stage == "assembling_context"
    assert not metadata.exists()
    writer.side_effect = write_json
    orchestrator.feishu.missing_message_ids = ["missing_parent"]
    previews = []

    cancelled = orchestrator.resume(
        failed.case_id, confirm=lambda preview: previews.append(preview) or False,
    )

    assert cancelled.stage == "cancelled"
    assert orchestrator.feishu.fetch_count == 3
    assert previews[0]["missing"] == ["missing_parent"]


@pytest.mark.parametrize("kind,related", [("other", "no"), ("requirement", "yes"), ("unknown", "unknown")])
def test_summary_only_never_creates_case_or_loads_repository_and_skill(tmp_path, mocker, kind, related):
    orchestrator, request, calls = runtime(tmp_path)
    orchestrator.settings.repositories["repo"].path = tmp_path / "unavailable"
    orchestrator.settings.repositories["repo"].diagnosis_skill = "missing"
    result = DiscussionTriage(topics=[DiscussionTopic(
        kind=kind, project_related=related, needs_code=False, summary="讨论总结",
        reason="无需检查实现，或信息不足", message_ids=["om_1"], missing_information=["需要更多背景"],
    )])
    orchestrator.classifier = mocker.Mock(classify=mocker.Mock(return_value=result))
    create = mocker.spy(orchestrator.store, "create")
    skill = mocker.patch.object(type(orchestrator.settings), "diagnosis_skill", side_effect=AssertionError("must not load Skill"))
    confirm = mocker.Mock()

    assert orchestrator.analyze(request, lambda items: items[0], confirm) == result
    create.assert_not_called()
    skill.assert_not_called()
    confirm.assert_not_called()
    orchestrator.classifier.assess_skill.assert_not_called()
    assert not orchestrator.store.root.exists() and calls == []


@pytest.mark.parametrize("budget", ["messages", "text_bytes"])
def test_triage_budget_is_independent_and_excess_stops_before_classifier(tmp_path, mocker, budget):
    orchestrator, request, calls = runtime(tmp_path)
    orchestrator.settings.defaults.max_messages = 0
    orchestrator.settings.defaults.max_triage_messages = 1
    orchestrator.feishu.message.text = "中"
    orchestrator.settings.repositories["repo"].description = "项目"
    # UTF-8 budget includes the project alias/description and chat body.
    orchestrator.settings.defaults.max_triage_text_bytes = len("repo项目中".encode("utf-8"))
    triage = DiscussionTriage(topics=[DiscussionTopic(
        kind="other", summary="讨论总结", project_related="no", needs_code=False,
        reason="无需代码分析", message_ids=["om_1"], missing_information=[],
    )])
    classify = mocker.patch.object(orchestrator.classifier, "classify", return_value=triage)
    confirm = mocker.Mock()
    assert orchestrator.analyze(request, lambda items: items[0], confirm) == triage
    if budget == "messages":
        extra = orchestrator.feishu.message.model_copy(update={"message_id": "extra", "text": ""})
        mocker.patch.object(orchestrator.feishu, "fetch_messages", return_value=FetchResult([
            orchestrator.feishu.message, extra,
        ]))
    else:
        orchestrator.settings.defaults.max_triage_text_bytes -= 1
    with pytest.raises(ContextTooLarge, match="请缩短 --since"):
        orchestrator.analyze(request, lambda items: items[0], confirm)
    classify.assert_called_once()
    confirm.assert_not_called()
    assert not orchestrator.store.root.exists() and not orchestrator._loggers and calls == []


@pytest.mark.parametrize("failure", ["fetch", "classification"])
def test_failures_before_gate_are_transient_and_not_retried(tmp_path, mocker, failure):
    orchestrator, request, calls = runtime(tmp_path)
    target = (orchestrator.feishu, "fetch_messages") if failure == "fetch" else (orchestrator.classifier, "classify")
    operation = mocker.patch.object(*target, side_effect=RuntimeError("transient failure"))
    with pytest.raises(RuntimeError, match="transient failure"):
        orchestrator.analyze(request, lambda items: items[0], lambda preview: True)
    operation.assert_called_once()
    assert not orchestrator.store.root.exists() and calls == []


@pytest.mark.parametrize("failure,error_type,pattern", [
    ("empty_chat", RuntimeError, "No messages found"),
    ("assembly_limit", ContextTooLarge, "exceeds limit"),
    ("repository", ConfigurationError, "missing"),
    ("confirmation", RuntimeError, "confirmation interrupted"),
])
def test_pre_diagnosis_failures_never_create_cases(tmp_path, mocker, failure, error_type, pattern):
    orchestrator, request, calls = runtime(tmp_path)
    if failure == "empty_chat":
        mocker.patch.object(orchestrator.feishu, "fetch_messages", return_value=FetchResult([]))
    elif failure == "assembly_limit":
        orchestrator.settings.defaults.max_messages = 0
    elif failure == "repository":
        orchestrator.settings.repositories["repo"].path = tmp_path / "missing"

    def confirm(preview):
        raise RuntimeError("confirmation interrupted")

    with pytest.raises(error_type, match=pattern):
        orchestrator.analyze(request, lambda items: items[0], confirm)
    assert not orchestrator.store.root.exists() and calls == []
    assert not orchestrator._loggers


def test_mixed_topics_persist_and_diagnose_only_code_context(tmp_path, mocker, message_factory):
    orchestrator, request, calls = runtime(tmp_path)
    messages = [message_factory("parent", "现有导出实现"),
                message_factory("code", "评估新增格式", thread_root_id="parent", minute=1),
                message_factory("lunch", "午餐安排", thread_root_id="parent", minute=2)]
    messages[-1].attachments = [AttachmentRef(
        source_message_id="lunch", file_key="never-download", resource_type="file",
        name="lunch.mp4", media_type="video/mp4", kind="video",
    )]
    mocker.patch.object(orchestrator.feishu, "fetch_messages", return_value=FetchResult(messages))
    download = mocker.spy(orchestrator.feishu, "download_attachment")
    triage = DiscussionTriage(topics=[
        DiscussionTopic(kind="requirement", summary="新增格式评估", project_related="yes", needs_code=True,
                        reason="需要检查导出扩展点", message_ids=["code"], missing_information=[]),
        DiscussionTopic(kind="other", summary="实现背景", project_related="yes", needs_code=False,
                        reason="作为引用背景", message_ids=["parent"], missing_information=[]),
        DiscussionTopic(kind="other", summary="午餐安排", project_related="no", needs_code=False,
                        reason="与项目无关", message_ids=["lunch"], missing_information=[]),
    ])
    mocker.patch.object(orchestrator.classifier, "classify", return_value=triage)
    show = mocker.Mock()
    orchestrator.triage_sink = show
    captured = []

    class CaptureCodex(FakeCodex):
        def diagnose(self, repository, bundle, attachments, skill=None):
            captured.append(bundle)
            assert not attachments
            if len(captured) == 1:
                raise RuntimeError("interrupted diagnosis")
            return super().diagnose(repository, bundle, attachments, skill)

    orchestrator.codex_factory = lambda root: CaptureCodex(calls)
    failed = orchestrator.analyze(request, lambda items: items[0], lambda preview: True)
    assert failed.failed_stage == "diagnosing_repository"
    state = orchestrator.resume(failed.case_id, confirm=lambda preview: True)
    assert state.stage == "completed"
    show.assert_called_once_with(triage)
    assert [item.message_id for item in captured[0].messages] == ["parent", "code"]
    assert [item.message_id for item in captured[1].messages] == ["parent", "code"]
    assert captured[0].analysis_message_ids == captured[1].analysis_message_ids == ["code"]
    assert captured[0].analysis_kind == "code"
    download.assert_not_called()
    case_dir = orchestrator.store.case_dir(state.case_id)
    assert "lunch" not in (case_dir / "raw_messages.json").read_text()
    assert "午餐安排" not in (case_dir / "triage.json").read_text()
    assert "午餐安排" not in (case_dir / "messages.json").read_text()
    report = (case_dir / "report.md").read_text()
    assert "## 代码分析结论" in report and "## 实现方向" in report
    assert "## 最可能根因" not in report


def test_skipped_skill_remains_skipped_on_resume_even_if_file_is_gone(tmp_path, mocker):
    events = []
    orchestrator, request, calls = runtime(tmp_path, events)
    chat_text = "客户合同细节只应保存在聊天证据中"
    orchestrator.feishu.message.text = chat_text
    repo = orchestrator.settings.repositories["repo"]
    repo.diagnosis_skill = "bugfix"
    path = repo.path / ".agents/skills/bugfix/SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("Bug diagnosis only")
    assess = mocker.patch.object(orchestrator.classifier, "assess_skill", return_value=SkillAssessment(
        applicable=False, reason=f"聊天讨论的是“{chat_text}”，不适合缺陷排查 Skill",
    ))

    class FailingCodex(FakeCodex):
        def diagnose(self, repository, bundle, attachments, skill=None):
            assert skill is None
            raise RuntimeError("interrupted diagnosis")

    orchestrator.codex_factory = lambda root: FailingCodex(calls)
    previews = []
    failed = orchestrator.analyze(request, lambda items: items[0], lambda preview: previews.append(preview) or True)
    assert failed.failed_stage == "diagnosing_repository"
    assert not previews[0]["skill_assessment"].applicable
    assert chat_text in previews[0]["skill_assessment"].reason
    assert "跳过 Skill bugfix" in (orchestrator.store.case_dir(failed.case_id) / "execution.log").read_text()
    assess.assert_called_once()
    path.unlink()
    restarted = DiagnosisOrchestrator(
        orchestrator.settings, orchestrator.store, orchestrator.feishu, lambda root: FakeCodex(calls),
        progress_sink=events.append,
    )
    assert restarted.resume(failed.case_id, confirm=lambda preview: True).stage == "completed"
    assert calls[-1][1] is None
    case_dir = orchestrator.store.case_dir(failed.case_id)
    assert chat_text in (case_dir / "triage.json").read_text()
    assert chat_text not in (case_dir / "execution.log").read_text()
    assert all(chat_text not in event.message for event in events)


def test_skill_assessment_failure_creates_no_case_and_requires_new_analyze(tmp_path, mocker):
    orchestrator, request, calls = runtime(tmp_path)
    repo = orchestrator.settings.repositories["repo"]
    repo.diagnosis_skill = "bugfix"
    path = repo.path / ".agents/skills/bugfix/SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("Bug diagnosis only")
    classify = mocker.spy(orchestrator.classifier, "classify")
    assess = mocker.patch.object(orchestrator.classifier, "assess_skill", side_effect=[
        RuntimeError("assessment unavailable"), SkillAssessment(applicable=True, reason="适合当前问题"),
    ])
    confirm = mocker.Mock(return_value=True)
    with pytest.raises(RuntimeError, match="assessment unavailable"):
        orchestrator.analyze(request, lambda items: items[0], confirm)
    assert not orchestrator.store.root.exists() and calls == []
    assert not orchestrator._loggers
    confirm.assert_not_called()
    assert assess.call_count == 1

    assert orchestrator.analyze(request, lambda items: items[0], confirm).stage == "completed"
    assert classify.call_count == 2 and assess.call_count == 2
    assert calls[-1][1].name == "bugfix"


def test_missing_new_triage_checkpoint_cannot_use_legacy_resume(tmp_path, mocker):
    orchestrator, request, calls = runtime(tmp_path)
    client = mocker.Mock()
    client.diagnose.side_effect = RuntimeError("diagnosis interrupted")
    orchestrator.codex_factory = lambda root: client
    failed = orchestrator.analyze(request, lambda items: items[0], lambda preview: True)
    (orchestrator.store.case_dir(failed.case_id) / "triage.json").unlink()

    resumed = orchestrator.resume(failed.case_id, confirm=lambda preview: True)
    assert resumed.stage == "failed" and "不能绕过讨论判断" in resumed.error
    assert calls == [] and orchestrator.feishu.fetch_count == 1
    client.diagnose.assert_called_once()
