from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.clock import as_utc, utcnow
from core.errors import AppError, InsufficientFunds
from database.models import Subscription, VipPlan
from services.wallet import VIP_PURCHASE, apply


async def is_vip(session: AsyncSession, user_id: int, now=None) -> bool:
    moment = now or utcnow()
    rows = await session.execute(
        select(Subscription).where(Subscription.user_id == user_id, Subscription.status == "active")
    )
    for sub in rows.scalars().all():
        ends = as_utc(sub.ends_at)
        if ends and ends > moment:
            return True
        sub.status = "expired"
    return False


async def active_until(session: AsyncSession, user_id: int):
    rows = await session.execute(
        select(Subscription)
        .where(Subscription.user_id == user_id, Subscription.status == "active")
        .order_by(Subscription.ends_at.desc())
    )
    sub = rows.scalars().first()
    if sub is None:
        return None
    ends = as_utc(sub.ends_at)
    if ends and ends <= utcnow():
        sub.status = "expired"
        return None
    return ends


async def grant(
    session: AsyncSession,
    user_id: int,
    plan: VipPlan,
    *,
    source: str,
    payment_order_id: str | None = None,
    idempotency_key: str,
    charge_coins: bool = False,
) -> Subscription:
    existing = await session.scalar(select(Subscription).where(Subscription.payment_order_id == idempotency_key))
    if existing:
        return existing
    if charge_coins:
        try:
            await apply(
                session,
                user_id,
                -plan.coin_price,
                VIP_PURCHASE,
                source="vip",
                idempotency_key=idempotency_key,
                meta={"plan": plan.code},
            )
        except InsufficientFunds:
            raise
    now = utcnow()
    current = await active_until(session, user_id)
    start = current if current and current > now else now
    sub = Subscription(
        user_id=user_id,
        plan_code=plan.code,
        status="active",
        starts_at=now,
        ends_at=start + timedelta(days=plan.days),
        source=source,
        payment_order_id=idempotency_key,
    )
    session.add(sub)
    await session.flush()
    return sub


async def grant_days(
    session: AsyncSession,
    user_id: int,
    days: int,
    *,
    source: str,
    idempotency_key: str,
) -> Subscription:
    if days < 1 or days > 365:
        raise AppError("bad_days", "errors.generic")
    existing = await session.scalar(select(Subscription).where(Subscription.payment_order_id == idempotency_key))
    if existing:
        return existing
    now = utcnow()
    current = await active_until(session, user_id)
    start = current if current and current > now else now
    sub = Subscription(
        user_id=user_id,
        plan_code="admin",
        status="active",
        starts_at=now,
        ends_at=start + timedelta(days=days),
        source=source,
        payment_order_id=idempotency_key,
    )
    session.add(sub)
    await session.flush()
    return sub


async def get_plan(session: AsyncSession, code: str) -> VipPlan | None:
    return await session.scalar(select(VipPlan).where(VipPlan.code == code, VipPlan.active.is_(True)))
