from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from core.clock import utcnow
from core.config import get_settings
from core.errors import NotVerified
from core.security import sign_provider_body, validate_init_data, verify_provider_signature
from database.models import CoinPackage, QotdAnswer, Referral, Subscription, TaskCompletion, UserPreference, VipPlan, WalletTransaction
from services.accounts import answer_reveal, public_card, request_reveal
from services.chat import relay
from services.matching import find_partner
from services.cryptopay import signature_for
from services.engagement import claim_task, daily_codes
from services.payments import confirm, create_coin_order, quote_for_stars, mark_refunded
from services.referrals import attach, qualify
from services.verification import decide, latest_request, start_manual, submit_photo, submit_video_note
from services.vip import grant, is_vip
from services.wallet import COIN_PURCHASE
from tests.helpers import balance, make_user


@pytest.mark.asyncio
async def test_verification_statuses_and_referral_once(session):
    inviter = await make_user(session, telegram_id=41, gender="male", nickname="Inviter")
    invitee = await make_user(session, telegram_id=42, gender="female", nickname="Invitee")
    await attach(session, invitee, inviter)
    await qualify(session, invitee)
    assert await balance(session, inviter.id) == 0

    await start_manual(session, invitee)
    assert invitee.verification_status == "pending"
    with pytest.raises(Exception):
        await submit_video_note(session, invitee, file_id="circle-1")
    await submit_photo(session, invitee, file_id="f1", digest="abc")
    assert invitee.verification_status == "pending"
    await submit_video_note(session, invitee, file_id="circle-1")
    assert invitee.verification_status == "in_review"
    stored = await latest_request(session, invitee.id)
    await decide(session, stored, invitee, "approved", actor="admin:1", admin_id=None)
    assert invitee.verification_status == "approved"
    assert invitee.trust_score >= 40
    referral = await session.scalar(select(Referral).where(Referral.invitee_id == invitee.id))
    assert referral.status == "rewarded"
    assert await balance(session, inviter.id) == 100
    await qualify(session, invitee)
    assert await balance(session, inviter.id) == 100
    txs = (
        await session.execute(
            select(WalletTransaction).where(WalletTransaction.idempotency_key == f"referral:{invitee.id}")
        )
    ).scalars().all()
    assert len(txs) == 1

    other = await make_user(session, telegram_id=43, gender="male")
    await start_manual(session, other)
    with pytest.raises(Exception):
        await submit_photo(session, other, file_id="f2", digest="abc")


@pytest.mark.asyncio
async def test_reveal_requires_both_verified_and_consent(session):
    woman = await make_user(session, telegram_id=51, gender="female", nickname="Lina")
    man = await make_user(session, telegram_id=52, gender="male", nickname="David")
    woman.telegram_username = "lina_name"
    await find_partner(session, woman.id)
    await find_partner(session, man.id)
    await session.refresh(woman)
    with pytest.raises(NotVerified):
        await request_reveal(session, woman)
    woman.verification_status = "approved"
    man.verification_status = "approved"
    request = await request_reveal(session, woman)
    await answer_reveal(session, man, request.id, True)
    card = await public_card(session, woman, "en")
    assert "Lina" in card
    assert "lina_name" not in card
    prefs = await session.get(UserPreference, woman.id)
    prefs.allow_username_reveal = True
    card = await public_card(session, woman, "en")
    assert "lina_name" in card
    photo = await relay(session, woman, kind="photo", file_id="pic")
    assert photo.status == "delivered"


@pytest.mark.asyncio
async def test_star_payment_is_idempotent(session):
    user = await make_user(session, telegram_id=61, gender="female")
    package = await session.scalar(select(CoinPackage).where(CoinPackage.code == "small"))
    order = await create_coin_order(session, user, package)
    assert order.mode == "test"
    await confirm(session, order.id, charge_id="charge-1", amount=package.stars, currency="XTR")
    await confirm(session, order.id, charge_id="charge-1", amount=package.stars, currency="XTR")
    assert await balance(session, user.id) == package.coins
    paid = (
        await session.execute(
            select(func.count(WalletTransaction.id)).where(
                WalletTransaction.user_id == user.id,
                WalletTransaction.operation_type == COIN_PURCHASE,
            )
        )
    ).scalar_one()
    assert paid == 1
    await mark_refunded(session, order.id, "charge-1")
    assert await balance(session, user.id) == 0


@pytest.mark.asyncio
async def test_crypto_payment_is_idempotent(session, monkeypatch):
    monkeypatch.setenv("PAYMENT_MODE", "both")
    monkeypatch.setenv("CRYPTO_PAY_TOKEN", "test-token")
    monkeypatch.setenv("STAR_USD_CENTS", "2")
    get_settings.cache_clear()
    user = await make_user(session, telegram_id=62, gender="male")
    package = await session.scalar(select(CoinPackage).where(CoinPackage.code == "small"))
    order = await create_coin_order(session, user, package, channel="crypto")
    assert order.mode == "crypto"
    assert order.quote_amount == quote_for_stars(package.stars)[0] == "1.00"
    order.provider_invoice_id = "99"
    await confirm(session, order.id, charge_id="crypto:99", amount=package.stars, currency="USDT", quoted="1.00")
    await confirm(session, order.id, charge_id="crypto:99", amount=package.stars, currency="USDT", quoted="1.00")
    assert await balance(session, user.id) == package.coins
    paid = (
        await session.execute(
            select(func.count(WalletTransaction.id)).where(
                WalletTransaction.user_id == user.id,
                WalletTransaction.operation_type == COIN_PURCHASE,
            )
        )
    ).scalar_one()
    assert paid == 1
    with pytest.raises(Exception):
        await confirm(session, order.id, charge_id="crypto:100", amount=package.stars, currency="USDT", quoted="9.00")
    body = b'{"update_type":"invoice_paid"}'
    assert signature_for("test-token", body) == signature_for("test-token", body)
    assert signature_for("test-token", body) != signature_for("other", body)
    monkeypatch.undo()
    get_settings.cache_clear()


def test_daily_board_changes_each_day():
    first = daily_codes(datetime(2026, 10, 18, tzinfo=timezone.utc))
    second = daily_codes(datetime(2026, 10, 19, tzinfo=timezone.utc))
    assert len(first) == 3 and len(set(first)) == 3
    assert first != second
    assert daily_codes(datetime(2026, 10, 18, 23, tzinfo=timezone.utc)) == first


@pytest.mark.asyncio
async def test_daily_task_can_be_claimed_again_next_day(session):
    from database.models import DailyTask

    user = await make_user(session, telegram_id=81, gender="female")
    today = utcnow().date()
    task = await session.scalar(select(DailyTask).where(DailyTask.code == "answer_qotd"))
    assert task is not None
    session.add(QotdAnswer(user_id=user.id, question_key="qotd.1", answered_on=today))
    session.add(TaskCompletion(user_id=user.id, task_id=task.id, completed_on=today - timedelta(days=1)))
    await session.flush()
    amount = await claim_task(session, user, "answer_qotd")
    assert amount == task.reward
    with pytest.raises(Exception):
        await claim_task(session, user, "answer_qotd")


@pytest.mark.asyncio
async def test_vip_does_not_bypass_restrictions(session):
    user = await make_user(session, telegram_id=71, gender="male", age=19)
    plan = await session.scalar(select(VipPlan).where(VipPlan.code == "week"))
    await grant(session, user.id, plan, source="test", idempotency_key="vip-test-1", charge_coins=False)
    assert await is_vip(session, user.id)
    user.status = "restricted"
    user.restriction_until = utcnow() + timedelta(hours=2)
    result = await find_partner(session, user.id)
    assert result.reason == "restricted"
    user.verification_status = "unverified"
    woman = await make_user(session, telegram_id=72, gender="female")
    await find_partner(session, woman.id)
    user.status = "active"
    user.restriction_until = None
    await find_partner(session, user.id)
    await session.refresh(user)
    blocked = await relay(session, user, kind="photo", file_id="x")
    assert blocked.code == "verification_required"
    rows = (await session.execute(select(Subscription).where(Subscription.user_id == user.id))).scalars().all()
    assert rows


def test_init_data_rejects_client_supplied_id_without_signature():
    with pytest.raises(Exception):
        validate_init_data("user=%7B%22id%22%3A1%7D&auth_date=1&hash=deadbeef", "token", 10**9)
    import json
    import time
    import urllib.parse
    import hmac
    import hashlib

    user = json.dumps({"id": 99, "username": "a"}, separators=(",", ":"))
    auth_date = str(int(time.time()))
    pairs = {"user": user, "auth_date": auth_date, "query_id": "q"}
    data_check = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    secret = hmac.new(b"WebAppData", b"token", hashlib.sha256).digest()
    digest = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    init_data = urllib.parse.urlencode({**pairs, "hash": digest})
    parsed = validate_init_data(init_data, "token", 3600)
    assert parsed["id"] == 99
    body = b'{"operation_id":"op1","status":"approved"}'
    secret_key = "provider-secret"
    signature = sign_provider_body(secret_key, body)
    assert verify_provider_signature(secret_key, body, signature)
    assert not verify_provider_signature(secret_key, body, "00")
    assert get_settings().payment_mode == "test"


def test_production_rejects_test_payments(monkeypatch):
    from core.config import Settings, validate_runtime

    settings = Settings(
        environment="production",
        payment_mode="test",
        admin_jwt_secret="x" * 32,
        identity_pepper="pepper-pepper-pepper",
        database_url="postgresql+asyncpg://user:strong-pass@db/love",
    )
    with pytest.raises(RuntimeError):
        validate_runtime(settings)
