from __future__ import annotations

import asyncio
import logging

from aiogram import Bot

from bot.app import build_dispatcher, setup_commands
from bot.delivery import deliver_pending
from core.config import get_settings, validate_runtime
from core.logging_config import setup_logging
from database.session import get_session_factory, init_database
from services.maintenance import run_maintenance
from services.payments import settle_crypto_invoices

logger = logging.getLogger(__name__)


async def crypto_watcher() -> None:
    factory = get_session_factory()
    while True:
        try:
            async with factory() as session:
                await settle_crypto_invoices(session)
                await session.commit()
        except Exception:
            logger.exception("crypto settlement failed")
        await asyncio.sleep(12)


async def housekeeping(bot: Bot) -> None:
    factory = get_session_factory()
    while True:
        try:
            await deliver_pending(factory, bot)
            async with factory() as session:
                await run_maintenance(session)
                await session.commit()
        except Exception:
            logger.exception("housekeeping failed")
        await asyncio.sleep(30)


async def main() -> None:
    setup_logging()
    settings = get_settings()
    validate_runtime(settings)
    if not settings.bot_token:
        raise SystemExit("BOT_TOKEN is empty. Create a bot in @BotFather and set BOT_TOKEN in .env")
    await init_database()
    bot = Bot(token=settings.bot_token)
    dispatcher = build_dispatcher(get_session_factory(), bot)
    asyncio.create_task(housekeeping(bot))
    asyncio.create_task(crypto_watcher())
    if settings.use_webhook:
        logger.info("USE_WEBHOOK is on; polling is disabled. The API process receives updates.")
        while True:
            await asyncio.sleep(3600)
    try:
        await setup_commands(bot)
        await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
