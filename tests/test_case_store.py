from pathlib import Path

import pytest

from feishu_chat_diagnose.case_store import CaseStore
from feishu_chat_diagnose.errors import InvalidStageTransition


def test_failed_case_records_exact_resume_stage(tmp_path: Path) -> None:
    store = CaseStore(tmp_path)
    state = store.create({"repo_alias": "canvas-editor"})
    store.transition(state.case_id, "resolving_conversation")

    failed = store.fail(state.case_id, "resolving_conversation", "lookup failed")

    assert failed.stage == "failed"
    assert failed.failed_stage == "resolving_conversation"
    assert store.prepare_resume(state.case_id).stage == "resolving_conversation"


def test_completed_case_cannot_transition_backwards(tmp_path: Path) -> None:
    store = CaseStore(tmp_path)
    state = store.create({"repo_alias": "canvas-editor"})
    store.write_state(state.model_copy(update={"stage": "completed"}))

    with pytest.raises(InvalidStageTransition):
        store.transition(state.case_id, "fetching_messages")


@pytest.mark.parametrize("stage", ["awaiting_confirmation", "preparing_attachments", "diagnosing_repository"])
def test_cancel_is_allowed_at_confirmation_stages(tmp_path: Path, stage) -> None:
    store = CaseStore(tmp_path)
    state = store.create({"repo_alias": "canvas-editor"})

    with pytest.raises(InvalidStageTransition):
        store.transition(state.case_id, "cancelled")

    store.write_state(state.model_copy(update={"stage": stage}))
    assert store.transition(state.case_id, "cancelled").stage == "cancelled"
