from __future__ import annotations

from datetime import date, datetime, time, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.clock import as_utc, utcnow
from core.errors import AppError
from core.i18n import t
from database.models import (
    ChatMessage,
    ChatParticipant,
    DailyTask,
    GameAnswer,
    Gift,
    GiftTransaction,
    PromoCode,
    PromoRedemption,
    QotdAnswer,
    Referral,
    TaskCompletion,
    User,
)
from database.repositories.users import get_prefs, get_profile, get_user, interest_keys
from services.matching import load_session_partner
from services.notify import enqueue
from services.settings_store import get_int_setting
from services.vip import is_vip
from services.wallet import GAME_BONUS, GIFT_PURCHASE, PROMO, TASK_REWARD, apply, get_transaction

QUESTIONS = ("q1", "q2", "q3")
ONCE_DATE = date(1970, 1, 1)
DAILY_POOL = (
    "start_chat",
    "answer_qotd",
    "three_messages",
    "play_game",
    "search_today",
    "send_gift",
)
ONCE_CODES = ("complete_profile", "five_interests", "invite_friend", "verify")


def daily_codes(moment: datetime | None = None) -> list[str]:
    day = (moment or utcnow()).date()
    start = day.toordinal() % len(DAILY_POOL)
    return [DAILY_POOL[(start + offset) % len(DAILY_POOL)] for offset in range(3)]


def _day_start(moment: datetime | None = None) -> tuple[datetime, date]:
    current = moment or utcnow()
    day = current.date()
    return datetime.combine(day, time.min, tzinfo=timezone.utc), day


async def send_gift(session: AsyncSession, sender: User, code: str) -> GiftTransaction:
    if not sender.active_session_id:
        raise AppError("no_chat", "chat.not_in_chat")
    gift = await session.scalar(select(Gift).where(Gift.code == code, Gift.active.is_(True)))
    if gift is None:
        raise AppError("missing_gift", "errors.not_found")
    if gift.vip_only and not await is_vip(session, sender.id):
        raise AppError("vip_only", "gifts.vip_only")
    partner = await load_session_partner(session, sender.id, sender.active_session_id)
    if partner is None:
        raise AppError("no_chat", "chat.not_in_chat")
    prefs = await get_prefs(session, partner.id)
    if prefs and not prefs.allow_gifts:
        raise AppError("gifts_off", "gifts.off")
    profile = await get_profile(session, partner.id)
    tx = await apply(
        session,
        sender.id,
        -gift.price,
        GIFT_PURCHASE,
        source=f"gift:{gift.code}",
        idempotency_key=f"gift:{sender.id}:{partner.id}:{gift.id}:{utcnow().timestamp()}",
        meta={"gift": gift.code, "to": partner.id},
    )
    row = GiftTransaction(
        gift_id=gift.id,
        sender_id=sender.id,
        recipient_id=partner.id,
        session_id=sender.active_session_id,
        wallet_transaction_id=tx.id,
    )
    session.add(row)
    show = profile is None or profile.show_gifts
    body = t(partner.language, "gifts.received", emoji=gift.emoji if show else "🎁")
    await enqueue(session, partner, body)
    await session.flush()
    return row


async def task_ready(session: AsyncSession, user: User, task: DailyTask) -> bool:
    if task.code == "complete_profile":
        return user.registration_completed
    if task.code == "five_interests":
        return len(await interest_keys(session, user.id)) >= 5
    if task.code == "start_chat":
        return await _messages_today(session, user.id) >= 1
    if task.code == "three_messages":
        return await _messages_today(session, user.id) >= 3
    if task.code == "play_game":
        return await _game_answers_today(session, user.id) >= 3
    if task.code == "search_today":
        return await _searched_today(session, user)
    if task.code == "send_gift":
        return await _gift_sent_today(session, user.id)
    if task.code == "answer_qotd":
        today = utcnow().date()
        found = await session.scalar(
            select(QotdAnswer.id).where(QotdAnswer.user_id == user.id, QotdAnswer.answered_on == today)
        )
        return found is not None
    if task.code == "invite_friend":
        found = await session.scalar(
            select(Referral.id).where(Referral.inviter_id == user.id, Referral.status == "rewarded")
        )
        return found is not None
    if task.code == "verify":
        return user.verification_status == "approved"
    return False


async def claim_task(session: AsyncSession, user: User, code: str) -> int:
    task = await session.scalar(select(DailyTask).where(DailyTask.code == code, DailyTask.active.is_(True)))
    if task is None:
        raise AppError("missing_task", "errors.not_found")
    if not await task_ready(session, user, task):
        raise AppError("not_ready", "tasks.not_ready")
    completed_on = utcnow().date() if task.repeatable else ONCE_DATE
    existing = await session.scalar(
        select(TaskCompletion).where(
            TaskCompletion.user_id == user.id,
            TaskCompletion.task_id == task.id,
            TaskCompletion.completed_on == completed_on,
        )
    )
    if existing:
        raise AppError("already", "tasks.done")
    tx = await apply(
        session,
        user.id,
        task.reward,
        TASK_REWARD,
        source=f"task:{task.code}",
        idempotency_key=f"task:{user.id}:{task.code}:{completed_on.isoformat()}",
    )
    session.add(
        TaskCompletion(user_id=user.id, task_id=task.id, completed_on=completed_on, transaction_id=tx.id)
    )
    await session.flush()
    return task.reward


async def answer_game(session: AsyncSession, user: User, question_key: str, answer: str) -> str:
    if question_key not in QUESTIONS or answer not in {"a", "b", "c"}:
        raise AppError("bad_answer", "errors.generic")
    if not user.active_session_id:
        raise AppError("no_chat", "chat.not_in_chat")
    existing = await session.scalar(
        select(GameAnswer).where(
            GameAnswer.session_id == user.active_session_id,
            GameAnswer.user_id == user.id,
            GameAnswer.question_key == question_key,
        )
    )
    if existing is None:
        session.add(
            GameAnswer(
                session_id=user.active_session_id,
                user_id=user.id,
                question_key=question_key,
                answer=answer,
            )
        )
        await session.flush()
    partner = await load_session_partner(session, user.id, user.active_session_id)
    if partner is None:
        return "saved"
    other = await session.scalar(
        select(GameAnswer).where(
            GameAnswer.session_id == user.active_session_id,
            GameAnswer.user_id == partner.id,
            GameAnswer.question_key == question_key,
        )
    )
    if other is None:
        await enqueue(session, user, t(user.language, "game.wait"))
        return "wait"
    mine = existing.answer if existing else answer
    same = mine == other.answer
    for member, member_answer in ((user, mine), (partner, other.answer)):
        label = t(member.language, f"game.{question_key}{member_answer}")
        text = t(member.language, "game.match" if same else "game.differ", answer=label)
        await enqueue(session, member, text)
    await _maybe_game_bonus(session, user.active_session_id)
    return "compared"


async def _maybe_game_bonus(session: AsyncSession, session_id: str) -> None:
    rows = (await session.execute(select(GameAnswer).where(GameAnswer.session_id == session_id))).scalars().all()
    by_user: dict[int, dict[str, str]] = {}
    for row in rows:
        by_user.setdefault(row.user_id, {})[row.question_key] = row.answer
    if len(by_user) != 2:
        return
    left, right = list(by_user.values())
    matches = sum(1 for key in QUESTIONS if left.get(key) and left.get(key) == right.get(key))
    if matches < 2:
        return
    amount = await get_int_setting(session, "game_bonus")
    for user_id in by_user:
        member = await get_user(session, user_id)
        if member is None:
            continue
        key = f"game:{session_id}:{user_id}"
        if await get_transaction(session, key):
            continue
        await apply(
            session,
            user_id,
            amount,
            GAME_BONUS,
            source="game",
            idempotency_key=key,
        )
        await enqueue(session, member, t(member.language, "game.bonus", amount=amount))


async def redeem_promo(session: AsyncSession, user: User, code: str) -> int:
    cleaned = (code or "").strip().upper()
    promo = await session.scalar(select(PromoCode).where(func.upper(PromoCode.code) == cleaned))
    if promo is None or not promo.active:
        raise AppError("bad_promo", "promo.bad")
    expires = as_utc(promo.expires_at)
    if expires and expires < utcnow():
        raise AppError("bad_promo", "promo.bad")
    if promo.redemptions_count >= promo.max_redemptions:
        raise AppError("bad_promo", "promo.bad")
    used = await session.scalar(
        select(PromoRedemption).where(PromoRedemption.promo_id == promo.id, PromoRedemption.user_id == user.id)
    )
    if used:
        raise AppError("bad_promo", "promo.bad")
    result = await session.execute(
        select(PromoCode)
        .where(PromoCode.id == promo.id, PromoCode.redemptions_count < PromoCode.max_redemptions)
    )
    current = result.scalar_one_or_none()
    if current is None:
        raise AppError("bad_promo", "promo.bad")
    current.redemptions_count += 1
    tx = await apply(
        session,
        user.id,
        current.coins,
        PROMO,
        source=f"promo:{current.code}",
        idempotency_key=f"promo:{current.id}:{user.id}",
    )
    session.add(PromoRedemption(promo_id=current.id, user_id=user.id, transaction_id=tx.id))
    await session.flush()
    return current.coins


async def list_gifts(session: AsyncSession) -> list[Gift]:
    rows = await session.execute(select(Gift).where(Gift.active.is_(True)).order_by(Gift.sort_order))
    return list(rows.scalars().all())


async def list_tasks(session: AsyncSession) -> list[DailyTask]:
    rows = await session.execute(select(DailyTask).where(DailyTask.active.is_(True)).order_by(DailyTask.sort_order))
    return list(rows.scalars().all())


async def task_claimed(session: AsyncSession, user: User, task: DailyTask) -> bool:
    completed_on = utcnow().date() if task.repeatable else ONCE_DATE
    existing = await session.scalar(
        select(TaskCompletion.id).where(
            TaskCompletion.user_id == user.id,
            TaskCompletion.task_id == task.id,
            TaskCompletion.completed_on == completed_on,
        )
    )
    return existing is not None


async def _messages_today(session: AsyncSession, user_id: int) -> int:
    start, _day = _day_start()
    count = await session.scalar(
        select(func.count(ChatMessage.id)).where(ChatMessage.sender_id == user_id, ChatMessage.created_at >= start)
    )
    return int(count or 0)


async def _game_answers_today(session: AsyncSession, user_id: int) -> int:
    start, _day = _day_start()
    rows = (
        await session.execute(
            select(GameAnswer.question_key).where(GameAnswer.user_id == user_id, GameAnswer.created_at >= start)
        )
    ).all()
    return len({key for (key,) in rows})


async def _searched_today(session: AsyncSession, user: User) -> bool:
    start, day = _day_start()
    started = as_utc(user.search_started_at)
    if user.presence_state == "searching" or (started and started.date() == day):
        return True
    joined = await session.scalar(
        select(ChatParticipant.id).where(ChatParticipant.user_id == user.id, ChatParticipant.joined_at >= start).limit(1)
    )
    return joined is not None


async def _gift_sent_today(session: AsyncSession, user_id: int) -> bool:
    start, _day = _day_start()
    sent = await session.scalar(
        select(GiftTransaction.id).where(GiftTransaction.sender_id == user_id, GiftTransaction.created_at >= start).limit(1)
    )
    return sent is not None
