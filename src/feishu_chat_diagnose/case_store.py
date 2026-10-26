from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from .errors import InvalidStageTransition
from .models import CaseStage, CaseState

_NEXT: dict[CaseStage, set[CaseStage]] = {
    "created": {"resolving_conversation"},
    "resolving_conversation": {"fetching_messages"},
    "fetching_messages": {"assembling_context"},
    "assembling_context": {"awaiting_confirmation"},
    "awaiting_confirmation": {"preparing_attachments", "cancelled"},
    "preparing_attachments": {"diagnosing_repository", "cancelled"},
    "diagnosing_repository": {"completed", "cancelled"},
    "completed": set(),
    "failed": set(),
    "cancelled": set(),
}


class CaseStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def create(self, request: dict[str, Any]) -> CaseState:
        now = datetime.now(UTC)
        case_id = f"{now:%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
        state = CaseState(case_id=case_id, created_at=now, updated_at=now)
        self.write_json(case_id, "request.json", request)
        self.write_state(state)
        return state

    def case_dir(self, case_id: str) -> Path:
        return self.root / case_id

    def read_state(self, case_id: str) -> CaseState:
        path = self.case_dir(case_id) / "state.json"
        return CaseState.model_validate_json(path.read_text(encoding="utf-8"))

    def read_json(self, case_id: str, name: str) -> Any:
        return json.loads((self.case_dir(case_id) / name).read_text(encoding="utf-8"))

    def write_state(self, state: CaseState) -> None:
        self.write_json(state.case_id, "state.json", state.model_dump(mode="json"))

    def transition(self, case_id: str, stage: CaseStage) -> CaseState:
        state = self.read_state(case_id)
        if stage not in _NEXT[state.stage]:
            raise InvalidStageTransition(f"Cannot transition {state.stage} to {stage}")
        now = datetime.now(UTC)
        updated = state.model_copy(
            update={
                "stage": stage,
                "last_successful_stage": state.stage,
                "updated_at": now,
                "error": "",
            }
        )
        self.write_state(updated)
        return updated

    def fail(self, case_id: str, failed_stage: CaseStage, error: str) -> CaseState:
        state = self.read_state(case_id)
        if state.stage in {"completed", "cancelled"}:
            raise InvalidStageTransition(f"Cannot fail terminal case in stage {state.stage}")
        updated = state.model_copy(
            update={
                "stage": "failed",
                "failed_stage": failed_stage,
                "updated_at": datetime.now(UTC),
                "error": error,
            }
        )
        self.write_state(updated)
        return updated

    def prepare_resume(self, case_id: str) -> CaseState:
        state = self.read_state(case_id)
        if state.stage != "failed" or state.failed_stage is None:
            raise InvalidStageTransition(f"Case {case_id} is not resumable")
        updated = state.model_copy(
            update={
                "stage": state.failed_stage,
                "failed_stage": None,
                "updated_at": datetime.now(UTC),
                "error": "",
            }
        )
        self.write_state(updated)
        return updated

    def list_states(self) -> list[CaseState]:
        if not self.root.exists():
            return []
        states = [self.read_state(path.name) for path in self.root.iterdir() if (path / "state.json").exists()]
        return sorted(states, key=lambda item: item.updated_at, reverse=True)

    def write_json(self, case_id: str, name: str, payload: Any) -> None:
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        self.write_text(case_id, name, text)

    def write_text(self, case_id: str, name: str, text: str) -> None:
        directory = self.case_dir(case_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)

    def append_text(self, case_id: str, name: str, text: str) -> None:
        directory = self.case_dir(case_id)
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / name).open("a", encoding="utf-8") as stream:
            stream.write(text)
