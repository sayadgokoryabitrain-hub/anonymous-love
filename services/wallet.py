from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.clock import utcnow
from core.errors import AppError, InsufficientFunds
from database.models import WalletAccount, WalletTransaction

REGISTRATION_BONUS = "registration_bonus"
DAILY_BONUS = "daily_bonus"
REFERRAL_REWARD = "referral_reward"
FIRST_CHAT_BONUS = "first_chat_bonus"
GIFT_PURCHASE = "gift_purchase"
TASK_REWARD = "task_reward"
PROMO = "promo"
COIN_PURCHASE = "coin_purchase"
VIP_PURCHASE = "vip_purchase"
SEARCH_FEE = "search_fee"
GAME_BONUS = "game_bonus"
ADMIN_ADJUSTMENT = "admin_adjustment"
REFUND = "refund"


class VersionConflict(Exception):
    pass


async def get_or_create_account(session: AsyncSession, user_id: int) -> WalletAccount:
    account = await session.get(WalletAccount, user_id)
    if account is None:
        account = WalletAccount(user_id=user_id, balance=0, version=0)
        session.add(account)
        await session.flush()
    return account


async def get_transaction(session: AsyncSession, idempotency_key: str) -> WalletTransaction | None:
    return await session.scalar(select(WalletTransaction).where(WalletTransaction.idempotency_key == idempotency_key))


async def apply(
    session: AsyncSession,
    user_id: int,
    amount: int,
    operation_type: str,
    *,
    source: str,
    idempotency_key: str,
    actor: str = "system",
    external_payment_id: str | None = None,
    reverses_transaction_id: int | None = None,
    meta: dict | None = None,
) -> WalletTransaction:
    if not idempotency_key or len(idempotency_key) > 160:
        raise AppError("bad_idempotency", "errors.generic")
    if amount == 0:
        raise AppError("zero_amount", "errors.generic")
    existing = await get_transaction(session, idempotency_key)
    if existing:
        return existing
    account = await get_or_create_account(session, user_id)
    for _ in range(5):
        existing = await get_transaction(session, idempotency_key)
        if existing:
            return existing
        await session.refresh(account)
        if account.balance + amount < 0:
            raise InsufficientFunds()
        expected = account.balance + amount
        tx = WalletTransaction(
            user_id=user_id,
            operation_type=operation_type,
            amount=amount,
            balance_after=expected,
            source=source[:128],
            status="completed",
            external_payment_id=external_payment_id,
            actor=actor[:64],
            idempotency_key=idempotency_key,
            reverses_transaction_id=reverses_transaction_id,
            meta=meta,
            created_at=utcnow(),
        )
        try:
            async with session.begin_nested():
                result = await session.execute(
                    update(WalletAccount)
                    .where(
                        WalletAccount.user_id == user_id,
                        WalletAccount.version == account.version,
                        WalletAccount.balance + amount >= 0,
                    )
                    .values(
                        balance=WalletAccount.balance + amount,
                        version=WalletAccount.version + 1,
                        updated_at=utcnow(),
                    )
                )
                if result.rowcount != 1:
                    raise VersionConflict()
                session.add(tx)
                await session.flush()
        except VersionConflict:
            continue
        except IntegrityError:
            existing = await get_transaction(session, idempotency_key)
            if existing:
                return existing
            raise
        account.balance = expected
        account.version = account.version + 1
        return tx
    raise AppError("wallet_conflict", "errors.generic", status_code=409)


async def history(session: AsyncSession, user_id: int, limit: int = 10) -> list[WalletTransaction]:
    rows = await session.execute(
        select(WalletTransaction)
        .where(WalletTransaction.user_id == user_id)
        .order_by(WalletTransaction.id.desc())
        .limit(limit)
    )
    return list(rows.scalars().all())
