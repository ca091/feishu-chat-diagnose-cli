from datetime import datetime
from pathlib import Path

import pytest

from feishu_chat_diagnose.case_store import CaseStore
from feishu_chat_diagnose.config import Settings
from feishu_chat_diagnose.models import (
    AttachmentRef, CodeEvidence, ConversationCandidate, Diagnosis, NormalizedMessage,
    RepositorySettings, Sender, TimeRange,
)
from feishu_chat_diagnose.orchestrator import AnalyzeRequest, DiagnosisOrchestrator


class FakeFeishu:
    def __init__(self, message: NormalizedMessage) -> None:
        self.message = message
        self.fetch_count = 0
        self.fail_fetch_once = False

    def search_groups(self, name):
        return [ConversationCandidate(chat_id="oc_1", name=name, conversation_type="group")]
    def search_people(self, name): return []
    def resolve_private_chat(self, name, participant_open_id): raise AssertionError
    def fetch_messages(self, chat_id, time_range):
        self.fetch_count += 1
        if self.fail_fetch_once:
            self.fail_fetch_once = False
            raise RuntimeError("temporary fetch failure")
        return [self.message]
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
    )
    request = AnalyzeRequest(group_name="Project", repo_alias="repo", time_range=TimeRange(start=now, end=now))
    return orchestrator, request, calls


def test_cancel_at_preview_never_starts_codex(tmp_path: Path) -> None:
    orchestrator, request, calls = runtime(tmp_path)
    state = orchestrator.analyze(request, lambda items: items[0], lambda preview: False)
    assert state.stage == "cancelled"
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


def test_fetch_failure_waits_for_explicit_resume(tmp_path: Path) -> None:
    events = []
    orchestrator, request, calls = runtime(tmp_path, events)
    orchestrator.feishu.fail_fetch_once = True
    failed = orchestrator.analyze(request, lambda items: items[0], lambda preview: True)
    assert failed.stage == "failed" and failed.failed_stage == "fetching_messages"
    assert orchestrator.feishu.fetch_count == 1
    assert calls == []
    completed = orchestrator.resume(failed.case_id, confirm=lambda preview: True)
    assert completed.stage == "completed"
    assert orchestrator.feishu.fetch_count == 2
    assert len(calls) == 1
    assert "resume.started" in [event.name for event in events]


def test_configured_key_is_redacted_from_case_artifacts_and_progress(tmp_path, monkeypatch) -> None:
    from pydantic import SecretStr

    from feishu_chat_diagnose.errors import ModelOutputError

    monkeypatch.setenv("DASHSCOPE_API_KEY", "environment-video-key")
    events = []
    orchestrator, request, _ = runtime(tmp_path, events)
    orchestrator.settings.video_tool.api_key = SecretStr("configured-video-key")

    class FailingCodex(FakeCodex):
        def set_progress(self, progress):
            self.progress = progress

        def diagnose(self, *args):
            from feishu_chat_diagnose.progress import ProgressEvent

            detail = "configured-video-key and environment-video-key"
            self.progress(ProgressEvent("ERROR", "video.failed", detail))
            raise ModelOutputError(f"first line\n{detail}", detail)

    orchestrator.codex_factory = lambda root: FailingCodex([])
    state = orchestrator.analyze(request, lambda items: items[0], lambda preview: True)

    assert state.stage == "failed"
    for path in orchestrator.store.case_dir(state.case_id).iterdir():
        text = path.read_text(encoding="utf-8")
        assert "configured-video-key" not in text
        assert "environment-video-key" not in text
    assert "[REDACTED]" in state.error
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

    assert cancelled.stage == "cancelled"
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

    failed = orchestrator.analyze(request, lambda items: items[0], interrupted_confirmation)
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
    orchestrator.feishu.last_missing_message_ids = ["missing_parent"]
    orchestrator.settings.defaults.max_messages = 0
    failed = orchestrator.analyze(request, lambda items: items[0], lambda preview: True)
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
    gateway.last_missing_message_ids = ["refetched_parent"] if legacy else []
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
    failed = orchestrator.analyze(request, lambda items: items[0], lambda preview: True)
    (orchestrator.store.case_dir(failed.case_id) / "fetch_metadata.json").unlink()
    orchestrator.settings.defaults.max_messages = 300
    gateway = FakeFeishu(orchestrator.feishu.message)
    gateway.fail_fetch_once = True
    gateway.last_missing_message_ids = ["missing_parent"]
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
    orchestrator.feishu.fail_fetch_once = True
    failed = orchestrator.analyze(request, lambda items: items[0], lambda preview: True)
    orchestrator.settings.defaults.max_messages = 0
    orchestrator.feishu.last_missing_message_ids = ["missing_parent"]

    failed_assembly = orchestrator.resume(failed.case_id, confirm=lambda preview: True)

    assert failed_assembly.stage == "failed" and failed_assembly.failed_stage == "assembling_context"
    assert orchestrator.store.read_json(failed.case_id, "fetch_metadata.json") == {
        "missing_message_ids": ["missing_parent"]
    }


@pytest.mark.parametrize("failed_file", ["raw_messages.json", "fetch_metadata.json"])
def test_interrupted_legacy_checkpoint_is_refetched_on_next_resume(tmp_path, mocker, failed_file) -> None:
    orchestrator, request, _ = runtime(tmp_path)
    orchestrator.settings.defaults.max_messages = 0
    failed = orchestrator.analyze(request, lambda items: items[0], lambda preview: True)
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
    orchestrator.feishu.last_missing_message_ids = ["missing_parent"]
    previews = []

    cancelled = orchestrator.resume(
        failed.case_id, confirm=lambda preview: previews.append(preview) or False,
    )

    assert cancelled.stage == "cancelled"
    assert orchestrator.feishu.fetch_count == 3
    assert previews[0]["missing"] == ["missing_parent"]
