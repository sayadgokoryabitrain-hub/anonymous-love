from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings
from core.security import hash_password
from database.models import AdminUser, CoinPackage, DailyTask, Gift, Interest, SystemSetting, VipPlan
from services.settings_store import DEFAULTS

logger = logging.getLogger(__name__)

INTERESTS = [
    "music",
    "games",
    "movies",
    "sports",
    "travel",
    "chat",
    "relationships",
    "languages",
    "random",
]

GIFTS = [
    ("rose", "🌹", 10, False),
    ("heart", "💖", 25, False),
    ("teddy", "🧸", 40, False),
    ("star", "⭐", 60, False),
    ("crown", "👑", 100, True),
    ("diamond", "💎", 200, True),
]

TASKS = [
    ("complete_profile", 15, False),
    ("five_interests", 10, False),
    ("start_chat", 10, True),
    ("answer_qotd", 5, True),
    ("three_messages", 15, True),
    ("play_game", 10, True),
    ("search_today", 8, True),
    ("send_gift", 12, True),
    ("invite_friend", 10, False),
    ("verify", 20, False),
]

PACKAGES = [
    ("small", 100, 50),
    ("medium", 500, 200),
    ("large", 1200, 400),
]

PLANS = [
    ("week", 7, 200, 100),
    ("month", 30, 600, 250),
]


async def seed_data(session: AsyncSession) -> None:
    for key, value in DEFAULTS.items():
        if await session.get(SystemSetting, key) is None:
            session.add(SystemSetting(key=key, value=value))
    existing = set((await session.execute(select(Interest.key))).scalars().all())
    for index, key in enumerate(INTERESTS):
        if key not in existing:
            session.add(Interest(key=key, sort_order=index))
    gift_codes = set((await session.execute(select(Gift.code))).scalars().all())
    for index, (code, emoji, price, vip_only) in enumerate(GIFTS):
        if code not in gift_codes:
            session.add(Gift(code=code, emoji=emoji, price=price, vip_only=vip_only, sort_order=index))
    task_codes = set((await session.execute(select(DailyTask.code))).scalars().all())
    for index, (code, reward, repeatable) in enumerate(TASKS):
        if code not in task_codes:
            session.add(DailyTask(code=code, reward=reward, repeatable=repeatable, sort_order=index))
    package_codes = set((await session.execute(select(CoinPackage.code))).scalars().all())
    for index, (code, coins, stars) in enumerate(PACKAGES):
        if code not in package_codes:
            session.add(CoinPackage(code=code, coins=coins, stars=stars, sort_order=index))
    plan_codes = set((await session.execute(select(VipPlan.code))).scalars().all())
    for code, days, coins, stars in PLANS:
        if code not in plan_codes:
            session.add(VipPlan(code=code, days=days, coin_price=coins, star_price=stars))
    await session.flush()


async def ensure_bootstrap_admin(session: AsyncSession) -> None:
    settings = get_settings()
    if not settings.admin_bootstrap_username or not settings.admin_bootstrap_password:
        return
    if len(settings.admin_bootstrap_password) < 10:
        logger.warning("bootstrap admin skipped: password shorter than 10 characters")
        return
    exists = await session.scalar(select(AdminUser.id).limit(1))
    if exists:
        return
    session.add(
        AdminUser(
            username=settings.admin_bootstrap_username.strip().lower(),
            password_hash=hash_password(settings.admin_bootstrap_password),
            role="superadmin",
            is_active=True,
        )
    )
    logger.warning("bootstrap superadmin created for %s", settings.admin_bootstrap_username)
