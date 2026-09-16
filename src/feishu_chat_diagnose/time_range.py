from __future__ import annotations

from datetime import datetime, timedelta
import re
from zoneinfo import ZoneInfo

from .models import TimeRange

SHANGHAI = ZoneInfo("Asia/Shanghai")
_DURATION = re.compile(r"^(?P<amount>\d+)(?P<unit>[mhd])$")


def _parse_explicit(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=SHANGHAI) if parsed.tzinfo is None else parsed


def _parse_duration(value: str) -> timedelta:
    match = _DURATION.fullmatch(value.strip())
    if match is None:
        raise ValueError("since must use a positive duration such as 30m, 2h, or 1d")
    amount = int(match.group("amount"))
    if amount <= 0:
        raise ValueError("since duration must be positive")
    unit = match.group("unit")
    return {
        "m": timedelta(minutes=amount),
        "h": timedelta(hours=amount),
        "d": timedelta(days=amount),
    }[unit]


def resolve_time_range(
    *,
    since: str | None,
    start: str | None,
    end: str | None,
    now: datetime | None = None,
) -> TimeRange:
    current = now or datetime.now(SHANGHAI)
    if since and (start or end):
        raise ValueError("since and explicit start/end are mutually exclusive")
    if since:
        resolved_end = current
        resolved_start = current - _parse_duration(since)
    elif start or end:
        if not start or not end:
            raise ValueError("start and end must be provided together")
        resolved_start = _parse_explicit(start)
        resolved_end = _parse_explicit(end)
    else:
        resolved_end = current
        resolved_start = current - timedelta(hours=2)
    if resolved_start >= resolved_end:
        raise ValueError("start must be before end")
    return TimeRange(start=resolved_start, end=resolved_end)

