from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from .case_store import CaseStore

ProgressLevel = Literal["INFO", "WARNING", "ERROR"]
ProgressSink = Callable[["ProgressEvent"], None]


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    level: ProgressLevel
    name: str
    message: str
    timestamp: datetime = field(
        default_factory=lambda: datetime.now(ZoneInfo("Asia/Shanghai"))
    )


class CaseProgressLogger:
    def __init__(
        self,
        store: CaseStore,
        case_id: str,
        sink: ProgressSink | None = None,
    ) -> None:
        self.store = store
        self.case_id = case_id
        self.sink = sink or (lambda event: None)
        self._file_enabled = True
        self._file_warning_sent = False

    def info(self, name: str, message: str) -> None:
        self.emit("INFO", name, message)

    def warning(self, name: str, message: str) -> None:
        self.emit("WARNING", name, message)

    def error(self, name: str, message: str) -> None:
        self.emit("ERROR", name, message)

    def forward(self, event: ProgressEvent) -> None:
        self.emit(event.level, event.name, event.message)

    def emit(self, level: ProgressLevel, name: str, message: str) -> None:
        event = ProgressEvent(level, name, sanitize_message(message))
        self.sink(event)
        if not self._file_enabled:
            return
        line = (
            f"{event.timestamp.isoformat(timespec='seconds')} {event.level} "
            f"{event.name} {event.message}\n"
        )
        try:
            self.store.append_text(self.case_id, "execution.log", line)
        except OSError:
            self._file_enabled = False
            if not self._file_warning_sent:
                self._file_warning_sent = True
                self.sink(
                    ProgressEvent(
                        "WARNING",
                        "logging.disabled",
                        "执行日志写入失败，本次运行仅保留终端输出",
                    )
                )


def sanitize_message(message: str) -> str:
    value = re.sub(
        r"(?i)\bBearer\s+[A-Za-z0-9._~+\-/]+=*",
        "Bearer [REDACTED]",
        str(message),
    )
    for name, secret in os.environ.items():
        if secret and any(
            token in name.upper()
            for token in ("TOKEN", "SECRET", "API_KEY", "PASSWORD")
        ):
            value = value.replace(secret, "[REDACTED]")
    return " ".join(value.splitlines()).strip()
