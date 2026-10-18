from __future__ import annotations

from datetime import timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.clock import utcnow
from core.config import get_settings
from core.errors import AppError
from core.i18n import t
from core.security import new_code
from database.models import User, VerificationEvent, VerificationRequest
from services.notify import enqueue
from services.referrals import qualify
from services.trust import refresh_trust

OPEN_STATUSES = {"pending", "in_review"}
DECISIONS = {"approved", "rejected", "suspended", "revoked"}


def _event(request: VerificationRequest, actor: str, new_status: str, note: str | None = None) -> VerificationEvent:
    return VerificationEvent(
        request_id=request.id,
        actor=actor,
        from_status=request.status,
        to_status=new_status,
        note=note,
    )


async def latest_request(session: AsyncSession, user_id: int) -> VerificationRequest | None:
    return await session.scalar(
        select(VerificationRequest)
        .where(VerificationRequest.user_id == user_id)
        .order_by(VerificationRequest.id.desc())
        .limit(1)
    )


async def start_manual(session: AsyncSession, user: User) -> VerificationRequest:
    if user.verification_status == "approved":
        raise AppError("already_verified", "verify.approved")
    if user.underage_blocked:
        raise AppError("underage", "age.rejected")
    current = await latest_request(session, user.id)
    if current and current.status in OPEN_STATUSES:
        return current
    request = VerificationRequest(
        user_id=user.id,
        status="pending",
        method="manual_selfie",
        one_time_code=new_code(6),
    )
    session.add(request)
    user.verification_status = "pending"
    await session.flush()
    session.add(_event(request, f"user:{user.id}", "pending", "manual start"))
    await enqueue(session, user, t(user.language, "verify.need_photo"))
    return request


async def submit_photo(
    session: AsyncSession,
    user: User,
    *,
    file_id: str,
    digest: str,
) -> VerificationRequest:
    request = await _open_manual_request(session, user)
    clash = await session.scalar(
        select(VerificationRequest.id).where(
            VerificationRequest.evidence_sha256 == digest,
            VerificationRequest.user_id != user.id,
        )
    )
    if clash:
        raise AppError("duplicate_photo", "verify.duplicate")
    request.evidence_file_id = file_id
    request.evidence_sha256 = digest
    request.video_note_file_id = None
    request.caption_matched = False
    request.status = "pending"
    request.updated_at = utcnow()
    user.verification_status = "pending"
    session.add(_event(request, f"user:{user.id}", "pending", "photo"))
    await session.flush()
    return request


async def submit_video_note(session: AsyncSession, user: User, *, file_id: str) -> VerificationRequest:
    request = await latest_request(session, user.id)
    if request is None or request.method != "manual_selfie":
        raise AppError("no_request", "verify.need_photo")
    if request.status == "in_review":
        raise AppError("in_review", "verify.in_review")
    if request.status not in {"pending", "rejected"}:
        raise AppError("no_request", "verify.need_photo")
    if not request.evidence_file_id:
        raise AppError("need_photo", "verify.need_photo")
    previous = request.status
    request.video_note_file_id = file_id
    request.status = "in_review"
    request.submitted_at = utcnow()
    request.evidence_expires_at = utcnow() + timedelta(hours=72)
    request.updated_at = utcnow()
    user.verification_status = "in_review"
    session.add(_event(request, f"user:{user.id}", "in_review", previous))
    await session.flush()
    return request


async def _open_manual_request(session: AsyncSession, user: User) -> VerificationRequest:
    request = await latest_request(session, user.id)
    if request is None or request.method != "manual_selfie" or request.status not in {"pending", "rejected"}:
        raise AppError("no_request", "verify.need_photo")
    if request.status == "rejected":
        request.status = "pending"
        request.evidence_file_id = None
        request.video_note_file_id = None
        request.evidence_sha256 = None
        user.verification_status = "pending"
    return request


async def start_external(session: AsyncSession, user: User) -> str:
    settings = get_settings()
    if not settings.provider_configured:
        raise AppError("provider_unconfigured", "verify.provider_missing")
    if user.verification_status == "approved":
        raise AppError("already_verified", "verify.approved")
    request = VerificationRequest(user_id=user.id, status="pending", method="external")
    session.add(request)
    await session.flush()
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                settings.verification_provider_url.rstrip("/") + "/verifications",
                headers={"Authorization": f"Bearer {settings.verification_provider_key}"},
                json={"reference": str(user.id), "min_age": 18},
            )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        request.status = "rejected"
        request.reject_reason = "provider_unreachable"
        raise AppError("provider_failed", "verify.provider_missing") from exc
    operation_id = str(data.get("operation_id") or "")
    redirect_url = str(data.get("redirect_url") or "")
    if not operation_id or not redirect_url:
        raise AppError("provider_failed", "verify.provider_missing")
    request.provider_operation_id = operation_id
    user.verification_status = "pending"
    session.add(_event(request, "provider", "pending", operation_id))
    await session.flush()
    return redirect_url


async def apply_provider_result(
    session: AsyncSession,
    *,
    operation_id: str,
    status: str,
    reason: str | None = None,
) -> VerificationRequest:
    if status not in {"approved", "rejected"}:
        raise AppError("bad_status", "errors.generic")
    request = await session.scalar(
        select(VerificationRequest).where(VerificationRequest.provider_operation_id == operation_id)
    )
    if request is None:
        raise AppError("missing", "errors.not_found", status_code=404)
    if request.status == status:
        return request
    user = await session.get(User, request.user_id)
    if user is None:
        raise AppError("missing", "errors.not_found", status_code=404)
    request.provider_result = status
    await decide(session, request, user, status, actor="provider", note=reason)
    return request


async def decide(
    session: AsyncSession,
    request: VerificationRequest,
    user: User,
    status: str,
    *,
    actor: str,
    note: str | None = None,
    admin_id: int | None = None,
) -> None:
    if status not in DECISIONS:
        raise AppError("bad_status", "errors.generic")
    if request.status == status and user.verification_status == status:
        return
    session.add(_event(request, actor, status, note))
    request.status = status
    request.reject_reason = note if status != "approved" else None
    request.reviewed_at = utcnow()
    request.reviewed_by_admin_id = admin_id
    request.evidence_file_id = None
    request.video_note_file_id = None
    request.updated_at = utcnow()
    user.verification_status = status
    refresh_trust(user)
    if status == "approved":
        await qualify(session, user)
        await enqueue(session, user, t(user.language, "verify.approved"))
    elif status == "rejected":
        await enqueue(session, user, t(user.language, "verify.rejected"))
    else:
        await enqueue(session, user, t(user.language, f"verify.{status}"))
    await session.flush()
