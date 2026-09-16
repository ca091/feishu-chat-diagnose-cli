from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from feishu_chat_diagnose.models import NormalizedMessage, Sender, TimeRange


class FakeLarkRunner:
    def __init__(self) -> None:
        self.responses: list[dict[str, Any]] = []
        self.calls: list[list[str]] = []
        self.cwds: list[Path | None] = []

    def run(self, args: list[str], cwd: Path | None = None) -> dict[str, Any]:
        self.calls.append(args)
        self.cwds.append(cwd)
        if not self.responses:
            raise AssertionError(f"No fake response queued for {args}")
        return self.responses.pop(0)


@pytest.fixture
def fake_runner() -> FakeLarkRunner:
    return FakeLarkRunner()


@pytest.fixture
def time_range() -> TimeRange:
    return TimeRange(
        start=datetime.fromisoformat("2026-07-15T14:00:00+08:00"),
        end=datetime.fromisoformat("2026-07-15T16:00:00+08:00"),
    )


@pytest.fixture
def message_factory():
    def make(
        message_id: str,
        text: str,
        *,
        reply_to_message_id: str = "",
        thread_root_id: str = "",
        minute: int = 0,
    ) -> NormalizedMessage:
        return NormalizedMessage(
            message_id=message_id,
            sender=Sender(open_id="ou_sender", name="Sender"),
            created_at=datetime.fromisoformat(f"2026-07-15T14:{minute:02d}:00+08:00"),
            text=text,
            reply_to_message_id=reply_to_message_id,
            thread_root_id=thread_root_id,
        )

    return make

