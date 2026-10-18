from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

_override: Callable[[], datetime] | None = None


def utcnow() -> datetime:
    if _override is not None:
        return _override()
    return datetime.now(timezone.utc)


def set_clock(fn: Callable[[], datetime] | None) -> None:
    global _override
    _override = fn


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
