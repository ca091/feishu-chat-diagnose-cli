from __future__ import annotations

from dataclasses import dataclass


class DiagnoseError(RuntimeError):
    """Base error for expected CLI failures."""


class ConfigurationError(DiagnoseError):
    pass


class InvalidStageTransition(DiagnoseError):
    pass


class ContextTooLarge(DiagnoseError):
    pass


class VideoPathDenied(DiagnoseError):
    pass


class LarkCliError(DiagnoseError):
    pass


@dataclass(slots=True)
class ModelOutputError(DiagnoseError):
    raw_response: str
    validation_errors: str

    def __str__(self) -> str:
        return f"Model output failed validation: {self.validation_errors}"

