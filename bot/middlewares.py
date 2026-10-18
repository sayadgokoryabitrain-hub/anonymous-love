from __future__ import annotations

import logging

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.errors import AppError
from core.i18n import refresh_overrides, t
from core.rate_limit import get_limiter
from bot.delivery import deliver_pending
from services.maintenance import log_error

logger = logging.getLogger(__name__)


class DatabaseMiddleware(BaseMiddleware):
    def __init__(self, factory: async_sessionmaker[AsyncSession], bot=None) -> None:
        self.factory = factory
        self.bot = bot

    async def __call__(self, handler, event: TelegramObject, data: dict):
        user = getattr(event, "from_user", None)
        if user is not None and not await get_limiter().allow(f"upd:{user.id}", 45, 15):
            await _reply(event, t("ru", "errors.rate_limit"))
            return None
        async with self.factory() as session:
            data["session"] = session
            try:
                await refresh_overrides(session)
                result = await handler(event, data)
                await session.commit()
            except AppError as exc:
                await session.rollback()
                await _reply(event, t("ru", exc.message_key, **exc.params))
                return None
            except Exception as exc:
                await session.rollback()
                logger.exception("bot handler failed")
                try:
                    async with self.factory() as log_session:
                        await log_error(log_session, "bot", exc.__class__.__name__, {"detail": str(exc)[:300]})
                        await log_session.commit()
                except Exception:
                    logger.exception("failed to store error log")
                await _reply(event, t("ru", "errors.generic"))
                return None
        if self.bot is not None:
            await deliver_pending(self.factory, self.bot)
        return result


async def _reply(event: TelegramObject, text: str) -> None:
    try:
        if isinstance(event, Message):
            await event.answer(text[:4000])
        elif isinstance(event, CallbackQuery):
            try:
                await event.answer(text[:180], show_alert=True)
            except Exception:
                if event.message is not None:
                    await event.message.answer(text[:4000])
    except Exception:
        logger.info("could not send error reply")
