from __future__ import annotations

import json
import os
import select
import subprocess
import time
from collections import deque
from collections.abc import Callable, Iterable
from typing import Any, Protocol

from .progress import ProgressEvent, ProgressLevel, ProgressSink


class JsonRpcTransport(Protocol):
    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]: ...
    def notify(self, method: str, params: dict[str, Any]) -> None: ...
    def events(self) -> Iterable[dict[str, Any]]: ...
    def close(self) -> None: ...


class StdioJsonRpcTransport:
    def __init__(
        self,
        process: subprocess.Popen[str],
        timeout_seconds: float = 600,
        progress: ProgressSink | None = None,
        heartbeat_seconds: float = 10,
        clock: Callable[[], float] = time.monotonic,
        *, total_timeout_seconds: float = 1800,
    ) -> None:
        self.process = process
        self.timeout_seconds = timeout_seconds
        self.progress = progress or (lambda event: None)
        self.heartbeat_seconds = heartbeat_seconds
        self.clock = clock
        self.total_timeout_seconds = total_timeout_seconds
        self._total_deadline: float | None = None
        self._next_id = 1
        self._pending: deque[dict[str, Any]] = deque()
        self._read_buffer = bytearray()

    def set_progress(self, progress: ProgressSink | None) -> None:
        self.progress = progress or (lambda event: None)

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        started = self.clock()
        self._check_total_deadline(started)
        deadline = started + self.timeout_seconds
        request_id = self._next_id
        self._next_id += 1
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        while True:
            message = self._read(deadline)
            if message.get("id") != request_id:
                self._pending.append(message)
                continue
            if "error" in message:
                raise RuntimeError(f"Codex app-server {method} failed: {message['error']}")
            return message.get("result", {})

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def events(self) -> Iterable[dict[str, Any]]:
        while self._pending:
            self._check_total_deadline(self.clock())
            yield self._pending.popleft()
        while True:
            yield self._read()

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)

    def _write(self, message: dict[str, Any]) -> None:
        if self.process.stdin is None:
            raise RuntimeError("Codex app-server stdin is unavailable")
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def _check_total_deadline(self, now: float) -> float:
        if self._total_deadline is None:
            self._total_deadline = now + self.total_timeout_seconds
        if now >= self._total_deadline:
            self._emit("ERROR", "codex.timeout", f"Codex 已达到执行总时限 {self.total_timeout_seconds:g} 秒")
            raise TimeoutError(f"Codex app-server exceeded total timeout of {self.total_timeout_seconds:g} seconds")
        return self._total_deadline

    def _read(self, request_deadline: float | None = None) -> dict[str, Any]:
        if self.process.stdout is None:
            raise RuntimeError("Codex app-server stdout is unavailable")
        started = self.clock()
        deadline = started + self.timeout_seconds
        if request_deadline is not None:
            deadline = min(deadline, request_deadline)
        while True:
            now = self.clock()
            total_deadline = self._check_total_deadline(now)
            remaining = deadline - now
            if remaining <= 0:
                self._emit(
                    "ERROR", "codex.timeout",
                    f"Codex 等待响应超过 {self.timeout_seconds:g} 秒",
                )
                raise TimeoutError(
                    f"Codex app-server did not respond within {self.timeout_seconds:g} seconds"
                )
            newline = self._read_buffer.find(b"\n")
            if newline >= 0:
                line = bytes(self._read_buffer[:newline])
                del self._read_buffer[:newline + 1]
                return json.loads(line)
            wait_seconds = min(self.heartbeat_seconds, remaining, total_deadline - now)
            readable, _, _ = select.select(
                [self.process.stdout], [], [], wait_seconds
            )
            if readable:
                # Read bytes directly: TextIOWrapper.readline() both prefetches
                # later messages and can block indefinitely on a partial line.
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if chunk:
                    self._read_buffer.extend(chunk)
                    continue
                if self._read_buffer:
                    line = bytes(self._read_buffer)
                    self._read_buffer.clear()
                    return json.loads(line)
                raise RuntimeError(f"Codex app-server exited with code {self.process.poll()}")
            elapsed = self.clock() - started
            self._emit(
                "INFO", "codex.heartbeat",
                f"Codex 正在执行，已等待 {elapsed:.0f} 秒",
            )

    def _emit(self, level: ProgressLevel, name: str, message: str) -> None:
        try:
            self.progress(ProgressEvent(level, name, message))
        except Exception:
            pass


def strict_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Convert Pydantic output to the strict object shape required by Codex."""
    result: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "default":
            continue
        if isinstance(value, dict):
            result[key] = strict_json_schema(value)
        elif isinstance(value, list):
            result[key] = [
                strict_json_schema(item) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            result[key] = value
    properties = result.get("properties")
    if isinstance(properties, dict):
        result["additionalProperties"] = False
        result["required"] = list(properties)
    return result
