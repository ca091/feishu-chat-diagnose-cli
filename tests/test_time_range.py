from datetime import datetime

import pytest

from feishu_chat_diagnose.time_range import resolve_time_range


def test_resolve_relative_time_range() -> None:
    now = datetime.fromisoformat("2026-07-15T16:00:00+08:00")

    result = resolve_time_range(since="2h", start=None, end=None, now=now)

    assert result.start.isoformat() == "2026-07-15T14:00:00+08:00"
    assert result.end == now


def test_explicit_naive_dates_use_shanghai_timezone() -> None:
    now = datetime.fromisoformat("2026-07-15T16:00:00+08:00")

    result = resolve_time_range(
        since=None,
        start="2026-07-15 14:00",
        end="2026-07-15 15:00",
        now=now,
    )

    assert result.start.isoformat() == "2026-07-15T14:00:00+08:00"


def test_rejects_since_with_explicit_dates() -> None:
    now = datetime.fromisoformat("2026-07-15T16:00:00+08:00")

    with pytest.raises(ValueError, match="mutually exclusive"):
        resolve_time_range(
            since="2h",
            start="2026-07-15 14:00",
            end="2026-07-15 16:00",
            now=now,
        )


def test_rejects_non_positive_range() -> None:
    now = datetime.fromisoformat("2026-07-15T16:00:00+08:00")

    with pytest.raises(ValueError, match="start must be before end"):
        resolve_time_range(
            since=None,
            start="2026-07-15 16:00",
            end="2026-07-15 15:00",
            now=now,
        )
