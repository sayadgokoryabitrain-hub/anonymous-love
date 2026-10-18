from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.clock import utcnow
from database.models import Referral, ReferralReward, User
from database.repositories.users import get_user
from services.notify import enqueue
from services.settings_store import get_int_setting
from services.wallet import REFERRAL_REWARD, apply
from core.i18n import t


async def attach(session: AsyncSession, invitee: User, inviter: User) -> Referral | None:
    if inviter.id == invitee.id:
        return None
    if inviter.telegram_id_hash and inviter.telegram_id_hash == invitee.telegram_id_hash:
        return None
    existing = await session.scalar(select(Referral).where(Referral.invitee_id == invitee.id))
    if existing:
        return existing
    referral = Referral(inviter_id=inviter.id, invitee_id=invitee.id, status="pending")
    hour_ago = utcnow() - timedelta(hours=1)
    burst = await session.scalar(
        select(func.count(Referral.id)).where(Referral.inviter_id == inviter.id, Referral.created_at >= hour_ago)
    )
    if burst and int(burst) >= 10:
        referral.suspicious = True
        referral.frozen = True
        referral.status = "suspicious"
    session.add(referral)
    invitee.referred_by_id = inviter.id
    await session.flush()
    return referral


async def qualify(session: AsyncSession, invitee: User) -> Referral | None:
    if invitee.verification_status != "approved" or not invitee.registration_completed:
        return None
    referral = await session.scalar(select(Referral).where(Referral.invitee_id == invitee.id))
    if referral is None or referral.frozen or referral.status in {"rewarded", "rejected", "suspicious"}:
        return referral
    if referral.inviter_id == invitee.id:
        referral.status = "rejected"
        return referral
    inviter = await get_user(session, referral.inviter_id)
    if inviter is None or inviter.status in {"banned", "deleted"}:
        referral.status = "rejected"
        return referral
    amount = await get_int_setting(session, "referral_bonus")
    tx = await apply(
        session,
        inviter.id,
        amount,
        REFERRAL_REWARD,
        source=f"referral:{invitee.id}",
        idempotency_key=f"referral:{invitee.id}",
        meta={"invitee_id": invitee.id},
    )
    reward = await session.scalar(select(ReferralReward).where(ReferralReward.referral_id == referral.id))
    if reward is None:
        session.add(ReferralReward(referral_id=referral.id, transaction_id=tx.id, amount=amount))
    referral.status = "rewarded"
    referral.qualified_at = utcnow()
    await enqueue(session, inviter, t(inviter.language, "referral.reward_note", amount=amount))
    await session.flush()
    return referral


async def stats(session: AsyncSession, user_id: int) -> dict[str, int]:
    rows = (await session.execute(select(Referral).where(Referral.inviter_id == user_id))).scalars().all()
    rewarded = [row for row in rows if row.status == "rewarded"]
    coins = 0
    if rewarded:
        ids = [row.id for row in rewarded]
        coins = int(
            (
                await session.scalar(
                    select(func.coalesce(func.sum(ReferralReward.amount), 0)).where(ReferralReward.referral_id.in_(ids))
                )
            )
            or 0
        )
    return {
        "invited": len(rows),
        "confirmed": len(rewarded),
        "active": len([row for row in rows if row.status in {"pending", "rewarded"}]),
        "coins": coins,
    }


async def freeze(session: AsyncSession, referral_id: int, frozen: bool) -> Referral | None:
    referral = await session.get(Referral, referral_id)
    if referral is None:
        return None
    referral.frozen = frozen
    if frozen and referral.status == "pending":
        referral.status = "suspicious"
    if not frozen and referral.status == "suspicious":
        referral.status = "pending"
    return referral
