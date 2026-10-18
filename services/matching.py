from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.clock import as_utc, utcnow
from core.i18n import t
from database.models import (
    ChatParticipant,
    ChatSession,
    Profile,
    RecentMatch,
    User,
    UserBlock,
    UserPreference,
)
from database.repositories.users import get_prefs, get_profile, get_user, interest_keys
from services.notify import enqueue
from services.settings_store import get_int_setting
from core.errors import InsufficientFunds
from services.vip import is_vip
from services.wallet import SEARCH_FEE, apply

_locks: dict[int, asyncio.Lock] = {}


def _lock(user_id: int) -> asyncio.Lock:
    lock = _locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[user_id] = lock
    return lock


class ClaimFailed(Exception):
    pass


@dataclass
class MatchResult:
    status: str
    session_id: str | None = None
    reason: str = ""


def compatibility_score(
    *,
    interests_a: set[str],
    interests_b: set[str],
    language_a: str,
    language_b: str,
    age_a: int,
    age_b: int,
    country_a: str | None,
    country_b: str | None,
    city_a: str | None,
    city_b: str | None,
    use_history: bool,
    successful_overlap: int = 0,
) -> tuple[int, list[str]]:
    """Transparent point score. It is not a psychological assessment."""
    shared = sorted(interests_a & interests_b)
    score = 10 * len(shared)
    if language_a == language_b:
        score += 20
    score += max(0, 15 - abs(age_a - age_b))
    if city_a and city_a == city_b:
        score += 8
    elif country_a and country_a == country_b:
        score += 4
    if use_history and successful_overlap:
        score += min(10, successful_overlap)
    return score, shared


def opposite(gender: str | None) -> str | None:
    if gender == "male":
        return "female"
    if gender == "female":
        return "male"
    return None


async def _blocked_ids(session: AsyncSession, user_id: int, candidate_ids: list[int]) -> set[int]:
    if not candidate_ids:
        return set()
    rows = await session.execute(
        select(UserBlock.blocker_id, UserBlock.blocked_id).where(
            or_(
                and_(UserBlock.blocker_id == user_id, UserBlock.blocked_id.in_(candidate_ids)),
                and_(UserBlock.blocked_id == user_id, UserBlock.blocker_id.in_(candidate_ids)),
            )
        )
    )
    blocked: set[int] = set()
    for blocker_id, blocked_id in rows.all():
        blocked.add(blocked_id if blocker_id == user_id else blocker_id)
    return blocked


async def _recent_ids(session: AsyncSession, user_id: int, hours: int) -> set[int]:
    cutoff = utcnow().timestamp() - hours * 3600
    rows = await session.execute(
        select(RecentMatch).where(or_(RecentMatch.user_low_id == user_id, RecentMatch.user_high_id == user_id))
    )
    found: set[int] = set()
    for row in rows.scalars().all():
        created = as_utc(row.created_at)
        if created and created.timestamp() >= cutoff:
            found.add(row.user_high_id if row.user_low_id == user_id else row.user_low_id)
    return found


async def _claim(session: AsyncSession, user_id: int, session_id: str, allow_idle: bool) -> bool:
    states = ["idle", "searching"] if allow_idle else ["searching"]
    result = await session.execute(
        update(User)
        .where(
            User.id == user_id,
            User.presence_state.in_(states),
            User.active_session_id.is_(None),
            User.status == "active",
        )
        .values(presence_state="in_chat", active_session_id=session_id, search_started_at=None)
    )
    return result.rowcount == 1


async def find_partner(session: AsyncSession, user_id: int) -> MatchResult:
    async with _lock(user_id):
        return await _find_partner(session, user_id)


async def _find_partner(session: AsyncSession, user_id: int) -> MatchResult:
    me = await get_user(session, user_id)
    if me is None:
        return MatchResult("denied", reason="missing")
    if me.underage_blocked:
        return MatchResult("denied", reason="underage")
    if me.status == "banned":
        return MatchResult("denied", reason="banned")
    if me.status == "deleted" or not me.registration_completed:
        return MatchResult("denied", reason="incomplete")
    if me.is_search_paused:
        return MatchResult("denied", reason="paused")
    if me.is_hidden:
        return MatchResult("denied", reason="hidden")
    now = utcnow()
    restricted = as_utc(me.restriction_until)
    if me.status == "restricted" and restricted and restricted > now:
        return MatchResult("denied", reason="restricted")
    if me.presence_state == "in_chat" and me.active_session_id:
        return MatchResult("already", session_id=me.active_session_id)
    profile = await get_profile(session, user_id)
    prefs = await get_prefs(session, user_id)
    if not profile or not prefs or not profile.gender or not profile.age or profile.age < 18:
        return MatchResult("denied", reason="incomplete")
    target = opposite(profile.gender)
    if target is None:
        return MatchResult("denied", reason="incomplete")

    price = 0
    if (prefs.same_region_only or prefs.same_language_only) and not await is_vip(session, user_id):
        price = await get_int_setting(session, "advanced_search_price")
        from database.repositories.users import get_wallet

        account = await get_wallet(session, user_id)
        if price > 0 and (account is None or account.balance < price):
            return MatchResult("denied", reason="need_coins")

    timeout = await get_int_setting(session, "search_timeout_sec")
    recent_hours = await get_int_setting(session, "recent_match_hours")
    my_interests = set(await interest_keys(session, user_id))
    recent = await _recent_ids(session, user_id, recent_hours)
    rows = await session.execute(
        select(User, Profile, UserPreference)
        .join(Profile, Profile.user_id == User.id)
        .join(UserPreference, UserPreference.user_id == User.id)
        .where(
            User.presence_state == "searching",
            User.id != user_id,
            User.status == "active",
            User.registration_completed.is_(True),
            User.is_hidden.is_(False),
            User.is_search_paused.is_(False),
            User.underage_blocked.is_(False),
            User.active_session_id.is_(None),
            Profile.gender == target,
            Profile.age.is_not(None),
        )
    )
    candidates = []
    raw = list(rows.all())
    blocked = await _blocked_ids(session, user_id, [user.id for user, _, _ in raw])
    for other, other_profile, other_prefs in raw:
        if other.id in blocked or other.id in recent:
            continue
        started = as_utc(other.search_started_at)
        if started is None or (now - started).total_seconds() > timeout:
            continue
        if other_profile.age is None:
            continue
        if not (prefs.age_min <= other_profile.age <= prefs.age_max):
            continue
        if not (other_prefs.age_min <= profile.age <= other_prefs.age_max):
            continue
        if (prefs.same_language_only or other_prefs.same_language_only) and me.language != other.language:
            continue
        if prefs.same_region_only or other_prefs.same_region_only:
            if not profile.country or profile.country != other_profile.country:
                continue
        their_interests = set(await interest_keys(session, other.id))
        use_history = bool(prefs.allow_stats_for_matching and other_prefs.allow_stats_for_matching)
        score, shared = compatibility_score(
            interests_a=my_interests,
            interests_b=their_interests,
            language_a=me.language,
            language_b=other.language,
            age_a=profile.age,
            age_b=other_profile.age,
            country_a=profile.country,
            country_b=other_profile.country,
            city_a=profile.city,
            city_b=other_profile.city,
            use_history=use_history,
        )
        vip_tie = 1 if await is_vip(session, other.id) else 0
        candidates.append((score, vip_tie, other, other_profile, shared))
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)

    for score, _, other, other_profile, shared in candidates:
        session_id = str(uuid.uuid4())
        try:
            async with session.begin_nested():
                order = sorted([user_id, other.id])
                for uid in order:
                    if not await _claim(session, uid, session_id, allow_idle=(uid == user_id)):
                        raise ClaimFailed()
                chat = ChatSession(
                    id=session_id,
                    status="active",
                    compatibility_score=score,
                    shared_interests=",".join(shared),
                    started_at=now,
                    last_activity_at=now,
                )
                session.add(chat)
                session.add(ChatParticipant(session_id=session_id, user_id=user_id))
                session.add(ChatParticipant(session_id=session_id, user_id=other.id))
                low, high = order
                session.add(RecentMatch(user_low_id=low, user_high_id=high, session_id=session_id, created_at=now))
                if price > 0:
                    await apply(
                        session,
                        user_id,
                        -price,
                        SEARCH_FEE,
                        source="search_filter",
                        idempotency_key=f"search:{session_id}:{user_id}",
                        meta={"session_id": session_id},
                    )
                await session.flush()
        except ClaimFailed:
            continue
        except InsufficientFunds:
            return MatchResult("denied", reason="need_coins")
        await _notify_match(session, me, profile, other, other_profile, session_id, score, shared)
        return MatchResult("found", session_id=session_id)

    await session.execute(
        update(User)
        .where(User.id == user_id, User.presence_state.in_(["idle", "searching"]), User.active_session_id.is_(None))
        .values(presence_state="searching", search_started_at=now)
    )
    return MatchResult("queued")


async def _notify_match(session, me, my_profile, other, other_profile, session_id, score, shared) -> None:
    from core.i18n import t as translate

    from services.questions import question_of_day_key

    qotd_key = question_of_day_key()
    for viewer, partner_profile in ((me, other_profile), (other, my_profile)):
        shared_labels = ", ".join(translate(viewer.language, f"interest.{key}") for key in shared) or translate(
            viewer.language, "chat.no_common"
        )
        text = translate(
            viewer.language,
            "search.found",
            nickname=partner_profile.nickname or "—",
            age=partner_profile.age or "—",
            score=score,
            shared=shared_labels,
            qotd=translate(viewer.language, qotd_key),
        )
        await enqueue(session, viewer, text, template="chat_panel", payload={"session_id": session_id})


async def cancel_search(session: AsyncSession, user_id: int) -> bool:
    result = await session.execute(
        update(User)
        .where(User.id == user_id, User.presence_state == "searching")
        .values(presence_state="idle", search_started_at=None)
    )
    return result.rowcount == 1


async def load_session_partner(session: AsyncSession, user_id: int, session_id: str) -> User | None:
    rows = await session.execute(select(ChatParticipant.user_id).where(ChatParticipant.session_id == session_id))
    ids = [item for item in rows.scalars().all() if item != user_id]
    if not ids:
        return None
    return await get_user(session, ids[0])


async def connect_pair(session: AsyncSession, first_id: int, second_id: int) -> MatchResult:
    """Open a direct session after both people agreed to meet again."""
    if first_id == second_id:
        return MatchResult("denied", reason="incomplete")
    users = []
    profiles = []
    for uid in (first_id, second_id):
        user = await get_user(session, uid)
        profile = await get_profile(session, uid)
        if not user or not profile or user.status != "active" or user.underage_blocked:
            return MatchResult("denied", reason="incomplete")
        if user.presence_state == "in_chat":
            return MatchResult("already", session_id=user.active_session_id)
        users.append(user)
        profiles.append(profile)
    if opposite(profiles[0].gender) != profiles[1].gender:
        return MatchResult("denied", reason="incomplete")
    blocked = await _blocked_ids(session, first_id, [second_id])
    if second_id in blocked:
        return MatchResult("denied", reason="blocked")
    now = utcnow()
    session_id = str(uuid.uuid4())
    keys_a = set(await interest_keys(session, first_id))
    keys_b = set(await interest_keys(session, second_id))
    score, shared = compatibility_score(
        interests_a=keys_a,
        interests_b=keys_b,
        language_a=users[0].language,
        language_b=users[1].language,
        age_a=profiles[0].age or 18,
        age_b=profiles[1].age or 18,
        country_a=profiles[0].country,
        country_b=profiles[1].country,
        city_a=profiles[0].city,
        city_b=profiles[1].city,
        use_history=False,
    )
    try:
        async with session.begin_nested():
            for uid in sorted((first_id, second_id)):
                result = await session.execute(
                    update(User)
                    .where(
                        User.id == uid,
                        User.presence_state == "idle",
                        User.active_session_id.is_(None),
                        User.status == "active",
                    )
                    .values(presence_state="in_chat", active_session_id=session_id)
                )
                if result.rowcount != 1:
                    raise ClaimFailed()
            session.add(
                ChatSession(
                    id=session_id,
                    status="active",
                    compatibility_score=score,
                    shared_interests=",".join(shared),
                    started_at=now,
                    last_activity_at=now,
                )
            )
            session.add(ChatParticipant(session_id=session_id, user_id=first_id))
            session.add(ChatParticipant(session_id=session_id, user_id=second_id))
            low, high = sorted((first_id, second_id))
            session.add(RecentMatch(user_low_id=low, user_high_id=high, session_id=session_id, created_at=now))
            await session.flush()
    except ClaimFailed:
        return MatchResult("denied", reason="already")
    await _notify_match(session, users[0], profiles[0], users[1], profiles[1], session_id, score, shared)
    return MatchResult("found", session_id=session_id)
