from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .errors import LarkCliError


class LarkCliRunner:
    def __init__(self, binary: str = "lark-cli", secret_values: set[str] | None = None) -> None:
        self.binary = binary
        self.secret_values = secret_values or set()

    def run(self, args: Sequence[str], cwd: Path | None = None) -> dict[str, Any]:
        completed = subprocess.run(
            [self.binary, *args],
            capture_output=True,
            text=True,
            shell=False,
            check=False,
            cwd=cwd,
        )
        if completed.returncode != 0:
            detail = self._error_detail(completed.stderr, completed.stdout)
            raise LarkCliError(self._redact(detail))

        try:
            result = json.loads(completed.stdout or "{}")
        except json.JSONDecodeError as error:
            raise LarkCliError(f"lark-cli returned invalid JSON: {error}") from error
        if not isinstance(result, dict):
            raise LarkCliError("lark-cli returned a non-object JSON value")
        return result

    def _error_detail(self, stderr: str, stdout: str) -> str:
        raw = stderr.strip() or stdout.strip() or "unknown lark-cli failure"
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return raw

        error = payload.get("error", payload) if isinstance(payload, dict) else payload
        if isinstance(error, dict):
            parts = [str(error.get(key, "")) for key in ("type", "message", "hint")]
            detail = ": ".join(part for part in parts if part)
            missing = error.get("missing")
            if isinstance(missing, list) and missing:
                detail = f"missing scopes: {', '.join(str(item) for item in missing)}"
            suggestion = error.get("suggestion")
            if suggestion:
                detail = f"{detail}; {suggestion}" if detail else str(suggestion)
            if detail:
                return detail
            return json.dumps(error, ensure_ascii=False)
        return str(error)

    def _redact(self, value: str) -> str:
        for secret in self.secret_values:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value
