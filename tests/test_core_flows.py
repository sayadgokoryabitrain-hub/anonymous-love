from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from core.content_policy import evaluate_media, evaluate_text
from core.errors import AppError, InsufficientFunds
from core.i18n import t
from database.base import Base
from database.models import ChatSession, User, UserBlock, UserPreference, WalletTransaction
from services.accounts import (
    accept_rules,
    claim_daily,
    complete_registration,
    confirm_age,
    confirm_underage,
    delete_account,
    finish_interests,
    open_user,
    set_age,
    set_age_range,
    set_bio,
    set_gender,
    set_language,
    set_nickname,
    set_place,
    set_search_prefs,
    toggle_interest,
)
from services.chat import end_session, relay
from services.matching import compatibility_score, find_partner
from services.seed import seed_data
from services.wallet import ADMIN_ADJUSTMENT, apply
from tests.helpers import balance, make_user


def test_score_is_transparent_and_not_a_promise():
    score, shared = compatibility_score(
        interests_a={"music", "travel"},
        interests_b={"music", "games"},
        language_a="ru",
        language_b="ru",
        age_a=24,
        age_b=26,
        country_a="AM",
        country_b="AM",
        city_a="Yerevan",
        city_b="Yerevan",
        use_history=False,
    )
    assert shared == ["music"]
    assert score == 10 + 20 + 13 + 8


def test_media_and_links_require_verification():
    blocked = evaluate_text("see t.me/someone", sender_verified=False, recipient_verified=False, username_allowed=False)
    assert blocked.code == "verification_required"
    allowed = evaluate_text("see https://example.com/a", sender_verified=True, recipient_verified=True, username_allowed=True)
    assert allowed.allowed
    dangerous = evaluate_text("javascript:alert(1)", sender_verified=True, recipient_verified=True, username_allowed=True)
    assert dangerous.code == "dangerous_scheme"
    photo = evaluate_media("photo", sender_verified=False, recipient_verified=True)
    assert photo.code == "verification_required"
    sticker = evaluate_media("sticker", sender_verified=False, recipient_verified=False)
    assert sticker.allowed
    safety = evaluate_text("child porn link", sender_verified=True, recipient_verified=True, username_allowed=True)
    assert safety.code == "child_safety"


@pytest.mark.asyncio
async def test_registration_age_gate_and_bonus_once(session):
    user = await open_user(session, 1001, "ann", "NOPE")
    with pytest.raises(AppError):
        await set_age(session, user, 17)
    await confirm_age(session, user, False)
    await confirm_underage(session, user)
    assert user.underage_blocked is True
    denied = await find_partner(session, user.id)
    assert denied.reason == "underage"

    adult = await open_user(session, 1002, None, None)
    await confirm_age(session, adult, True)
    await accept_rules(session, adult, True)
    await set_language(session, adult, "hy")
    await set_gender(session, adult, "female")
    await set_age(session, adult, 24)
    await set_place(session, adult, "country", None)
    await set_place(session, adult, "city", "Yerevan")
    from database.models import Interest

    interests = (await session.execute(select(Interest).limit(3))).scalars().all()
    for item in interests:
        await toggle_interest(session, adult, item.id)
    await finish_interests(session, adult)
    await set_nickname(session, adult, "Nare")
    await set_bio(session, adult, "Люблю музыку и путешествия")
    await set_age_range(session, adult, 20, 32)
    granted = 0
    await set_search_prefs(session, adult, "any")
    assert adult.registration_completed is True
    assert await balance(session, adult.id) == 100
    await complete_registration(session, adult)
    assert await balance(session, adult.id) == 100
    assert granted == 0


@pytest.mark.asyncio
async def test_opposite_gender_match_and_filters(session):
    woman = await make_user(session, telegram_id=1, gender="female", age=24, interests=["music", "travel"])
    man = await make_user(session, telegram_id=2, gender="male", age=26, interests=["music", "games"])
    other_man = await make_user(session, telegram_id=3, gender="male", age=27)
    assert (await find_partner(session, man.id)).status == "queued"
    assert (await find_partner(session, other_man.id)).status == "queued"
    matched = await find_partner(session, woman.id)
    assert matched.status == "found"
    await session.refresh(woman)
    await session.refresh(man)
    assert woman.active_session_id == man.active_session_id == matched.session_id
    chat = await session.get(ChatSession, matched.session_id)
    assert chat.shared_interests == "music"

    await end_session(session, matched.session_id, man.id, "stop")
    await session.refresh(woman)
    await session.refresh(man)
    await session.refresh(other_man)
    assert woman.presence_state == "idle"
    assert man.active_session_id is None
    other_man.presence_state = "idle"
    other_man.search_started_at = None
    await session.flush()

    await find_partner(session, woman.id)
    again = await find_partner(session, man.id)
    assert again.status == "queued"


@pytest.mark.asyncio
async def test_block_and_age_window_prevent_match(session):
    woman = await make_user(session, telegram_id=11, gender="female", age=42)
    man = await make_user(session, telegram_id=12, gender="male", age=22)
    prefs = await session.get(UserPreference, man.id)
    prefs.age_max = 30
    await find_partner(session, woman.id)
    missed = await find_partner(session, man.id)
    assert missed.status == "queued"

    woman.presence_state = "idle"
    man.presence_state = "idle"
    prefs.age_max = 50
    session.add(UserBlock(blocker_id=woman.id, blocked_id=man.id))
    await session.flush()
    await find_partner(session, woman.id)
    blocked = await find_partner(session, man.id)
    assert blocked.status == "queued"


@pytest.mark.asyncio
async def test_chat_routing_and_closed_media(session):
    woman = await make_user(session, telegram_id=21, gender="female")
    man = await make_user(session, telegram_id=22, gender="male")
    await find_partner(session, woman.id)
    await find_partner(session, man.id)
    await session.refresh(man)
    delivered = await relay(session, man, kind="text", text="Привет")
    assert delivered.status == "delivered"
    photo = await relay(session, man, kind="photo", file_id="file-1")
    assert photo.code == "verification_required"
    link = await relay(session, man, kind="text", text="мой @secretname")
    assert link.code == "verification_required"
    man.verification_status = "approved"
    woman.verification_status = "approved"
    from database.models import UserPreference

    prefs = await session.get(UserPreference, man.id)
    prefs.allow_username_reveal = True
    opened = await relay(session, man, kind="text", text="мой @secretname")
    assert opened.status == "delivered"
    safety = await relay(session, man, kind="text", text="this is child porn")
    assert safety.code == "child_safety"
    await session.refresh(man)
    assert man.status == "restricted"


@pytest.mark.asyncio
async def test_wallet_idempotency_and_non_negative(session):
    user = await make_user(session, telegram_id=31, gender="male")
    first = await apply(session, user.id, 100, ADMIN_ADJUSTMENT, source="test", idempotency_key="k1", actor="admin:1")
    second = await apply(session, user.id, 100, ADMIN_ADJUSTMENT, source="test", idempotency_key="k1", actor="admin:1")
    assert first.id == second.id
    await apply(session, user.id, -40, ADMIN_ADJUSTMENT, source="test", idempotency_key="k2", actor="admin:1")
    assert await balance(session, user.id) == 60
    with pytest.raises(InsufficientFunds):
        await apply(session, user.id, -1000, ADMIN_ADJUSTMENT, source="test", idempotency_key="k3", actor="admin:1")
    assert await balance(session, user.id) == 60
    count = await session.scalar(select(func.count(WalletTransaction.id)).where(WalletTransaction.user_id == user.id))
    assert count == 2


@pytest.mark.asyncio
async def test_daily_bonus_once(session):
    user = await make_user(session, telegram_id=32, gender="female")
    created, amount = await claim_daily(session, user)
    assert created is True and amount == 20
    created_again, _ = await claim_daily(session, user)
    assert created_again is False
    assert await balance(session, user.id) == 20


@pytest.mark.asyncio
async def test_delete_keeps_ledger_and_blocks_second_bonus(session, factory):
    user = await open_user(session, 777, "nick", None)
    await confirm_age(session, user, True)
    await accept_rules(session, user, True)
    await set_language(session, user, "en")
    await set_gender(session, user, "male")
    await set_age(session, user, 30)
    await set_place(session, user, "country", "AM")
    await set_place(session, user, "city", None)
    from database.models import Interest

    for item in (await session.execute(select(Interest).limit(3))).scalars().all():
        await toggle_interest(session, user, item.id)
    await finish_interests(session, user)
    await set_nickname(session, user, "Armen")
    await set_bio(session, user, "Hello there friend")
    await set_age_range(session, user, 18, 40)
    await set_search_prefs(session, user, "any")
    user_id = user.id
    assert await balance(session, user_id) == 100
    await delete_account(session, user)
    await session.commit()
    async with factory() as db:
        deleted = await db.get(User, user_id)
        assert deleted.telegram_id is None
        assert deleted.telegram_id_hash
        assert deleted.status == "deleted"
        from database.models import Profile

        profile = await db.get(Profile, user_id)
        assert profile.nickname is None
        assert await balance(db, user_id) == 100
        restored = await open_user(db, 777, None, None)
        assert restored.id == user_id
        assert restored.registration_bonus_granted is True
        await db.commit()


@pytest.mark.asyncio
async def test_parallel_search_single_session(tmp_path):
    path = tmp_path / "race.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{path}",
        poolclass=NullPool,
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        await seed_data(session)
        woman = await make_user(session, telegram_id=501, gender="female")
        man_a = await make_user(session, telegram_id=502, gender="male")
        man_b = await make_user(session, telegram_id=503, gender="male")
        await find_partner(session, woman.id)
        await session.commit()
        ids = (man_a.id, man_b.id)

    async def search(user_id: int):
        async with maker() as db:
            result = await find_partner(db, user_id)
            await db.commit()
            return result.status

    statuses = await asyncio.gather(search(ids[0]), search(ids[1]))
    async with maker() as db:
        active = await db.scalar(select(func.count(ChatSession.id)).where(ChatSession.status == "active"))
        searching = await db.scalar(select(func.count(User.id)).where(User.presence_state == "searching"))
    await engine.dispose()
    assert statuses.count("found") == 1
    assert active == 1
    assert searching == 1
