from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Interest, Profile, User, UserInterest, UserPreference, WalletAccount
from services.wallet import get_or_create_account


async def make_user(
    session: AsyncSession,
    *,
    telegram_id: int,
    gender: str,
    age: int = 25,
    language: str = "ru",
    nickname: str | None = None,
    interests: list[str] | None = None,
    country: str = "AM",
    city: str = "Yerevan",
) -> User:
    nick = nickname or f"user{telegram_id}"
    user = User(
        telegram_id=telegram_id,
        telegram_id_hash=f"hash-{telegram_id}",
        language=language,
        language_chosen=True,
        referral_code=f"REF{telegram_id}",
        age_confirmed=True,
        rules_accepted=True,
        registration_completed=True,
        registration_step="done",
        verification_status="unverified",
    )
    session.add(user)
    await session.flush()
    session.add(
        Profile(
            user_id=user.id,
            nickname=nick,
            nickname_folded=nick.casefold(),
            bio="Short bio for tests.",
            gender=gender,
            age=age,
            country=country,
            city=city,
            country_set=True,
            city_set=True,
        )
    )
    session.add(
        UserPreference(
            user_id=user.id,
            age_min=18,
            age_max=40,
            prefs_set=True,
        )
    )
    await get_or_create_account(session, user.id)
    if interests:
        rows = (await session.execute(select(Interest).where(Interest.key.in_(interests)))).scalars().all()
        for interest in rows:
            session.add(UserInterest(user_id=user.id, interest_id=interest.id))
    await session.flush()
    return user


async def balance(session: AsyncSession, user_id: int) -> int:
    account = await session.get(WalletAccount, user_id)
    return 0 if account is None else account.balance
