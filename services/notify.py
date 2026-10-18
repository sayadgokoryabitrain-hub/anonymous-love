from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from database.models import NotificationOutbox, User


async def enqueue(
    session: AsyncSession,
    user: User,
    text: str,
    *,
    template: str | None = None,
    payload: dict | None = None,
) -> None:
    if not user.telegram_id:
        return
    session.add(
        NotificationOutbox(
            user_id=user.id,
            telegram_id=user.telegram_id,
            language=user.language or "ru",
            text=text[:4000],
            template=template,
            payload=payload or {},
            status="pending",
        )
    )
