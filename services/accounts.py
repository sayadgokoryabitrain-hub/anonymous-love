from __future__ import annotations

import re
from datetime import date

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.clock import utcnow
from core.content_policy import _bad_url, looks_like_link
from core.errors import AppError, NotVerified
from core.i18n import t
from core.security import identity_hash, new_referral_code
from database.models import (
    ChatMessage,
    Favorite,
    Profile,
    QotdAnswer,
    RematchChoice,
    RevealRequest,
    User,
    UserInterest,
    UserPreference,
    VerificationRequest,
)
from database.repositories.users import get_by_hash, get_by_referral_code, get_by_telegram, get_prefs, get_profile, interest_keys
from services.chat import end_session
from services.notify import enqueue
from services.referrals import attach, qualify
from services.settings_store import get_int_setting
from services.trust import refresh_trust, trust_level
from services.vip import is_vip
from services.wallet import DAILY_BONUS, REGISTRATION_BONUS, apply, get_transaction

NICK_RE = re.compile(r"^[\w][\w \-]{1,22}[\w]$", re.UNICODE)
PLACE_RE = re.compile(r"^[\w][\w \-'.]{0,38}[\w.]$", re.UNICODE)


async def open_user(session: AsyncSession, telegram_id: int, username: str | None, ref_code: str | None) -> User:
    now = utcnow()
    user = await get_by_telegram(session, telegram_id)
    if user:
        user.telegram_username = username
        user.last_seen_at = now
        return user
    digest = identity_hash(telegram_id)
    user = await get_by_hash(session, digest)
    if user:
        user.telegram_id = telegram_id
        user.telegram_username = username
        user.last_seen_at = now
        if user.status == "deleted":
            user.status = "active"
            user.deleted_at = None
            user.registration_completed = False
            user.registration_step = "blocked" if user.underage_blocked else "age"
            user.age_confirmed = False
            user.rules_accepted = False
            user.language_chosen = False
            user.is_hidden = False
        return user
    code = new_referral_code()
    while await get_by_referral_code(session, code):
        code = new_referral_code()
    user = User(
        telegram_id=telegram_id,
        telegram_id_hash=digest,
        telegram_username=username,
        referral_code=code,
        last_seen_at=now,
        registration_step="age",
    )
    session.add(user)
    await session.flush()
    session.add(Profile(user_id=user.id))
    session.add(UserPreference(user_id=user.id))
    from services.wallet import get_or_create_account

    await get_or_create_account(session, user.id)
    if ref_code:
        inviter = await get_by_referral_code(session, ref_code.strip().upper())
        if inviter and inviter.id != user.id:
            await attach(session, user, inviter)
    await session.flush()
    return user


async def confirm_age(session: AsyncSession, user: User, adult: bool) -> None:
    if user.underage_blocked:
        raise AppError("underage", "age.rejected")
    if not adult:
        user.registration_step = "underage_confirm"
        return
    user.age_confirmed = True
    user.registration_step = "rules"
    user.updated_at = utcnow()


async def confirm_underage(session: AsyncSession, user: User) -> None:
    user.underage_blocked = True
    user.status = "restricted"
    user.registration_step = "blocked"
    user.restriction_reason = "underage"
    user.updated_at = utcnow()


async def accept_rules(session: AsyncSession, user: User, accepted: bool) -> None:
    if not accepted:
        raise AppError("rules_declined", "rules.declined")
    user.rules_accepted = True
    user.registration_step = "language"
    user.updated_at = utcnow()


async def set_language(session: AsyncSession, user: User, language: str) -> None:
    if language not in {"ru", "hy", "en"}:
        raise AppError("bad_language", "errors.generic")
    user.language = language
    user.language_chosen = True
    if user.registration_step == "language":
        user.registration_step = "gender"
    user.updated_at = utcnow()


async def set_gender(session: AsyncSession, user: User, gender: str) -> None:
    if gender not in {"male", "female"}:
        raise AppError("bad_gender", "errors.generic")
    profile = await _profile(session, user.id)
    profile.gender = gender
    if user.registration_step == "gender":
        user.registration_step = "user_age"
    user.updated_at = utcnow()


async def set_age(session: AsyncSession, user: User, age: int) -> None:
    if age < 18 or age > 99:
        raise AppError("bad_age", "user_age.invalid")
    profile = await _profile(session, user.id)
    profile.age = age
    prefs = await _prefs(session, user.id)
    if user.registration_step == "user_age":
        prefs.age_min = max(18, age - 5)
        prefs.age_max = min(99, age + 7)
        user.registration_step = "country"
    refresh_trust(user)


async def set_place(session: AsyncSession, user: User, field: str, value: str | None) -> None:
    profile = await _profile(session, user.id)
    if value:
        cleaned = " ".join(value.split())
        if not PLACE_RE.match(cleaned):
            raise AppError("bad_place", "errors.generic")
        value = cleaned
    if field == "country":
        profile.country = value
        profile.country_set = True
        if user.registration_step == "country":
            user.registration_step = "city"
    elif field == "city":
        profile.city = value
        profile.city_set = True
        if user.registration_step == "city":
            user.registration_step = "interests"
    else:
        raise AppError("bad_field", "errors.generic")


async def toggle_interest(session: AsyncSession, user: User, interest_id: int) -> int:
    from database.models import Interest

    interest = await session.get(Interest, interest_id)
    if interest is None:
        raise AppError("bad_interest", "errors.generic")
    existing = await session.scalar(
        select(UserInterest).where(UserInterest.user_id == user.id, UserInterest.interest_id == interest_id)
    )
    if existing:
        await session.delete(existing)
    else:
        limit = 9 if await is_vip(session, user.id) else await get_int_setting(session, "max_free_interests")
        current = await interest_keys(session, user.id)
        if len(current) >= limit:
            raise AppError("interest_limit", "interests.limit", limit=limit)
        session.add(UserInterest(user_id=user.id, interest_id=interest_id))
    await session.flush()
    return len(await interest_keys(session, user.id))


async def finish_interests(session: AsyncSession, user: User) -> None:
    count = len(await interest_keys(session, user.id))
    if count < 3:
        raise AppError("need_interests", "interests.need")
    if user.registration_step == "interests":
        user.registration_step = "nickname"


async def set_nickname(session: AsyncSession, user: User, nickname: str) -> None:
    cleaned = " ".join((nickname or "").split())
    if not NICK_RE.match(cleaned) or looks_like_link(cleaned) or "@" in cleaned:
        raise AppError("bad_nick", "nickname.invalid")
    folded = cleaned.casefold()
    from database.models import Profile as ProfileModel

    taken = await session.scalar(
        select(ProfileModel.user_id).where(ProfileModel.nickname_folded == folded, ProfileModel.user_id != user.id)
    )
    if taken:
        raise AppError("nick_taken", "nickname.taken")
    profile = await _profile(session, user.id)
    profile.nickname = cleaned
    profile.nickname_folded = folded
    if user.registration_step == "nickname":
        user.registration_step = "bio"


async def set_bio(session: AsyncSession, user: User, bio: str) -> None:
    cleaned = (bio or "").strip()
    if not 1 <= len(cleaned) <= 300:
        raise AppError("bad_bio", "bio.invalid")
    if looks_like_link(cleaned) or "@" in cleaned:
        if user.verification_status != "approved" or _bad_url(cleaned):
            raise AppError("bio_link", "bio.link_blocked")
    profile = await _profile(session, user.id)
    profile.bio = cleaned
    if user.registration_step == "bio":
        user.registration_step = "age_range"


async def set_age_range(session: AsyncSession, user: User, age_min: int, age_max: int) -> None:
    if age_min < 18 or age_max > 99 or age_min > age_max:
        raise AppError("bad_range", "user_age.invalid")
    prefs = await _prefs(session, user.id)
    prefs.age_min = age_min
    prefs.age_max = age_max
    if user.registration_step == "age_range":
        user.registration_step = "search_prefs"


async def set_search_prefs(session: AsyncSession, user: User, mode: str) -> int:
    if mode not in {"any", "language", "country", "both"}:
        raise AppError("bad_prefs", "errors.generic")
    prefs = await _prefs(session, user.id)
    prefs.same_language_only = mode in {"language", "both"}
    prefs.same_region_only = mode in {"country", "both"}
    prefs.prefs_set = True
    if user.registration_step == "search_prefs":
        return await complete_registration(session, user)
    return 0


async def complete_registration(session: AsyncSession, user: User) -> int:
    profile = await _profile(session, user.id)
    prefs = await _prefs(session, user.id)
    count = len(await interest_keys(session, user.id))
    if not (
        user.age_confirmed
        and user.rules_accepted
        and user.language_chosen
        and profile.gender in {"male", "female"}
        and profile.age
        and profile.age >= 18
        and profile.country_set
        and profile.city_set
        and count >= 3
        and profile.nickname
        and profile.bio
        and prefs.prefs_set
    ):
        raise AppError("incomplete", "search.incomplete")
    user.registration_completed = True
    user.registration_step = "done"
    user.updated_at = utcnow()
    refresh_trust(user)
    granted = 0
    if not user.registration_bonus_granted:
        amount = await get_int_setting(session, "registration_bonus")
        await apply(
            session,
            user.id,
            amount,
            REGISTRATION_BONUS,
            source="registration",
            idempotency_key=f"registration:{user.id}",
        )
        user.registration_bonus_granted = True
        granted = amount
    if user.verification_status == "approved":
        await qualify(session, user)
    return granted


async def claim_daily(session: AsyncSession, user: User) -> tuple[bool, int]:
    from services.vip import is_vip as vip_active

    day = utcnow().date().isoformat()
    key = f"daily:{user.id}:{day}"
    if await get_transaction(session, key):
        return False, 0
    amount = await get_int_setting(session, "daily_bonus")
    if await vip_active(session, user.id):
        amount += await get_int_setting(session, "vip_daily_extra")
    await apply(session, user.id, amount, DAILY_BONUS, source="daily", idempotency_key=key)
    return True, amount


async def delete_account(session: AsyncSession, user: User) -> None:
    if user.active_session_id:
        await end_session(session, user.active_session_id, user.id, "delete")
    now = utcnow()
    user.status = "deleted"
    user.deleted_at = now
    user.telegram_id = None
    user.telegram_username = None
    user.registration_completed = False
    user.presence_state = "idle"
    user.active_session_id = None
    user.is_hidden = True
    profile = await get_profile(session, user.id)
    if profile:
        profile.nickname = None
        profile.nickname_folded = None
        profile.bio = None
        profile.country = None
        profile.city = None
        profile.gender = None
        profile.age = None
        profile.country_set = False
        profile.city_set = False
    await session.execute(delete(UserInterest).where(UserInterest.user_id == user.id))
    await session.execute(
        delete(ChatMessage).where(ChatMessage.sender_id == user.id, ChatMessage.retained_for_report.is_(False))
    )
    await session.execute(
        delete(Favorite).where((Favorite.user_id == user.id) | (Favorite.favorite_user_id == user.id))
    )
    requests = (
        await session.execute(select(VerificationRequest).where(VerificationRequest.user_id == user.id))
    ).scalars().all()
    for request in requests:
        request.evidence_file_id = None
    user.updated_at = now


async def request_reveal(session: AsyncSession, user: User) -> RevealRequest:
    if user.verification_status != "approved":
        raise NotVerified()
    prefs = await _prefs(session, user.id)
    if not prefs.allow_profile_reveal:
        raise AppError("reveal_off", "reveal.disabled")
    if not user.active_session_id:
        raise AppError("no_chat", "chat.not_in_chat")
    from services.matching import load_session_partner

    partner = await load_session_partner(session, user.id, user.active_session_id)
    if partner is None:
        raise AppError("no_chat", "chat.not_in_chat")
    if partner.verification_status != "approved":
        raise NotVerified()
    existing = await session.scalar(
        select(RevealRequest).where(
            RevealRequest.session_id == user.active_session_id,
            RevealRequest.status.in_(["pending", "accepted"]),
            RevealRequest.requester_id == user.id,
            RevealRequest.target_id == partner.id,
        )
    )
    if existing:
        return existing
    request = RevealRequest(
        session_id=user.active_session_id,
        requester_id=user.id,
        target_id=partner.id,
        status="pending",
    )
    session.add(request)
    await session.flush()
    await enqueue(
        session,
        partner,
        t(partner.language, "reveal.incoming"),
        template="reveal_prompt",
        payload={"request_id": request.id},
    )
    return request


async def answer_reveal(session: AsyncSession, user: User, request_id: int, accept: bool) -> RevealRequest:
    request = await session.get(RevealRequest, request_id)
    if request is None or request.target_id != user.id or request.status != "pending":
        raise AppError("missing", "errors.not_found")
    request.resolved_at = utcnow()
    requester = await session.get(User, request.requester_id)
    if not accept:
        request.status = "declined"
        if requester:
            await enqueue(session, requester, t(requester.language, "reveal.declined"))
        return request
    target_prefs = await _prefs(session, user.id)
    if not target_prefs.allow_profile_reveal or user.verification_status != "approved":
        raise NotVerified()
    request.status = "accepted"
    request.target_sharing = True
    if requester:
        await enqueue(session, requester, await public_card(session, user, requester.language))
        await enqueue(session, user, await public_card(session, requester, user.language))
    return request


async def revoke_reveal(session: AsyncSession, user: User, request_id: int) -> None:
    request = await session.get(RevealRequest, request_id)
    if request is None or user.id not in {request.requester_id, request.target_id}:
        raise AppError("missing", "errors.not_found")
    request.status = "revoked"
    request.requester_sharing = False
    request.target_sharing = False
    other_id = request.target_id if user.id == request.requester_id else request.requester_id
    other = await session.get(User, other_id)
    if other:
        await enqueue(session, other, t(other.language, "reveal.revoked"))


async def public_card(session: AsyncSession, user: User, lang: str) -> str:
    profile = await _profile(session, user.id)
    prefs = await _prefs(session, user.id)
    keys = await interest_keys(session, user.id)
    labels = ", ".join(t(lang, f"interest.{key}") for key in keys) or "—"
    badge = "✅ " if user.verification_status == "approved" else ""
    gender = t(lang, f"gender.{profile.gender}_noun") if profile.gender else "—"
    location = ", ".join(part for part in (profile.city, profile.country) if part) or t(lang, "profile.location_unknown")
    username_line = t(lang, "reveal.no_username")
    if (
        user.verification_status == "approved"
        and prefs.allow_username_reveal
        and user.telegram_username
    ):
        username_line = t(lang, "reveal.username", username=user.telegram_username)
    level = t(lang, f"trust.{trust_level(user.trust_score)}")
    return t(
        lang,
        "reveal.card",
        badge=badge,
        nickname=profile.nickname or "—",
        gender=gender,
        age=profile.age or "—",
        location=location,
        bio=profile.bio or "—",
        interests=labels,
        trust=user.trust_score,
        trust_level=level,
        username_line=username_line,
    )


async def ask_favorite(session: AsyncSession, user: User) -> None:
    if not user.active_session_id:
        raise AppError("no_chat", "chat.not_in_chat")
    from services.matching import load_session_partner

    partner = await load_session_partner(session, user.id, user.active_session_id)
    if partner is None:
        raise AppError("no_chat", "chat.not_in_chat")
    existing = await session.scalar(
        select(Favorite).where(Favorite.user_id == user.id, Favorite.favorite_user_id == partner.id)
    )
    if existing is None:
        session.add(Favorite(user_id=user.id, favorite_user_id=partner.id, mutual=False))
    await enqueue(
        session,
        partner,
        t(partner.language, "favorites.incoming"),
        template="favorite_prompt",
        payload={"user_id": user.id},
    )


async def accept_favorite(session: AsyncSession, user: User, other_id: int) -> None:
    incoming = await session.scalar(
        select(Favorite).where(Favorite.user_id == other_id, Favorite.favorite_user_id == user.id)
    )
    if incoming is None:
        raise AppError("missing", "errors.not_found")
    incoming.mutual = True
    back = await session.scalar(select(Favorite).where(Favorite.user_id == user.id, Favorite.favorite_user_id == other_id))
    if back is None:
        session.add(Favorite(user_id=user.id, favorite_user_id=other_id, mutual=True))
    else:
        back.mutual = True
    other = await session.get(User, other_id)
    if other:
        await enqueue(session, other, t(other.language, "favorites.added"))
    await enqueue(session, user, t(user.language, "favorites.added"))


async def choose_rematch(session: AsyncSession, user: User, session_id: str, choice: str) -> str:
    if choice not in {"yes", "no"}:
        raise AppError("bad_choice", "errors.generic")
    from database.models import ChatParticipant

    member = await session.scalar(
        select(ChatParticipant).where(ChatParticipant.session_id == session_id, ChatParticipant.user_id == user.id)
    )
    if member is None:
        raise AppError("missing", "errors.not_found")
    row = await session.scalar(
        select(RematchChoice).where(RematchChoice.session_id == session_id, RematchChoice.user_id == user.id)
    )
    if row is None:
        row = RematchChoice(session_id=session_id, user_id=user.id, choice=choice)
        session.add(row)
    else:
        row.choice = choice
    await session.flush()
    if choice == "no":
        return "no"
    other = await session.scalar(
        select(RematchChoice).where(
            RematchChoice.session_id == session_id,
            RematchChoice.user_id != user.id,
            RematchChoice.choice == "yes",
        )
    )
    if other is None:
        return "wait"
    partner_id = other.user_id
    partner = await session.get(User, partner_id)
    await enqueue(
        session,
        user,
        t(user.language, "rematch.both"),
        template="rematch_open",
        payload={"session_id": session_id, "partner_id": partner_id},
    )
    if partner:
        await enqueue(
            session,
            partner,
            t(partner.language, "rematch.both"),
            template="rematch_open",
            payload={"session_id": session_id, "partner_id": user.id},
        )
    return "mutual"


async def record_qotd(session: AsyncSession, user: User, question_key: str) -> bool:
    today: date = utcnow().date()
    existing = await session.scalar(
        select(QotdAnswer).where(QotdAnswer.user_id == user.id, QotdAnswer.answered_on == today)
    )
    if existing:
        return False
    session.add(QotdAnswer(user_id=user.id, question_key=question_key, answered_on=today))
    return True


async def _profile(session: AsyncSession, user_id: int) -> Profile:
    profile = await get_profile(session, user_id)
    if profile is None:
        profile = Profile(user_id=user_id)
        session.add(profile)
        await session.flush()
    return profile


async def _prefs(session: AsyncSession, user_id: int) -> UserPreference:
    prefs = await get_prefs(session, user_id)
    if prefs is None:
        prefs = UserPreference(user_id=user_id)
        session.add(prefs)
        await session.flush()
    return prefs
