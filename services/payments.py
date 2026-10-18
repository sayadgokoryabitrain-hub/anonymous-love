from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.clock import as_utc, utcnow
from core.config import get_settings
from core.errors import AppError, InsufficientFunds
from core.i18n import t
from database.models import CoinPackage, PaymentEvent, PaymentOrder, User, VipPlan
from services import cryptopay
from services.notify import enqueue
from services.vip import get_plan, grant
from services.wallet import COIN_PURCHASE, REFUND, apply

logger = logging.getLogger(__name__)


def _payments_open() -> str:
    settings = get_settings()
    if settings.payment_mode == "disabled":
        raise AppError("payments_disabled", "pay.disabled")
    if settings.payment_mode == "test" and settings.environment == "production":
        raise AppError("payments_disabled", "pay.disabled")
    if settings.payment_mode not in {"test", "stars", "crypto", "both"}:
        raise AppError("payments_disabled", "pay.disabled")
    return settings.payment_mode


def open_channels() -> list[str]:
    mode = _payments_open()
    if mode == "test":
        return ["test"]
    if mode == "stars":
        return ["stars"]
    settings = get_settings()
    if mode == "crypto":
        return ["crypto"] if settings.crypto_pay_token else []
    channels = ["stars"]
    if settings.crypto_pay_token:
        channels.append("crypto")
    return channels


def quote_for_stars(stars: int) -> tuple[str, str]:
    settings = get_settings()
    cents = stars * max(settings.star_usd_cents, 1)
    asset = (settings.crypto_asset or "USDT").upper()
    return f"{cents // 100}.{cents % 100:02d}", asset


def _same_money(left: str, right: str) -> bool:
    try:
        return Decimal(left) == Decimal(right)
    except Exception:
        return False


def _mode_matches(order_mode: str) -> bool:
    mode = get_settings().payment_mode
    if mode == "both":
        return order_mode in {"stars", "crypto"}
    return order_mode == mode


def _channel(requested: str) -> str:
    mode = _payments_open()
    if mode == "test":
        return "test"
    if requested not in open_channels():
        raise AppError("channel_closed", "pay.disabled")
    return requested


async def create_coin_order(
    session: AsyncSession, user: User, package: CoinPackage, *, channel: str = "stars"
) -> PaymentOrder:
    chosen = _channel(channel)
    amount, asset = quote_for_stars(package.stars) if chosen == "crypto" else (None, None)
    order = PaymentOrder(
        id=str(uuid.uuid4()),
        user_id=user.id,
        kind="coins",
        package_code=package.code,
        coins=package.coins,
        stars=package.stars,
        currency="XTR" if chosen != "crypto" else asset or "USDT",
        status="pending",
        mode=chosen,
        quote_amount=amount,
        quote_asset=asset,
    )
    session.add(order)
    await session.flush()
    return order


async def create_vip_order(session: AsyncSession, user: User, plan: VipPlan, *, channel: str = "stars") -> PaymentOrder:
    chosen = _channel(channel)
    amount, asset = quote_for_stars(plan.star_price) if chosen == "crypto" else (None, None)
    order = PaymentOrder(
        id=str(uuid.uuid4()),
        user_id=user.id,
        kind="vip",
        package_code=plan.code,
        coins=0,
        stars=plan.star_price,
        currency="XTR" if chosen != "crypto" else asset or "USDT",
        status="pending",
        mode=chosen,
        quote_amount=amount,
        quote_asset=asset,
    )
    session.add(order)
    await session.flush()
    return order


async def recent_crypto_order(session: AsyncSession, user_id: int, kind: str, package_code: str) -> PaymentOrder | None:
    rows = (
        await session.execute(
            select(PaymentOrder)
            .where(
                PaymentOrder.user_id == user_id,
                PaymentOrder.kind == kind,
                PaymentOrder.package_code == package_code,
                PaymentOrder.mode == "crypto",
                PaymentOrder.status == "pending",
            )
            .order_by(PaymentOrder.created_at.desc())
            .limit(5)
        )
    ).scalars().all()
    cutoff = utcnow() - timedelta(minutes=50)
    for row in rows:
        created = as_utc(row.created_at)
        if row.pay_url and created and created >= cutoff:
            return row
    return None


async def pre_checkout_ok(session: AsyncSession, order_id: str, currency: str, total: int) -> bool:
    order = await session.get(PaymentOrder, order_id)
    if order is None or order.status != "pending":
        return False
    return order.currency == currency and order.stars == total and currency == "XTR"


async def confirm(
    session: AsyncSession,
    order_id: str,
    *,
    charge_id: str,
    amount: int,
    currency: str,
    quoted: str | None = None,
) -> PaymentOrder:
    if not charge_id:
        raise AppError("missing_charge", "pay.fail")
    order = await session.get(PaymentOrder, order_id)
    if order is None:
        raise AppError("missing_order", "pay.fail")
    if not _mode_matches(order.mode):
        raise AppError("mode_mismatch", "pay.fail")
    if order.mode == "crypto":
        if not quoted or not order.quote_amount or not _same_money(quoted, order.quote_amount):
            raise AppError("amount_mismatch", "pay.fail")
        if currency != (order.quote_asset or ""):
            raise AppError("amount_mismatch", "pay.fail")
    elif currency != "XTR" or amount != order.stars:
        raise AppError("amount_mismatch", "pay.fail")
    if order.status == "paid":
        return order
    duplicate = await session.scalar(
        select(PaymentOrder).where(PaymentOrder.telegram_payment_charge_id == charge_id, PaymentOrder.id != order.id)
    )
    if duplicate:
        raise AppError("duplicate_charge", "pay.fail")
    result = await session.execute(
        update(PaymentOrder)
        .where(PaymentOrder.id == order.id, PaymentOrder.status == "pending")
        .values(status="paid", telegram_payment_charge_id=charge_id, paid_at=utcnow())
    )
    if result.rowcount != 1:
        await session.refresh(order)
        return order
    session.add(
        PaymentEvent(
            order_id=order.id,
            event_type="paid",
            external_id=charge_id,
            payload={"amount": amount, "currency": currency},
        )
    )
    user = await session.get(User, order.user_id)
    if order.kind == "coins":
        await apply(
            session,
            order.user_id,
            order.coins,
            COIN_PURCHASE,
            source="cryptobot" if order.mode == "crypto" else "telegram_stars",
            idempotency_key=f"pay:{charge_id}",
            external_payment_id=charge_id,
            meta={"order_id": order.id, "package": order.package_code},
        )
    elif order.kind == "vip":
        plan = await get_plan(session, order.package_code)
        if plan is None:
            raise AppError("missing_plan", "pay.fail")
        await grant(
            session,
            order.user_id,
            plan,
            source="cryptobot" if order.mode == "crypto" else "stars",
            idempotency_key=f"vip-pay:{charge_id}",
            charge_coins=False,
        )
    if user:
        if order.kind == "vip":
            notice = t(user.language, "pay.vip_success", package=order.package_code)
        else:
            notice = t(user.language, "pay.success", coins=order.coins, package=order.package_code)
        await enqueue(session, user, notice)
    await session.flush()
    await session.refresh(order)
    return order


async def mark_refunded(session: AsyncSession, order_id: str, charge_id: str) -> PaymentOrder:
    order = await session.get(PaymentOrder, order_id)
    if order is None:
        raise AppError("missing_order", "pay.fail", status_code=404)
    if order.status == "refunded":
        return order
    if order.status != "paid" or order.telegram_payment_charge_id != charge_id:
        raise AppError("not_paid", "pay.fail")
    existing = await session.scalar(
        select(PaymentEvent).where(
            PaymentEvent.order_id == order.id,
            PaymentEvent.event_type == "refund",
            PaymentEvent.external_id == charge_id,
        )
    )
    if existing:
        return order
    session.add(PaymentEvent(order_id=order.id, event_type="refund", external_id=charge_id, payload={}))
    if order.kind == "coins" and order.coins > 0:
        try:
            await apply(
                session,
                order.user_id,
                -order.coins,
                REFUND,
                source="telegram_stars",
                idempotency_key=f"refund:{charge_id}",
                external_payment_id=charge_id,
                meta={"order_id": order.id},
            )
        except InsufficientFunds:
            order.status = "refund_review"
            await session.flush()
            return order
    order.status = "refunded"
    await session.flush()
    user = await session.get(User, order.user_id)
    if user:
        await enqueue(session, user, t(user.language, "pay.refunded"))
    return order


async def attach_crypto_invoice(session: AsyncSession, order: PaymentOrder, description: str) -> PaymentOrder:
    if order.pay_url and order.provider_invoice_id:
        return order
    if not order.quote_amount or not order.quote_asset:
        raise AppError("missing_quote", "pay.fail")
    invoice = await cryptopay.create_invoice(
        amount=order.quote_amount,
        asset=order.quote_asset,
        description=description,
        payload=order.id,
    )
    order.provider_invoice_id = str(invoice["invoice_id"])
    order.pay_url = str(invoice["bot_invoice_url"])
    await session.flush()
    return order


async def settle_crypto_invoices(session: AsyncSession) -> int:
    settings = get_settings()
    if settings.payment_mode not in {"crypto", "both"} or not settings.crypto_pay_token:
        return 0
    rows = (
        await session.execute(
            select(PaymentOrder)
            .where(
                PaymentOrder.mode == "crypto",
                PaymentOrder.status == "pending",
                PaymentOrder.provider_invoice_id.is_not(None),
            )
            .limit(100)
        )
    ).scalars().all()
    if not rows:
        return 0
    try:
        invoices = await cryptopay.get_invoices([row.provider_invoice_id or "" for row in rows])
    except AppError:
        logger.warning("cryptopay invoice poll failed")
        return 0
    by_id = {str(item.get("invoice_id")): item for item in invoices}
    settled = 0
    for order in rows:
        invoice = by_id.get(order.provider_invoice_id or "")
        if invoice is None:
            continue
        if await _apply_invoice(session, order, invoice):
            settled += 1
    return settled


async def apply_crypto_update(session: AsyncSession, body: bytes, signature: str) -> None:
    if not cryptopay.signature_matches(body, signature):
        raise AppError("bad_signature", "pay.fail", status_code=401)
    import json

    try:
        payload = json.loads(body.decode("utf-8"))
    except ValueError as exc:
        raise AppError("bad_body", "pay.fail") from exc
    if payload.get("update_type") != "invoice_paid":
        return
    invoice = payload.get("payload") or {}
    if not isinstance(invoice, dict):
        return
    order = await session.get(PaymentOrder, str(invoice.get("payload") or ""))
    if order is None:
        return
    await _apply_invoice(session, order, invoice)


async def _apply_invoice(session: AsyncSession, order: PaymentOrder, invoice: dict) -> bool:
    status = str(invoice.get("status") or "")
    if status == "expired" and order.status == "pending":
        order.status = "expired"
        return False
    if status != "paid" or order.status == "paid":
        return False
    if str(invoice.get("payload") or "") != order.id:
        return False
    if order.provider_invoice_id and str(invoice.get("invoice_id")) != order.provider_invoice_id:
        return False
    await confirm(
        session,
        order.id,
        charge_id=f"crypto:{invoice.get('invoice_id')}",
        amount=order.stars,
        currency=str(invoice.get("asset") or ""),
        quoted=str(invoice.get("amount") or ""),
    )
    return order.status == "paid"
