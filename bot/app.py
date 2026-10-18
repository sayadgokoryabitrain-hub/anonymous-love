from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.handlers import group_router, router
from bot.middlewares import DatabaseMiddleware
from core.config import get_settings
from core.i18n import t

logger = logging.getLogger(__name__)

COMMANDS = (
    "start",
    "profile",
    "search",
    "stop",
    "verify",
    "balance",
    "referral",
    "settings",
    "help",
    "delete_account",
)


def build_storage():
    settings = get_settings()
    if not settings.redis_enabled:
        return MemoryStorage()
    try:
        from aiogram.fsm.storage.redis import RedisStorage

        return RedisStorage.from_url(settings.redis_url)
    except Exception:
        logger.warning("Redis FSM storage is unavailable; using memory")
        return MemoryStorage()


def build_dispatcher(factory: async_sessionmaker[AsyncSession], bot: Bot) -> Dispatcher:
    dispatcher = Dispatcher(storage=build_storage())
    dispatcher.update.middleware(DatabaseMiddleware(factory, bot))
    dispatcher.include_router(group_router)
    dispatcher.include_router(router)
    return dispatcher


async def setup_commands(bot: Bot) -> None:
    for language in ("ru", "hy", "en"):
        await bot.set_my_commands(
            [BotCommand(command=name, description=t(language, f"cmd.{name}")[:256]) for name in COMMANDS],
            language_code=language,
        )
