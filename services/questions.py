from __future__ import annotations

from core.clock import utcnow


def question_of_day_key(moment=None) -> str:
    current = moment or utcnow()
    return f"qotd.{(current.timetuple().tm_yday % 8) + 1}"
