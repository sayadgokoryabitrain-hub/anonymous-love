from __future__ import annotations

import csv
import io
import logging
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.deps import db_session, require_role
from core.clock import utcnow
from core.config import get_settings, validate_runtime
from core.errors import AppError, AuthError
from core.i18n import clear_override_cache, load_bundles
from core.rate_limit import get_limiter
from core.security import decode_token, hash_password, identity_hash, issue_token, validate_init_data, verify_password, verify_provider_signature
from database.models import (
    AdminAuditLog,
    AdminUser,
    Appeal,
    ChatParticipant,
    ChatSession,
    CoinPackage,
    DailyTask,
    ErrorLog,
    Gift,
    LocalizationOverride,
    PaymentOrder,
    Profile,
    PromoCode,
    Referral,
    Report,
    Subscription,
    SystemSetting,
    User,
    UserBlock,
    VerificationRequest,
    VipPlan,
    WalletAccount,
    WalletTransaction,
)
from database.repositories.users import get_profile, get_user
from database.session import get_session_factory, init_database
from services.audit import audit
from services.engagement import claim_task, redeem_promo
from services.moderation import ban_user, resolve_appeal, restrict_user, unban_user, warn_user
from services.referrals import freeze
from services.settings_store import SETTING_KEYS
from services.verification import apply_provider_result, decide, latest_request
from services.vip import grant_days
from services.wallet import ADMIN_ADJUSTMENT, apply

logger = logging.getLogger(__name__)


class LoginIn(BaseModel):
    username: str
    password: str


class ReasonIn(BaseModel):
    reason: str = Field(min_length=3, max_length=300)


class RestrictIn(ReasonIn):
    hours: int = Field(ge=1, le=720)


class DecisionIn(BaseModel):
    status: str
    reason: str = Field(min_length=3, max_length=500)


class ReportIn(BaseModel):
    resolution: str = Field(min_length=3, max_length=500)
    action: str


class AdjustIn(BaseModel):
    user_id: int
    amount: int
    reason: str = Field(min_length=5, max_length=300)
    idempotency_key: str = Field(min_length=8, max_length=160)


class PromoIn(BaseModel):
    code: str = Field(min_length=3, max_length=32)
    coins: int = Field(gt=0, le=100000)
    max_redemptions: int = Field(ge=1, le=100000)
    days_valid: int | None = Field(default=None, ge=1, le=365)


class SettingIn(BaseModel):
    key: str
    value: int


class TranslationIn(BaseModel):
    language: str
    key: str
    value: str = Field(max_length=2000)


class GiftIn(BaseModel):
    price: int = Field(ge=0, le=100000)
    active: bool


class TaskIn(BaseModel):
    reward: int = Field(ge=0, le=100000)
    active: bool


class VipGrantIn(BaseModel):
    user_id: int
    days: int = Field(ge=1, le=365)
    reason: str = Field(min_length=3, max_length=300)


class AdminIn(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=10, max_length=128)
    role: str


class PackageIn(BaseModel):
    coins: int = Field(gt=0, le=1_000_000)
    stars: int = Field(gt=0, le=1_000_000)
    active: bool


class PlanIn(BaseModel):
    days: int = Field(ge=1, le=365)
    coin_price: int = Field(ge=0, le=1_000_000)
    star_price: int = Field(ge=0, le=1_000_000)
    active: bool


class InitIn(BaseModel):
    init_data: str


class PromoCodeIn(BaseModel):
    code: str


def create_app(session_factory: async_sessionmaker[AsyncSession] | None = None, *, init_db: bool = True) -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if app.state.init_db:
            validate_runtime(settings)
            await init_database(app.state.session_factory)
            if settings.use_webhook and settings.bot_token:
                from aiogram import Bot

                from bot.app import build_dispatcher

                bot = Bot(token=settings.bot_token)
                app.state.bot = bot
                app.state.dispatcher = build_dispatcher(app.state.session_factory, bot)
        yield
        bot = getattr(app.state, "bot", None)
        if bot is not None:
            await bot.session.close()

    app = FastAPI(title="ANONYMOUS LOVE", docs_url=None if settings.environment == "production" else "/docs", lifespan=lifespan)
    app.state.session_factory = session_factory or get_session_factory()
    app.state.init_db = init_db
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.exception_handler(AppError)
    async def app_error(_, exc: AppError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message_key, "code": exc.code})

    @app.get("/health")
    async def health(session: AsyncSession = Depends(db_session)):
        await session.execute(select(func.count(User.id)))
        return {"status": "ok"}

    @app.post("/api/auth/login")
    async def login(body: LoginIn, request: Request, session: AsyncSession = Depends(db_session)):
        ip = request.client.host if request.client else "local"
        if not await get_limiter().allow(f"admin-login:{ip}", 8, 60):
            raise HTTPException(status_code=429, detail="rate_limit")
        admin = await session.scalar(select(AdminUser).where(AdminUser.username == body.username.strip().lower()))
        if admin is None or not admin.is_active or not verify_password(body.password, admin.password_hash):
            raise HTTPException(status_code=401, detail="unauthorized")
        admin.last_login_at = utcnow()
        token = issue_token(str(admin.id), admin.role, "admin", settings.access_token_hours)
        return {"access_token": token, "role": admin.role, "username": admin.username}

    @app.get("/api/auth/me")
    async def me(admin: AdminUser = Depends(require_role("support"))):
        return {"id": admin.id, "username": admin.username, "role": admin.role}

    @app.get("/api/stats")
    async def stats(_: AdminUser = Depends(require_role("support")), session: AsyncSession = Depends(db_session)):
        users = await session.scalar(select(func.count(User.id)))
        active_chats = await session.scalar(select(func.count(ChatSession.id)).where(ChatSession.status == "active"))
        open_reports = await session.scalar(select(func.count(Report.id)).where(Report.status == "open"))
        pending = await session.scalar(
            select(func.count(VerificationRequest.id)).where(VerificationRequest.status.in_(["pending", "in_review"]))
        )
        paid = await session.scalar(select(func.count(PaymentOrder.id)).where(PaymentOrder.status == "paid"))
        return {
            "users": users or 0,
            "active_chats": active_chats or 0,
            "open_reports": open_reports or 0,
            "verification_queue": pending or 0,
            "paid_orders": paid or 0,
        }

    @app.get("/api/users")
    async def users(q: str = "", limit: int = 50, offset: int = 0, admin: AdminUser = Depends(require_role("support")), session: AsyncSession = Depends(db_session)):
        stmt = select(User).order_by(User.id.desc())
        if q.isdigit():
            stmt = stmt.where(User.id == int(q))
        elif q:
            stmt = stmt.join(Profile, Profile.user_id == User.id).where(Profile.nickname_folded == q.casefold())
        rows = (await session.execute(stmt.limit(min(limit, 100)).offset(offset))).scalars().all()
        return [await _user_out(session, row, admin.role) for row in rows]

    @app.get("/api/users/{user_id}")
    async def user_detail(user_id: int, admin: AdminUser = Depends(require_role("support")), session: AsyncSession = Depends(db_session)):
        user = await get_user(session, user_id)
        if user is None:
            raise HTTPException(404, "not_found")
        return await _user_out(session, user, admin.role)

    @app.post("/api/users/{user_id}/warn")
    async def warn(user_id: int, body: ReasonIn, admin: AdminUser = Depends(require_role("moderator")), session: AsyncSession = Depends(db_session)):
        user = await _need_user(session, user_id)
        await warn_user(session, user, body.reason)
        await audit(session, admin.id, "warn", "user", user_id, {"reason": body.reason})
        return {"ok": True}

    @app.post("/api/users/{user_id}/restrict")
    async def restrict(user_id: int, body: RestrictIn, admin: AdminUser = Depends(require_role("moderator")), session: AsyncSession = Depends(db_session)):
        user = await _need_user(session, user_id)
        await restrict_user(session, user, body.hours, body.reason)
        await audit(session, admin.id, "restrict", "user", user_id, {"hours": body.hours, "reason": body.reason})
        return {"ok": True}

    @app.post("/api/users/{user_id}/ban")
    async def ban(user_id: int, body: ReasonIn, admin: AdminUser = Depends(require_role("moderator")), session: AsyncSession = Depends(db_session)):
        user = await _need_user(session, user_id)
        await ban_user(session, user, body.reason)
        await audit(session, admin.id, "ban", "user", user_id, {"reason": body.reason})
        return {"ok": True}

    @app.post("/api/users/{user_id}/unban")
    async def unban(user_id: int, body: ReasonIn, admin: AdminUser = Depends(require_role("moderator")), session: AsyncSession = Depends(db_session)):
        user = await _need_user(session, user_id)
        await unban_user(session, user)
        await audit(session, admin.id, "unban", "user", user_id, {"reason": body.reason})
        return {"ok": True}

    @app.post("/api/users/{user_id}/verification")
    async def set_verification(user_id: int, body: DecisionIn, admin: AdminUser = Depends(require_role("moderator")), session: AsyncSession = Depends(db_session)):
        user = await _need_user(session, user_id)
        request = await latest_request(session, user_id)
        if request is None:
            request = VerificationRequest(user_id=user_id, status=user.verification_status, method="manual_selfie")
            session.add(request)
            await session.flush()
        await decide(session, request, user, body.status, actor=f"admin:{admin.id}", note=body.reason, admin_id=admin.id)
        await audit(session, admin.id, "verification", "user", user_id, {"status": body.status, "reason": body.reason})
        return {"ok": True}

    @app.get("/api/verifications")
    async def verifications(status: str = "in_review", _: AdminUser = Depends(require_role("moderator")), session: AsyncSession = Depends(db_session)):
        rows = (
            await session.execute(select(VerificationRequest).where(VerificationRequest.status == status).order_by(VerificationRequest.id.desc()).limit(100))
        ).scalars().all()
        return [
            {
                "id": row.id,
                "user_id": row.user_id,
                "status": row.status,
                "method": row.method,
                "caption_matched": row.caption_matched,
                "has_evidence": bool(row.evidence_file_id),
                "has_photo": bool(row.evidence_file_id),
                "has_video_note": bool(row.video_note_file_id),
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ]

    @app.get("/api/verifications/{request_id}/media")
    async def verification_media(
        request_id: int,
        kind: str = "photo",
        admin: AdminUser = Depends(require_role("moderator")),
        session: AsyncSession = Depends(db_session),
    ):
        row = await session.get(VerificationRequest, request_id)
        if row is None:
            raise HTTPException(404, "not_found")
        file_id = row.evidence_file_id if kind == "photo" else row.video_note_file_id if kind == "video" else None
        settings_now = get_settings()
        if not file_id or not settings_now.bot_token:
            raise HTTPException(404, "not_found")
        await audit(session, admin.id, "view_verification_media", "verification", request_id, {"kind": kind})
        async with httpx.AsyncClient(timeout=30) as client:
            meta = await client.get(
                f"https://api.telegram.org/bot{settings_now.bot_token}/getFile",
                params={"file_id": file_id},
            )
            if meta.status_code != 200:
                raise HTTPException(404, "not_found")
            file_path = str((meta.json().get("result") or {}).get("file_path") or "")
            if not file_path:
                raise HTTPException(404, "not_found")
            downloaded = await client.get(f"https://api.telegram.org/file/bot{settings_now.bot_token}/{file_path}")
        if downloaded.status_code != 200:
            raise HTTPException(404, "not_found")
        media_type = "video/mp4" if kind == "video" else "image/jpeg"
        return Response(downloaded.content, media_type=media_type)

    @app.get("/api/reports")
    async def reports(status: str = "open", _: AdminUser = Depends(require_role("moderator")), session: AsyncSession = Depends(db_session)):
        rows = (await session.execute(select(Report).where(Report.status == status).order_by(Report.id.desc()).limit(100))).scalars().all()
        return [_report_out(row) for row in rows]

    @app.get("/api/reports/{report_id}")
    async def report_detail(report_id: int, admin: AdminUser = Depends(require_role("moderator")), session: AsyncSession = Depends(db_session)):
        row = await session.get(Report, report_id)
        if row is None:
            raise HTTPException(404, "not_found")
        await audit(session, admin.id, "view_report", "report", report_id, {})
        return _report_out(row)

    @app.post("/api/reports/{report_id}/resolve")
    async def resolve_report(report_id: int, body: ReportIn, admin: AdminUser = Depends(require_role("moderator")), session: AsyncSession = Depends(db_session)):
        row = await session.get(Report, report_id)
        if row is None:
            raise HTTPException(404, "not_found")
        target = await _need_user(session, row.target_user_id)
        if body.action == "warn":
            await warn_user(session, target, body.resolution)
        elif body.action == "restrict":
            await restrict_user(session, target, 24, body.resolution)
        elif body.action == "ban":
            await ban_user(session, target, body.resolution)
        elif body.action != "dismiss":
            raise HTTPException(400, "bad_action")
        row.status = "dismissed" if body.action == "dismiss" else "resolved"
        row.resolution = body.resolution
        row.reviewed_by = admin.id
        row.resolved_at = utcnow()
        await audit(session, admin.id, "resolve_report", "report", report_id, {"action": body.action})
        return {"ok": True}

    @app.get("/api/chats/active")
    async def active_chats(_: AdminUser = Depends(require_role("moderator")), session: AsyncSession = Depends(db_session)):
        rows = (await session.execute(select(ChatSession).where(ChatSession.status == "active").limit(100))).scalars().all()
        result = []
        for chat in rows:
            people = (
                await session.execute(select(ChatParticipant.user_id, ChatParticipant.messages_sent).where(ChatParticipant.session_id == chat.id))
            ).all()
            result.append(
                {
                    "id": chat.id,
                    "started_at": chat.started_at.isoformat() if chat.started_at else None,
                    "score": chat.compatibility_score,
                    "participants": [{"user_id": user_id, "messages_sent": sent} for user_id, sent in people],
                }
            )
        return result

    @app.get("/api/blocks")
    async def blocks(_: AdminUser = Depends(require_role("moderator")), session: AsyncSession = Depends(db_session)):
        rows = (await session.execute(select(UserBlock).order_by(UserBlock.id.desc()).limit(100))).scalars().all()
        return [{"id": row.id, "blocker_id": row.blocker_id, "blocked_id": row.blocked_id} for row in rows]

    @app.get("/api/payments")
    async def payments(_: AdminUser = Depends(require_role("support")), session: AsyncSession = Depends(db_session)):
        rows = (await session.execute(select(PaymentOrder).order_by(PaymentOrder.created_at.desc()).limit(100))).scalars().all()
        return [
            {
                "id": row.id,
                "user_id": row.user_id,
                "kind": row.kind,
                "package_code": row.package_code,
                "stars": row.stars,
                "coins": row.coins,
                "status": row.status,
                "mode": row.mode,
            }
            for row in rows
        ]

    @app.get("/api/wallet/transactions")
    async def transactions(user_id: int | None = None, _: AdminUser = Depends(require_role("administrator")), session: AsyncSession = Depends(db_session)):
        stmt = select(WalletTransaction).order_by(WalletTransaction.id.desc()).limit(100)
        if user_id:
            stmt = stmt.where(WalletTransaction.user_id == user_id)
        rows = (await session.execute(stmt)).scalars().all()
        return [
            {
                "id": row.id,
                "user_id": row.user_id,
                "operation_type": row.operation_type,
                "amount": row.amount,
                "balance_after": row.balance_after,
                "source": row.source,
                "status": row.status,
                "actor": row.actor,
                "idempotency_key": row.idempotency_key,
            }
            for row in rows
        ]

    @app.post("/api/wallet/adjust")
    async def adjust(body: AdjustIn, admin: AdminUser = Depends(require_role("administrator")), session: AsyncSession = Depends(db_session)):
        if body.amount == 0:
            raise HTTPException(400, "zero")
        tx = await apply(
            session,
            body.user_id,
            body.amount,
            ADMIN_ADJUSTMENT,
            source="admin_adjustment",
            idempotency_key=body.idempotency_key,
            actor=f"admin:{admin.id}",
            meta={"reason": body.reason},
        )
        await audit(session, admin.id, "wallet_adjust", "user", body.user_id, {"amount": body.amount, "reason": body.reason, "tx": tx.id})
        return {"transaction_id": tx.id, "balance_after": tx.balance_after}

    @app.get("/api/referrals")
    async def referrals(_: AdminUser = Depends(require_role("support")), session: AsyncSession = Depends(db_session)):
        rows = (await session.execute(select(Referral).order_by(Referral.id.desc()).limit(100))).scalars().all()
        return [
            {"id": row.id, "inviter_id": row.inviter_id, "invitee_id": row.invitee_id, "status": row.status, "frozen": row.frozen}
            for row in rows
        ]

    @app.post("/api/referrals/{referral_id}/freeze")
    async def freeze_referral(referral_id: int, body: ReasonIn, admin: AdminUser = Depends(require_role("administrator")), session: AsyncSession = Depends(db_session)):
        row = await freeze(session, referral_id, True)
        if row is None:
            raise HTTPException(404, "not_found")
        await audit(session, admin.id, "freeze_referral", "referral", referral_id, {"reason": body.reason})
        return {"ok": True}

    @app.get("/api/subscriptions")
    async def subscriptions(_: AdminUser = Depends(require_role("support")), session: AsyncSession = Depends(db_session)):
        rows = (await session.execute(select(Subscription).order_by(Subscription.id.desc()).limit(100))).scalars().all()
        return [
            {"id": row.id, "user_id": row.user_id, "plan_code": row.plan_code, "status": row.status, "ends_at": row.ends_at.isoformat()}
            for row in rows
        ]

    @app.post("/api/subscriptions/grant")
    async def grant_vip(body: VipGrantIn, admin: AdminUser = Depends(require_role("administrator")), session: AsyncSession = Depends(db_session)):
        sub = await grant_days(session, body.user_id, body.days, source=f"admin:{admin.id}", idempotency_key=f"admin-vip:{body.user_id}:{body.reason}:{int(utcnow().timestamp())}")
        await audit(session, admin.id, "grant_vip", "user", body.user_id, {"days": body.days, "reason": body.reason})
        return {"id": sub.id, "ends_at": sub.ends_at.isoformat()}

    @app.get("/api/gifts")
    async def gifts(_: AdminUser = Depends(require_role("support")), session: AsyncSession = Depends(db_session)):
        rows = (await session.execute(select(Gift).order_by(Gift.sort_order))).scalars().all()
        return [{"id": row.id, "code": row.code, "emoji": row.emoji, "price": row.price, "vip_only": row.vip_only, "active": row.active} for row in rows]

    @app.put("/api/gifts/{gift_id}")
    async def edit_gift(gift_id: int, body: GiftIn, admin: AdminUser = Depends(require_role("administrator")), session: AsyncSession = Depends(db_session)):
        gift = await session.get(Gift, gift_id)
        if gift is None:
            raise HTTPException(404, "not_found")
        gift.price = body.price
        gift.active = body.active
        await audit(session, admin.id, "edit_gift", "gift", gift_id, body.model_dump())
        return {"ok": True}

    @app.get("/api/tasks")
    async def tasks(_: AdminUser = Depends(require_role("support")), session: AsyncSession = Depends(db_session)):
        rows = (await session.execute(select(DailyTask).order_by(DailyTask.sort_order))).scalars().all()
        return [{"id": row.id, "code": row.code, "reward": row.reward, "repeatable": row.repeatable, "active": row.active} for row in rows]

    @app.put("/api/tasks/{task_id}")
    async def edit_task(task_id: int, body: TaskIn, admin: AdminUser = Depends(require_role("administrator")), session: AsyncSession = Depends(db_session)):
        task = await session.get(DailyTask, task_id)
        if task is None:
            raise HTTPException(404, "not_found")
        task.reward = body.reward
        task.active = body.active
        await audit(session, admin.id, "edit_task", "task", task_id, body.model_dump())
        return {"ok": True}

    @app.get("/api/promos")
    async def promos(_: AdminUser = Depends(require_role("administrator")), session: AsyncSession = Depends(db_session)):
        rows = (await session.execute(select(PromoCode).order_by(PromoCode.id.desc()))).scalars().all()
        return [{"id": row.id, "code": row.code, "coins": row.coins, "max_redemptions": row.max_redemptions, "redemptions_count": row.redemptions_count, "active": row.active} for row in rows]

    @app.post("/api/promos")
    async def create_promo(body: PromoIn, admin: AdminUser = Depends(require_role("administrator")), session: AsyncSession = Depends(db_session)):
        promo = PromoCode(
            code=body.code.strip().upper(),
            coins=body.coins,
            max_redemptions=body.max_redemptions,
            active=True,
            expires_at=utcnow() + timedelta(days=body.days_valid) if body.days_valid else None,
            created_by=admin.id,
        )
        session.add(promo)
        await session.flush()
        await audit(session, admin.id, "create_promo", "promo", promo.id, {"code": promo.code})
        return {"id": promo.id}

    @app.post("/api/promos/{promo_id}/deactivate")
    async def deactivate_promo(promo_id: int, admin: AdminUser = Depends(require_role("administrator")), session: AsyncSession = Depends(db_session)):
        promo = await session.get(PromoCode, promo_id)
        if promo is None:
            raise HTTPException(404, "not_found")
        promo.active = False
        await audit(session, admin.id, "deactivate_promo", "promo", promo_id, {})
        return {"ok": True}

    @app.get("/api/translations")
    async def translations(lang: str = "ru", _: AdminUser = Depends(require_role("administrator"))):
        bundle = load_bundles().get(lang) or {}
        return [{"key": key, "value": value} for key, value in sorted(bundle.items())]

    @app.put("/api/translations")
    async def put_translation(body: TranslationIn, admin: AdminUser = Depends(require_role("administrator")), session: AsyncSession = Depends(db_session)):
        if body.language not in {"ru", "hy", "en"} or body.key not in load_bundles()["ru"]:
            raise HTTPException(400, "bad_translation")
        row = await session.scalar(
            select(LocalizationOverride).where(LocalizationOverride.language == body.language, LocalizationOverride.message_key == body.key)
        )
        if row is None:
            row = LocalizationOverride(language=body.language, message_key=body.key, value=body.value, updated_by=admin.id)
            session.add(row)
        else:
            row.value = body.value
            row.updated_by = admin.id
            row.updated_at = utcnow()
        clear_override_cache()
        await audit(session, admin.id, "translation", "locale", body.key, {"language": body.language})
        return {"ok": True}

    @app.get("/api/settings")
    async def get_settings_api(_: AdminUser = Depends(require_role("administrator")), session: AsyncSession = Depends(db_session)):
        rows = (await session.execute(select(SystemSetting))).scalars().all()
        return [{"key": row.key, "value": row.value} for row in rows if row.key in SETTING_KEYS]

    @app.put("/api/settings")
    async def put_setting(body: SettingIn, admin: AdminUser = Depends(require_role("administrator")), session: AsyncSession = Depends(db_session)):
        if body.key not in SETTING_KEYS:
            raise HTTPException(400, "unknown_setting")
        row = await session.get(SystemSetting, body.key)
        if row is None:
            row = SystemSetting(key=body.key, value=body.value, updated_by=admin.id)
            session.add(row)
        else:
            row.value = body.value
            row.updated_by = admin.id
            row.updated_at = utcnow()
        await audit(session, admin.id, "setting", "setting", body.key, {"value": body.value})
        return {"ok": True}

    @app.get("/api/audit")
    async def audit_log(_: AdminUser = Depends(require_role("administrator")), session: AsyncSession = Depends(db_session)):
        rows = (await session.execute(select(AdminAuditLog).order_by(AdminAuditLog.id.desc()).limit(200))).scalars().all()
        return [
            {"id": row.id, "admin_id": row.admin_id, "action": row.action, "entity_type": row.entity_type, "entity_id": row.entity_id, "details": row.details}
            for row in rows
        ]

    @app.get("/api/errors")
    async def errors(_: AdminUser = Depends(require_role("administrator")), session: AsyncSession = Depends(db_session)):
        rows = (await session.execute(select(ErrorLog).order_by(ErrorLog.id.desc()).limit(100))).scalars().all()
        return [{"id": row.id, "source": row.source, "message": row.message, "created_at": row.created_at.isoformat() if row.created_at else None} for row in rows]

    @app.get("/api/appeals")
    async def appeals(_: AdminUser = Depends(require_role("moderator")), session: AsyncSession = Depends(db_session)):
        rows = (await session.execute(select(Appeal).where(Appeal.status == "open").order_by(Appeal.id.desc()))).scalars().all()
        return [{"id": row.id, "user_id": row.user_id, "text": row.text, "status": row.status} for row in rows]

    @app.post("/api/appeals/{appeal_id}/resolve")
    async def resolve(appeal_id: int, body: ReportIn, admin: AdminUser = Depends(require_role("moderator")), session: AsyncSession = Depends(db_session)):
        appeal = await session.get(Appeal, appeal_id)
        if appeal is None:
            raise HTTPException(404, "not_found")
        await resolve_appeal(session, appeal, body.action == "accept", admin.id)
        await audit(session, admin.id, "appeal", "appeal", appeal_id, {"action": body.action})
        return {"ok": True}

    @app.get("/api/admins")
    async def admins(_: AdminUser = Depends(require_role("superadmin")), session: AsyncSession = Depends(db_session)):
        rows = (await session.execute(select(AdminUser).order_by(AdminUser.id))).scalars().all()
        return [{"id": row.id, "username": row.username, "role": row.role, "is_active": row.is_active} for row in rows]

    @app.post("/api/admins")
    async def create_admin(body: AdminIn, admin: AdminUser = Depends(require_role("superadmin")), session: AsyncSession = Depends(db_session)):
        if body.role not in ROLE_SAFE:
            raise HTTPException(400, "bad_role")
        session.add(AdminUser(username=body.username.strip().lower(), password_hash=hash_password(body.password), role=body.role))
        await audit(session, admin.id, "create_admin", "admin", body.username, {"role": body.role})
        return {"ok": True}

    @app.get("/api/export/summary.csv")
    async def export_summary(_: AdminUser = Depends(require_role("administrator")), session: AsyncSession = Depends(db_session)):
        users = await session.scalar(select(func.count(User.id))) or 0
        paid = await session.scalar(select(func.count(PaymentOrder.id)).where(PaymentOrder.status == "paid")) or 0
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["metric", "value"])
        writer.writerow(["users", users])
        writer.writerow(["paid_orders", paid])
        return Response(buffer.getvalue(), media_type="text/csv")

    @app.post("/api/payments/crypto")
    async def crypto_webhook(request: Request, session: AsyncSession = Depends(db_session)):
        from services.payments import apply_crypto_update

        body = await request.body()
        signature = request.headers.get("crypto-pay-api-signature", "")
        await apply_crypto_update(session, body, signature)
        return {"ok": True}

    @app.post("/api/verification/provider-callback")
    async def provider_callback(request: Request, session: AsyncSession = Depends(db_session)):
        settings_now = get_settings()
        if not settings_now.provider_configured:
            raise HTTPException(503, "provider_not_configured")
        body = await request.body()
        signature = request.headers.get("X-Provider-Signature", "")
        if not verify_provider_signature(settings_now.verification_provider_secret, body, signature):
            raise HTTPException(401, "bad_signature")
        import json

        payload = json.loads(body.decode("utf-8"))
        await apply_provider_result(
            session,
            operation_id=str(payload.get("operation_id") or ""),
            status=str(payload.get("status") or ""),
            reason=payload.get("reason"),
        )
        return {"ok": True}

    @app.post("/api/miniapp/session")
    async def mini_session(body: InitIn, session: AsyncSession = Depends(db_session)):
        settings_now = get_settings()
        try:
            tg_user = validate_init_data(body.init_data, settings_now.bot_token, settings_now.auth_date_max_age_sec)
        except AuthError as exc:
            raise HTTPException(401, "unauthorized") from exc
        digest = identity_hash(int(tg_user["id"]))
        user = await session.scalar(select(User).where(User.telegram_id_hash == digest))
        if user is None or user.status == "deleted":
            raise HTTPException(401, "register_in_bot")
        token = issue_token(str(user.id), "user", "miniapp", settings_now.miniapp_token_hours, {"tg": int(tg_user["id"])})
        return {"access_token": token}

    @app.get("/api/miniapp/me")
    async def mini_me(user: User = Depends(_mini_user)):
        return {"id": user.id, "language": user.language, "verification_status": user.verification_status, "trust_score": user.trust_score}

    @app.get("/api/miniapp/overview")
    async def mini_overview(user: User = Depends(_mini_user), session: AsyncSession = Depends(db_session)):
        profile = await get_profile(session, user.id)
        account = await session.get(WalletAccount, user.id)
        gifts = (await session.execute(select(Gift).where(Gift.active.is_(True)).order_by(Gift.sort_order))).scalars().all()
        tasks = (await session.execute(select(DailyTask).where(DailyTask.active.is_(True)).order_by(DailyTask.sort_order))).scalars().all()
        return {
            "id": user.id,
            "language": user.language,
            "verification_status": user.verification_status,
            "trust_score": user.trust_score,
            "nickname": profile.nickname if profile else None,
            "gender": profile.gender if profile else None,
            "age": profile.age if profile else None,
            "balance": account.balance if account else 0,
            "gifts": [{"code": gift.code, "emoji": gift.emoji, "price": gift.price, "vip_only": gift.vip_only} for gift in gifts],
            "tasks": [{"code": task.code, "reward": task.reward} for task in tasks],
        }

    @app.post("/api/miniapp/promo")
    async def mini_promo(body: PromoCodeIn, user: User = Depends(_mini_user), session: AsyncSession = Depends(db_session)):
        coins = await redeem_promo(session, user, body.code)
        return {"coins": coins}

    @app.post("/api/miniapp/tasks/{code}/claim")
    async def mini_claim(code: str, user: User = Depends(_mini_user), session: AsyncSession = Depends(db_session)):
        coins = await claim_task(session, user, code)
        return {"coins": coins}

    @app.get("/api/packages")
    async def packages(_: AdminUser = Depends(require_role("support")), session: AsyncSession = Depends(db_session)):
        rows = (await session.execute(select(CoinPackage).order_by(CoinPackage.sort_order))).scalars().all()
        return [{"id": row.id, "code": row.code, "coins": row.coins, "stars": row.stars, "active": row.active} for row in rows]

    @app.put("/api/packages/{package_id}")
    async def edit_package(package_id: int, body: PackageIn, admin: AdminUser = Depends(require_role("administrator")), session: AsyncSession = Depends(db_session)):
        row = await session.get(CoinPackage, package_id)
        if row is None:
            raise HTTPException(404, "not_found")
        row.coins = body.coins
        row.stars = body.stars
        row.active = body.active
        await audit(session, admin.id, "edit_package", "package", package_id, body.model_dump())
        return {"ok": True}

    @app.get("/api/vip-plans")
    async def vip_plans(_: AdminUser = Depends(require_role("support")), session: AsyncSession = Depends(db_session)):
        rows = (await session.execute(select(VipPlan).order_by(VipPlan.days))).scalars().all()
        return [{"id": row.id, "code": row.code, "days": row.days, "coin_price": row.coin_price, "star_price": row.star_price, "active": row.active} for row in rows]

    @app.put("/api/vip-plans/{plan_id}")
    async def edit_plan(plan_id: int, body: PlanIn, admin: AdminUser = Depends(require_role("administrator")), session: AsyncSession = Depends(db_session)):
        row = await session.get(VipPlan, plan_id)
        if row is None:
            raise HTTPException(404, "not_found")
        row.days = body.days
        row.coin_price = body.coin_price
        row.star_price = body.star_price
        row.active = body.active
        await audit(session, admin.id, "edit_vip_plan", "vip_plan", plan_id, body.model_dump())
        return {"ok": True}

    @app.post("/telegram/webhook")
    async def telegram_webhook(request: Request):
        settings_now = get_settings()
        if not settings_now.use_webhook:
            raise HTTPException(404, "webhook_disabled")
        secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not settings_now.webhook_secret or secret != settings_now.webhook_secret:
            raise HTTPException(403, "bad_webhook_secret")
        dispatcher = getattr(request.app.state, "dispatcher", None)
        bot = getattr(request.app.state, "bot", None)
        if dispatcher is None or bot is None:
            raise HTTPException(503, "bot_not_attached")
        from aiogram.types import Update

        update = Update.model_validate(await request.json(), context={"bot": bot})
        await dispatcher.feed_update(bot, update)
        return {"ok": True}

    return app


ROLE_SAFE = {"administrator", "moderator", "support"}


async def _need_user(session: AsyncSession, user_id: int) -> User:
    user = await get_user(session, user_id)
    if user is None:
        raise HTTPException(404, "not_found")
    return user


async def _user_out(session: AsyncSession, user: User, role: str) -> dict:
    profile = await get_profile(session, user.id)
    data = {
        "id": user.id,
        "status": user.status,
        "language": user.language,
        "verification_status": user.verification_status,
        "trust_score": user.trust_score,
        "nickname": profile.nickname if profile else None,
        "gender": profile.gender if profile else None,
        "age": profile.age if profile else None,
        "registration_completed": user.registration_completed,
        "warning_count": user.warning_count,
        "confirmed_violations": user.confirmed_violations,
    }
    if role in {"administrator", "superadmin"}:
        data["telegram_id"] = user.telegram_id
    return data


def _report_out(row: Report) -> dict:
    return {
        "id": row.id,
        "reporter_id": row.reporter_id,
        "target_user_id": row.target_user_id,
        "reason": row.reason,
        "snapshot": row.snapshot,
        "status": row.status,
        "details": row.details,
        "resolution": row.resolution,
    }


async def _mini_user(request: Request, session: AsyncSession = Depends(db_session)) -> User:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(401, "unauthorized")
    try:
        payload = decode_token(header.split(" ", 1)[1], "miniapp")
    except AuthError as exc:
        raise HTTPException(401, "unauthorized") from exc
    user = await get_user(session, int(payload["sub"]))
    if user is None:
        raise HTTPException(401, "unauthorized")
    return user


app = create_app()


if __name__ == "__main__":
    import uvicorn

    runtime = get_settings()
    uvicorn.run(app, host=runtime.api_host, port=runtime.api_port)
