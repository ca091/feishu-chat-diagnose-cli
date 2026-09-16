import json
from pathlib import Path

import pytest

from feishu_chat_diagnose.case_store import CaseStore
from feishu_chat_diagnose.errors import InvalidStageTransition


def test_case_store_writes_state_atomically(tmp_path: Path) -> None:
    store = CaseStore(tmp_path)
    state = store.create({"repo_alias": "canvas-editor"})

    updated = store.transition(state.case_id, "resolving_conversation")

    assert updated.stage == "resolving_conversation"
    assert not list(tmp_path.rglob("*.tmp"))


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


def test_cancel_is_allowed_only_from_preview(tmp_path: Path) -> None:
    store = CaseStore(tmp_path)
    state = store.create({"repo_alias": "canvas-editor"})

    with pytest.raises(InvalidStageTransition):
        store.transition(state.case_id, "cancelled")

    store.write_state(state.model_copy(update={"stage": "awaiting_confirmation"}))
    assert store.transition(state.case_id, "cancelled").stage == "cancelled"


def test_artifact_helpers_write_valid_json_and_text(tmp_path: Path) -> None:
    store = CaseStore(tmp_path)
    state = store.create({"repo_alias": "canvas-editor"})

    store.write_json(state.case_id, "messages.json", {"messages": []})
    store.write_text(state.case_id, "model-response.txt", "bad output")

    case_dir = tmp_path / state.case_id
    assert json.loads((case_dir / "messages.json").read_text()) == {"messages": []}
    assert (case_dir / "model-response.txt").read_text() == "bad output"
