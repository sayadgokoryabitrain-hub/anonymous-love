from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.clock import utcnow
from core.i18n import t
from database.models import NotificationOutbox

logger = logging.getLogger(__name__)


def markup_for(row: NotificationOutbox) -> InlineKeyboardMarkup | None:
    lang = row.language or "ru"
    payload = row.payload or {}
    if row.template == "chat_panel":
        from bot.keyboards.common import chat_keyboard

        return chat_keyboard(lang)
    if row.template == "rematch":
        session_id = payload.get("session_id", "")
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text=t(lang, "rematch.yes"), callback_data=f"rematch:yes:{session_id}"),
                    InlineKeyboardButton(text=t(lang, "rematch.no"), callback_data=f"rematch:no:{session_id}"),
                ]
            ]
        )
    if row.template == "rematch_open":
        session_id = payload.get("session_id", "")
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=t(lang, "rematch.open"), callback_data=f"rematch:open:{session_id}")]
            ]
        )
    if row.template == "reveal_prompt":
        request_id = payload.get("request_id")
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text=t(lang, "reveal.accept"), callback_data=f"reveal:accept:{request_id}"),
                    InlineKeyboardButton(text=t(lang, "reveal.decline"), callback_data=f"reveal:decline:{request_id}"),
                ]
            ]
        )
    if row.template == "favorite_prompt":
        user_id = payload.get("user_id")
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=t(lang, "favorites.ask"), callback_data=f"fav:yes:{user_id}")]]
        )
    return None


async def deliver_pending(factory: async_sessionmaker[AsyncSession], bot: Bot, limit: int = 30) -> None:
    async with factory() as session:
        rows = (
            await session.execute(
                select(NotificationOutbox)
                .where(NotificationOutbox.status == "pending")
                .order_by(NotificationOutbox.id)
                .limit(limit)
            )
        ).scalars().all()
        claimed: list[NotificationOutbox] = []
        for row in rows:
            row.status = "sending"
            claimed.append(row)
        await session.commit()
    if not claimed:
        return
    async with factory() as session:
        for row in claimed:
            current = await session.get(NotificationOutbox, row.id)
            if current is None or current.status != "sending":
                continue
            try:
                payload = current.payload or {}
                if current.template == "sticker" and payload.get("file_id"):
                    await bot.send_sticker(current.telegram_id, payload["file_id"])
                elif current.template == "photo" and payload.get("file_id"):
                    await bot.send_photo(
                        current.telegram_id,
                        payload["file_id"],
                        caption=(current.text or "")[:1024],
                    )
                else:
                    await bot.send_message(
                        current.telegram_id,
                        current.text[:4000],
                        reply_markup=markup_for(current),
                    )
                current.status = "sent"
                current.sent_at = utcnow()
            except Exception as exc:
                current.attempts += 1
                current.last_error = str(exc)[:300]
                current.status = "failed" if current.attempts >= 5 else "pending"
                logger.info("notification delivery failed id=%s", current.id)
        await session.commit()
