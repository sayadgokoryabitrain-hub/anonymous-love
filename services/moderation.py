from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.clock import utcnow
from core.errors import AppError
from core.i18n import t
from database.models import Appeal, ChatMessage, ChatSession, Report, User, UserBlock
from database.repositories.users import get_user
from services.chat import end_session
from services.matching import load_session_partner
from services.notify import enqueue
from services.settings_store import get_int_setting
from services.trust import refresh_trust


async def block_user(session: AsyncSession, blocker: User, blocked_id: int) -> None:
    if blocker.id == blocked_id:
        raise AppError("bad_block", "errors.generic")
    existing = await session.scalar(
        select(UserBlock).where(UserBlock.blocker_id == blocker.id, UserBlock.blocked_id == blocked_id)
    )
    if existing is None:
        session.add(UserBlock(blocker_id=blocker.id, blocked_id=blocked_id))
    if blocker.active_session_id:
        partner = await load_session_partner(session, blocker.id, blocker.active_session_id)
        if partner and partner.id == blocked_id:
            await end_session(session, blocker.active_session_id, blocker.id, "block")
            await enqueue(session, partner, t(partner.language, "chat.partner_blocked"))
    await enqueue(session, blocker, t(blocker.language, "chat.blocked_done"))


async def create_report(
    session: AsyncSession,
    reporter: User,
    *,
    reason: str,
    details: str | None = None,
) -> Report:
    if reason not in {"spam", "abuse", "scam", "safety", "other"}:
        raise AppError("bad_reason", "errors.generic")
    if not reporter.active_session_id:
        raise AppError("no_chat", "chat.not_in_chat")
    partner = await load_session_partner(session, reporter.id, reporter.active_session_id)
    if partner is None:
        raise AppError("no_chat", "chat.not_in_chat")
    limit = await get_int_setting(session, "report_daily_limit")
    start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    count = await session.scalar(
        select(func.count(Report.id)).where(Report.reporter_id == reporter.id, Report.created_at >= start)
    )
    if count and int(count) >= limit:
        raise AppError("report_limit", "report.limit")
    message = await session.scalar(
        select(ChatMessage)
        .where(ChatMessage.session_id == reporter.active_session_id, ChatMessage.sender_id == partner.id)
        .order_by(ChatMessage.id.desc())
        .limit(1)
    )
    snapshot = None
    message_id = None
    if message:
        message.retained_for_report = True
        message_id = message.id
        snapshot = message.text_excerpt or f"[{message.message_type}]"
    report = Report(
        reporter_id=reporter.id,
        target_user_id=partner.id,
        session_id=reporter.active_session_id,
        message_id=message_id,
        reason=reason,
        details=(details or "")[:1000] or None,
        snapshot=snapshot,
        status="open",
    )
    session.add(report)
    await session.flush()
    return report


async def submit_appeal(session: AsyncSession, user: User, text: str) -> Appeal:
    cleaned = (text or "").strip()
    if len(cleaned) < 10:
        raise AppError("bad_appeal", "errors.generic")
    appeal = Appeal(user_id=user.id, text=cleaned[:2000], status="open")
    session.add(appeal)
    await session.flush()
    return appeal


async def warn_user(session: AsyncSession, user: User, reason: str) -> None:
    user.warning_count = (user.warning_count or 0) + 1
    refresh_trust(user)
    await enqueue(session, user, t(user.language, "restricted.notice", reason=reason[:300]))


async def restrict_user(session: AsyncSession, user: User, hours: int, reason: str) -> None:
    if hours < 1 or hours > 24 * 30:
        raise AppError("bad_hours", "errors.generic")
    user.status = "restricted"
    user.restriction_until = utcnow() + timedelta(hours=hours)
    user.restriction_reason = reason[:300]
    if user.active_session_id:
        await end_session(session, user.active_session_id, user.id, "ban")
    refresh_trust(user)
    await enqueue(session, user, t(user.language, "restricted.notice", reason=reason[:300]))


async def ban_user(session: AsyncSession, user: User, reason: str) -> None:
    user.status = "banned"
    user.restriction_reason = reason[:300]
    user.confirmed_violations = (user.confirmed_violations or 0) + 1
    if user.presence_state == "searching":
        user.presence_state = "idle"
        user.search_started_at = None
    if user.active_session_id:
        await end_session(session, user.active_session_id, user.id, "ban")
    refresh_trust(user)
    await enqueue(session, user, t(user.language, "errors.banned", reason=reason[:300]))


async def unban_user(session: AsyncSession, user: User) -> None:
    if user.underage_blocked:
        user.status = "restricted"
        user.restriction_reason = "underage"
    else:
        user.status = "active"
        user.restriction_until = None
        user.restriction_reason = None
    refresh_trust(user)


async def resolve_appeal(session: AsyncSession, appeal: Appeal, accept: bool, admin_id: int) -> None:
    appeal.status = "accepted" if accept else "rejected"
    appeal.admin_id = admin_id
    appeal.resolved_at = utcnow()
    if accept:
        user = await get_user(session, appeal.user_id)
        if user:
            await unban_user(session, user)


async def active_chats(session: AsyncSession) -> list[ChatSession]:
    rows = await session.execute(select(ChatSession).where(ChatSession.status == "active").order_by(ChatSession.started_at.desc()))
    return list(rows.scalars().all())
