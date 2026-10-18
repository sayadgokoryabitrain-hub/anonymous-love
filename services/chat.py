from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.clock import as_utc, utcnow
from core.content_policy import evaluate_media, evaluate_text
from core.i18n import t
from core.rate_limit import get_limiter
from database.models import ChatMessage, ChatParticipant, ChatSession, Report, User
from database.repositories.users import get_prefs, get_profile, get_user
from services.matching import load_session_partner
from services.notify import enqueue
from services.settings_store import get_int_setting
from services.trust import refresh_trust
from services.wallet import FIRST_CHAT_BONUS, apply


@dataclass
class RelayResult:
    status: str
    code: str = ""


async def _active_chat(session: AsyncSession, user: User) -> ChatSession | None:
    if user.presence_state != "in_chat" or not user.active_session_id:
        return None
    chat = await session.get(ChatSession, user.active_session_id)
    if chat is None or chat.status != "active":
        return None
    return chat


async def relay(
    session: AsyncSession,
    sender: User,
    *,
    kind: str,
    text: str | None = None,
    file_id: str | None = None,
    telegram_message_id: int | None = None,
    file_size: int | None = None,
) -> RelayResult:
    if sender.status == "banned":
        return RelayResult("denied", "banned")
    chat = await _active_chat(session, sender)
    if chat is None:
        return RelayResult("no_chat")
    partner = await load_session_partner(session, sender.id, chat.id)
    if partner is None or not partner.telegram_id:
        return RelayResult("no_chat")
    if not await get_limiter().allow(f"msg:{sender.id}", 20, 60):
        return RelayResult("denied", "rate")
    sender_verified = sender.verification_status == "approved"
    partner_verified = partner.verification_status == "approved"
    prefs = await get_prefs(session, sender.id)
    username_allowed = bool(prefs and prefs.allow_username_reveal)
    if kind == "text":
        decision = evaluate_text(
            text or "",
            sender_verified=sender_verified,
            recipient_verified=partner_verified,
            username_allowed=username_allowed,
        )
    else:
        decision = evaluate_media(kind, sender_verified=sender_verified, recipient_verified=partner_verified)
    if not decision.allowed and decision.code == "child_safety":
        await _safety_stop(session, sender, partner, chat)
        return RelayResult("blocked", "child_safety")
    if not decision.allowed:
        return RelayResult("blocked", decision.code or "verification_required")
    if kind == "photo":
        limit = await get_int_setting(session, "photo_limit_per_10min")
        if file_size and file_size > 5 * 1024 * 1024:
            return RelayResult("blocked", "file_size")
        if not await get_limiter().allow(f"photo:{chat.id}:{sender.id}", limit, 600):
            return RelayResult("blocked", "photo_limit")
    excerpt = None
    if kind == "text" and text:
        excerpt = text[:400]
    elif decision.code == "child_safety":
        excerpt = None
    session.add(
        ChatMessage(
            session_id=chat.id,
            sender_id=sender.id,
            message_type=kind,
            text_excerpt=excerpt,
            telegram_message_id=telegram_message_id,
        )
    )
    await session.execute(
        update(ChatParticipant)
        .where(ChatParticipant.session_id == chat.id, ChatParticipant.user_id == sender.id)
        .values(messages_sent=ChatParticipant.messages_sent + 1)
    )
    chat.last_activity_at = utcnow()
    profile = await get_profile(session, sender.id)
    nickname = profile.nickname if profile and profile.nickname else "—"
    if kind == "text":
        body = f"{nickname}: {text}"
        if decision.scam_hint:
            body = t(partner.language, "scam.warn") + "\n" + body
        await enqueue(session, partner, body[:4000])
    elif kind == "sticker" and file_id:
        await enqueue(session, partner, nickname, template="sticker", payload={"file_id": file_id})
    elif kind == "photo" and file_id:
        rules = t(partner.language, "media.rules")
        await enqueue(
            session,
            partner,
            rules,
            template="photo",
            payload={"file_id": file_id, "caption": nickname},
        )
    else:
        await enqueue(session, partner, f"{nickname}", template=kind, payload={"file_id": file_id or ""})
    return RelayResult("delivered", "scam" if decision.scam_hint else "")


async def _safety_stop(session: AsyncSession, sender: User, partner: User, chat: ChatSession) -> None:
    from datetime import timedelta

    sender.status = "restricted"
    sender.restriction_until = utcnow() + timedelta(hours=24)
    sender.restriction_reason = "child_safety_review"
    refresh_trust(sender)
    session.add(
        Report(
            reporter_id=sender.id,
            target_user_id=sender.id,
            session_id=chat.id,
            reason="child_safety",
            snapshot="[blocked-safety]",
            status="open",
        )
    )
    await end_session(session, chat.id, sender.id, "safety")
    await enqueue(session, sender, t(sender.language, "safety.blocked"))
    await enqueue(session, partner, t(partner.language, "safety.partner"))


async def end_session(session: AsyncSession, session_id: str, user_id: int | None, reason: str) -> bool:
    now = utcnow()
    result = await session.execute(
        update(ChatSession)
        .where(ChatSession.id == session_id, ChatSession.status == "active")
        .values(status="ended", ended_at=now, ended_by_user_id=user_id, end_reason=reason[:32])
    )
    if result.rowcount != 1:
        return False
    chat = await session.get(ChatSession, session_id)
    participants = list(
        (
            await session.execute(select(ChatParticipant).where(ChatParticipant.session_id == session_id))
        ).scalars().all()
    )
    await session.execute(
        update(User)
        .where(User.active_session_id == session_id)
        .values(presence_state="idle", active_session_id=None, search_started_at=None)
    )
    await session.execute(
        update(ChatParticipant).where(ChatParticipant.session_id == session_id).values(left_at=now)
    )
    if chat is None:
        return True
    min_seconds = await get_int_setting(session, "min_chat_seconds_for_bonus")
    bonus = await get_int_setting(session, "first_chat_bonus")
    started = as_utc(chat.started_at) or now
    activity = as_utc(chat.last_activity_at) or started
    span = (activity - started).total_seconds()
    talked = len(participants) == 2 and all(item.messages_sent >= 1 for item in participants)
    for item in participants:
        member = await get_user(session, item.user_id)
        if member is None:
            continue
        if talked and span >= min_seconds and not member.first_chat_bonus_granted and bonus > 0:
            await apply(
                session,
                member.id,
                bonus,
                FIRST_CHAT_BONUS,
                source="chat",
                idempotency_key=f"first_chat:{member.id}",
                meta={"session_id": session_id},
            )
            member.first_chat_bonus_granted = True
        if reason not in {"safety", "ban", "delete"}:
            await enqueue(
                session,
                member,
                t(member.language, "rematch.prompt"),
                template="rematch",
                payload={"session_id": session_id},
            )
        elif reason == "delete" and member.id != user_id:
            await enqueue(session, member, t(member.language, "chat.partner_left"))
    return True


async def common_interest_text(session: AsyncSession, user: User) -> str:
    chat = await _active_chat(session, user)
    if chat is None:
        return t(user.language, "chat.not_in_chat")
    if not chat.shared_interests:
        return t(user.language, "chat.no_common")
    labels = [t(user.language, f"interest.{key}") for key in chat.shared_interests.split(",") if key]
    return t(user.language, "chat.interests_body", shared=", ".join(labels), score=chat.compatibility_score)
