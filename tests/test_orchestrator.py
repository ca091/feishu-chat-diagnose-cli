from datetime import datetime
from pathlib import Path

from feishu_chat_diagnose.case_store import CaseStore
from feishu_chat_diagnose.config import Settings
from feishu_chat_diagnose.models import (
    CodeEvidence, ConversationCandidate, Diagnosis, NormalizedMessage, RepositorySettings, Sender, TimeRange,
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
