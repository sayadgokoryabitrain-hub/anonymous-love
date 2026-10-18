from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from database.models import SystemSetting

DEFAULTS: dict[str, int] = {
    "registration_bonus": 100,
    "daily_bonus": 20,
    "referral_bonus": 100,
    "first_chat_bonus": 10,
    "advanced_search_price": 5,
    "min_chat_seconds_for_bonus": 300,
    "search_timeout_sec": 600,
    "chat_idle_timeout_sec": 1800,
    "recent_match_hours": 24,
    "message_retention_days": 30,
    "report_daily_limit": 5,
    "photo_limit_per_10min": 5,
    "max_free_interests": 5,
    "game_bonus": 5,
    "vip_daily_extra": 10,
}

SETTING_KEYS = tuple(DEFAULTS)


async def get_int_setting(session: AsyncSession, key: str) -> int:
    if key not in DEFAULTS:
        raise KeyError(key)
    row = await session.get(SystemSetting, key)
    if row is None:
        return DEFAULTS[key]
    try:
        return int(row.value)
    except (TypeError, ValueError):
        return DEFAULTS[key]
