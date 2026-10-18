from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Interest, Profile, User, UserInterest, UserPreference, WalletAccount


async def get_user(session: AsyncSession, user_id: int) -> User | None:
    return await session.get(User, user_id)


async def get_by_telegram(session: AsyncSession, telegram_id: int) -> User | None:
    return await session.scalar(select(User).where(User.telegram_id == telegram_id))


async def get_by_hash(session: AsyncSession, digest: str) -> User | None:
    return await session.scalar(select(User).where(User.telegram_id_hash == digest))


async def get_by_referral_code(session: AsyncSession, code: str) -> User | None:
    return await session.scalar(select(User).where(User.referral_code == code))


async def get_profile(session: AsyncSession, user_id: int) -> Profile | None:
    return await session.get(Profile, user_id)


async def get_prefs(session: AsyncSession, user_id: int) -> UserPreference | None:
    return await session.get(UserPreference, user_id)


async def interest_keys(session: AsyncSession, user_id: int) -> list[str]:
    rows = await session.execute(
        select(Interest.key)
        .join(UserInterest, UserInterest.interest_id == Interest.id)
        .where(UserInterest.user_id == user_id)
        .order_by(Interest.sort_order)
    )
    return list(rows.scalars().all())


async def get_wallet(session: AsyncSession, user_id: int) -> WalletAccount | None:
    return await session.get(WalletAccount, user_id)
