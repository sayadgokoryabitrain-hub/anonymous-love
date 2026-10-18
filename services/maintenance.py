from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.clock import utcnow
from database.models import ChatMessage, ChatSession, ErrorLog, User, VerificationRequest
from services.chat import end_session
from services.settings_store import get_int_setting


async def log_error(session: AsyncSession, source: str, message: str, details: dict | None = None) -> None:
    session.add(ErrorLog(source=source[:64], message=message[:500], details=details))


async def run_maintenance(session: AsyncSession) -> dict[str, int]:
    now = utcnow()
    search_timeout = await get_int_setting(session, "search_timeout_sec")
    idle_timeout = await get_int_setting(session, "chat_idle_timeout_sec")
    retention_days = await get_int_setting(session, "message_retention_days")
    search_cutoff = now - timedelta(seconds=search_timeout)
    expired_search = await session.execute(
        update(User)
        .where(User.presence_state == "searching", User.search_started_at.is_not(None), User.search_started_at < search_cutoff)
        .values(presence_state="idle", search_started_at=None)
    )
    idle_cutoff = now - timedelta(seconds=idle_timeout)
    stale = (
        await session.execute(
            select(ChatSession.id).where(ChatSession.status == "active", ChatSession.last_activity_at < idle_cutoff)
        )
    ).scalars().all()
    closed = 0
    for session_id in stale:
        if await end_session(session, session_id, None, "idle"):
            closed += 1
    message_cutoff = now - timedelta(days=retention_days)
    removed = await session.execute(
        delete(ChatMessage).where(
            ChatMessage.created_at < message_cutoff,
            ChatMessage.retained_for_report.is_(False),
        )
    )
    await session.execute(
        update(VerificationRequest)
        .where(
            VerificationRequest.evidence_file_id.is_not(None),
            VerificationRequest.evidence_expires_at.is_not(None),
            VerificationRequest.evidence_expires_at < now,
        )
        .values(evidence_file_id=None, video_note_file_id=None)
    )
    return {
        "expired_search": int(expired_search.rowcount or 0),
        "closed_chats": closed,
        "purged_messages": int(removed.rowcount or 0),
    }
