from __future__ import annotations

import asyncio
import hashlib
from io import BytesIO

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards.common import back_menu, chat_keyboard, main_menu, verify_keyboard
from bot.states import Form
from core.clock import utcnow
from core.config import get_settings
from core.errors import AppError, InsufficientFunds
from core.i18n import t
from database.models import CoinPackage, Favorite, Interest, User, VipPlan
from database.repositories.users import get_prefs, get_profile, interest_keys
from services.accounts import (
    accept_favorite,
    accept_rules,
    answer_reveal,
    ask_favorite,
    choose_rematch,
    claim_daily,
    confirm_age,
    confirm_underage,
    delete_account,
    finish_interests,
    open_user,
    record_qotd,
    request_reveal,
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
from services.chat import common_interest_text, end_session, relay
from services.engagement import (
    ONCE_CODES,
    answer_game,
    claim_task,
    daily_codes,
    list_gifts,
    list_tasks,
    redeem_promo,
    send_gift,
    task_claimed,
    task_ready,
)
from services.matching import cancel_search, connect_pair, find_partner, load_session_partner
from services.moderation import block_user, create_report, submit_appeal
from services.notify import enqueue
from services.payments import (
    attach_crypto_invoice,
    confirm,
    create_coin_order,
    create_vip_order,
    open_channels,
    pre_checkout_ok,
    quote_for_stars,
    recent_crypto_order,
)
from services.questions import question_of_day_key
from services.referrals import stats as referral_stats
from services.trust import trust_level
from services.verification import latest_request, start_external, start_manual, submit_photo, submit_video_note
from services.vip import active_until, get_plan, grant, is_vip
from services.wallet import history
from database.repositories.users import get_wallet

router = Router(name="private")
group_router = Router(name="groups")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")
group_router.message.filter(F.chat.type != "private")

DISPATCH: dict = {}
_CLICKS: dict[int, asyncio.Lock] = {}


def _button(lang: str, key: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=t(lang, key), callback_data=data)


async def show(event: Message | CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    text = text[:4000]
    if isinstance(event, CallbackQuery):
        if event.message is not None:
            try:
                await event.message.edit_text(text, reply_markup=markup)
                return
            except TelegramBadRequest as exc:
                if "message is not modified" in str(exc).lower():
                    return
                await event.message.answer(text, reply_markup=markup)
                return
        return
    await event.answer(text, reply_markup=markup)


async def current(session: AsyncSession, event: Message | CallbackQuery) -> User:
    username = event.from_user.username if event.from_user else None
    telegram_id = event.from_user.id
    return await open_user(session, telegram_id, username, None)


async def render_step(event, session: AsyncSession, state: FSMContext, user: User) -> None:
    lang = user.language
    step = user.registration_step
    await state.set_state(None)
    if user.underage_blocked or step == "blocked":
        await show(event, t(lang, "age.rejected"))
        return
    if step == "age":
        await show(event, t(lang, "age.prompt"), _pairs(lang, [("age.yes", "reg:age_yes"), ("age.no", "reg:age_no")]))
        return
    if step == "underage_confirm":
        await show(
            event,
            t(lang, "age.confirm_under"),
            _pairs(lang, [("age.under_yes", "reg:under_yes"), ("age.under_no", "reg:under_no")]),
        )
        return
    if step == "rules":
        await show(
            event,
            t(lang, "rules.title") + "\n\n" + t(lang, "rules.text"),
            _pairs(lang, [("rules.accept", "reg:rules_yes"), ("rules.decline", "reg:rules_no")], width=1),
        )
        return
    if step == "language":
        await show(event, t(lang, "language.prompt"), _language_kb())
        return
    if step == "gender":
        await show(event, t(lang, "gender.prompt"), _pairs(lang, [("gender.male", "gender:male"), ("gender.female", "gender:female")]))
        return
    if step == "user_age":
        await state.set_state(Form.age)
        await show(event, t(lang, "user_age.prompt"))
        return
    if step == "country":
        await state.set_state(Form.country)
        await show(event, t(lang, "country.prompt"), _pairs(lang, [("common.skip", "place:skip")]))
        return
    if step == "city":
        await state.set_state(Form.city)
        await show(event, t(lang, "city.prompt"), _pairs(lang, [("common.skip", "place:skip")]))
        return
    if step == "interests":
        await show(event, *(await _interest_view(session, user)))
        return
    if step == "nickname":
        await state.set_state(Form.nickname)
        await show(event, t(lang, "nickname.prompt"))
        return
    if step == "bio":
        await state.set_state(Form.bio)
        await show(event, t(lang, "bio.prompt"))
        return
    if step == "age_range":
        await show(event, t(lang, "age_range.prompt"), await _range_kb(session, user))
        return
    if step == "search_prefs":
        await show(event, t(lang, "search_prefs.prompt") + "\n" + t(lang, "search.filters_note"), _prefs_kb(lang))
        return
    await state.clear()
    await show(event, t(lang, "menu.title"), main_menu(lang))


def _pairs(lang: str, items: list[tuple[str, str]], width: int = 2) -> InlineKeyboardMarkup:
    buttons = [_button(lang, key, data) for key, data in items]
    rows = [buttons[i : i + width] for i in range(0, len(buttons), width)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _language_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Русский", callback_data="lang:ru"),
                InlineKeyboardButton(text="Հայերեն", callback_data="lang:hy"),
                InlineKeyboardButton(text="English", callback_data="lang:en"),
            ]
        ]
    )


def _prefs_kb(lang: str) -> InlineKeyboardMarkup:
    return _pairs(
        lang,
        [
            ("search_prefs.any", "prefs:any"),
            ("search_prefs.language", "prefs:language"),
            ("search_prefs.country", "prefs:country"),
            ("search_prefs.both", "prefs:both"),
        ],
        width=1,
    )


async def _range_kb(session: AsyncSession, user: User) -> InlineKeyboardMarkup:
    profile = await get_profile(session, user.id)
    age = profile.age if profile and profile.age else 25
    low = max(18, age - 5)
    high = min(99, age + 7)
    lang = user.language
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t(lang, "age_range.around"), callback_data=f"range:{low}:{high}")],
            [
                InlineKeyboardButton(text="18–25", callback_data="range:18:25"),
                InlineKeyboardButton(text="21–30", callback_data="range:21:30"),
            ],
            [
                InlineKeyboardButton(text="25–35", callback_data="range:25:35"),
                InlineKeyboardButton(text="30–45", callback_data="range:30:45"),
            ],
            [InlineKeyboardButton(text=t(lang, "age_range.all"), callback_data="range:18:99")],
        ]
    )


async def _interest_view(session: AsyncSession, user: User) -> tuple[str, InlineKeyboardMarkup]:
    from services.settings_store import get_int_setting

    selected = set(await interest_keys(session, user.id))
    limit = 9 if await is_vip(session, user.id) else await get_int_setting(session, "max_free_interests")
    rows_db = (await session.execute(select(Interest).order_by(Interest.sort_order))).scalars().all()
    buttons = []
    for item in rows_db:
        mark = "✅ " if item.key in selected else ""
        buttons.append(
            InlineKeyboardButton(text=f"{mark}{t(user.language, f'interest.{item.key}')}", callback_data=f"interest:{item.id}")
        )
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append([_button(user.language, "interests.done", "interest:done")])
    if user.registration_completed:
        rows.append([_button(user.language, "common.back", "menu:settings")])
    text = t(user.language, "interests.prompt", count=len(selected), limit=limit)
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _ready(event, session, state, user: User) -> bool:
    if user.underage_blocked:
        await show(event, t(user.language, "age.rejected"))
        return False
    if user.status == "banned":
        await show(
            event,
            t(user.language, "errors.banned", reason=user.restriction_reason or "—"),
            _pairs(user.language, [("appeal.button", "appeal:ask")], width=1),
        )
        return False
    if not user.registration_completed:
        await render_step(event, session, state, user)
        return False
    return True


async def render_home(event, session, state, user: User) -> None:
    await state.clear()
    if user.registration_completed and user.status != "banned" and not user.underage_blocked:
        await show(event, t(user.language, "menu.title"), main_menu(user.language))
        return
    await render_step(event, session, state, user)


async def render_profile(event, session, user: User) -> None:
    profile = await get_profile(session, user.id)
    keys = await interest_keys(session, user.id)
    labels = ", ".join(t(user.language, f"interest.{key}") for key in keys) or "—"
    location = ", ".join(part for part in (profile.city, profile.country) if profile and part) or t(
        user.language, "profile.location_unknown"
    )
    if user.is_hidden:
        search_state = t(user.language, "profile.hidden")
    elif user.is_search_paused:
        search_state = t(user.language, "profile.paused")
    else:
        search_state = t(user.language, "profile.visible")
    text = t(
        user.language,
        "profile.own",
        badge="✅ " if user.verification_status == "approved" else "",
        nickname=profile.nickname if profile else "—",
        gender=t(user.language, f"gender.{profile.gender}_noun") if profile and profile.gender else "—",
        age=profile.age if profile else "—",
        location=location,
        bio=profile.bio if profile and profile.bio else "—",
        interests=labels,
        trust=t(user.language, "trust.line", score=user.trust_score, level=t(user.language, f"trust.{trust_level(user.trust_score)}")),
        verify=t(user.language, f"verify.{user.verification_status}"),
        search_state=search_state,
    )
    await show(event, text, back_menu(user.language))


async def render_balance(event, session, user: User) -> None:
    account = await get_wallet(session, user.id)
    amount = account.balance if account else 0
    rows = await history(session, user.id, 8)
    if rows:
        lines = "\n".join(f"{row.operation_type}: {row.amount:+d}" for row in rows)
        extra = "\n\n" + t(user.language, "wallet.history", lines=lines)
    else:
        extra = "\n\n" + t(user.language, "wallet.empty")
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [_button(user.language, "wallet.daily", "daily:claim"), _button(user.language, "wallet.promo", "promo:ask")],
            [_button(user.language, "menu.topup", "menu:topup")],
            [_button(user.language, "common.back", "menu:home")],
        ]
    )
    await show(event, t(user.language, "wallet.title", balance=amount) + extra, markup)


async def render_settings(event, session, user: User) -> None:
    lang = user.language
    await show(
        event,
        t(lang, "settings.title"),
        InlineKeyboardMarkup(
            inline_keyboard=[
                [_button(lang, "settings.edit_nick", "set:nick"), _button(lang, "settings.edit_bio", "set:bio")],
                [_button(lang, "settings.edit_city", "set:city"), _button(lang, "settings.language", "set:lang")],
                [_button(lang, "settings.interests", "set:interests"), _button(lang, "settings.age_range", "set:range")],
                [_button(lang, "settings.search", "set:filters")],
                [
                    InlineKeyboardButton(text="🙈", callback_data="set:hide"),
                    InlineKeyboardButton(text="⏸", callback_data="set:pause"),
                    InlineKeyboardButton(text="🎁", callback_data="set:gifts"),
                    InlineKeyboardButton(text="@", callback_data="set:username"),
                ],
                [_button(lang, "settings.delete", "set:delete")],
                [_button(lang, "common.back", "menu:home")],
            ]
        ),
    )


async def do_search(event, session, user: User) -> None:
    result = await find_partner(session, user.id)
    await session.refresh(user)
    if result.status in {"found", "queued"}:
        if result.status == "queued":
            await show(
                event,
                t(user.language, "search.queued"),
                _pairs(user.language, [("search.stop_queue", "search:stop"), ("common.back", "menu:home")], width=1),
            )
        return
    reason = result.reason if result.reason in {
        "paused",
        "hidden",
        "banned",
        "restricted",
        "incomplete",
        "underage",
        "need_coins",
    } else "denied"
    key = "search.already" if result.status == "already" else f"search.{reason}"
    markup = chat_keyboard(user.language) if result.status == "already" else back_menu(user.language)
    await show(event, t(user.language, key), markup)


async def render_verify(event, session, user: User) -> None:
    current_request = await latest_request(session, user.id)
    status = user.verification_status
    text = t(user.language, "verify.intro") + "\n\n" + t(user.language, f"verify.{status}")
    rows = [[_button(user.language, "verify.manual", "verify:manual")]]
    if get_settings().provider_configured:
        rows.append([_button(user.language, "verify.external", "verify:external")])
    if current_request and current_request.status == "pending":
        step = "verify.need_video" if current_request.evidence_file_id else "verify.need_photo"
        text += "\n\n" + t(user.language, step)
    rows.append([_button(user.language, "common.back", "menu:home")])
    await show(event, text, InlineKeyboardMarkup(inline_keyboard=rows))


async def render_referral(event, session, user: User) -> None:
    data = await referral_stats(session, user.id)
    username = get_settings().bot_username or "your_bot"
    link = f"https://t.me/{username}?start=ref_{user.referral_code}"
    text = "\n\n".join(
        [
            t(user.language, "referral.title"),
            t(user.language, "referral.body"),
            t(user.language, "referral.link", link=link),
            t(user.language, "referral.stats", **data),
        ]
    )
    await show(event, text, back_menu(user.language))


async def render_tasks(event, session, user: User) -> None:
    today = utcnow().date().isoformat()
    catalog = {task.code: task for task in await list_tasks(session)}
    lines = [t(user.language, "tasks.title"), t(user.language, "tasks.today", date=today)]
    buttons = []
    for code in daily_codes():
        task = catalog.get(code)
        if task is None:
            continue
        title = t(user.language, f"tasks.{task.code}")
        if await task_claimed(session, user, task):
            lines.append(f"• {title} — {t(user.language, 'tasks.done_mark')}")
            continue
        lines.append(f"• {title} — +{task.reward}")
        if await task_ready(session, user, task):
            buttons.append(InlineKeyboardButton(text=f"{title} +{task.reward}", callback_data=f"task:{task.code}"))
    once = [catalog[code] for code in ONCE_CODES if code in catalog and not await task_claimed(session, user, catalog[code])]
    if once:
        lines.append("")
        lines.append(t(user.language, "tasks.once_title"))
        for task in once:
            title = t(user.language, f"tasks.{task.code}")
            lines.append(f"• {title} — +{task.reward}")
            if await task_ready(session, user, task):
                buttons.append(InlineKeyboardButton(text=f"{title} +{task.reward}", callback_data=f"task:{task.code}"))
    buttons.append(_button(user.language, "common.back", "menu:home"))
    await show(event, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=[[button] for button in buttons]))


async def render_gifts(event, session, user: User) -> None:
    if not user.active_session_id:
        await show(event, t(user.language, "gifts.title") + "\n" + t(user.language, "chat.not_in_chat"), back_menu(user.language))
        return
    buttons = []
    for gift in await list_gifts(session):
        label = f"{gift.emoji} {gift.price}"
        buttons.append(InlineKeyboardButton(text=label, callback_data=f"gift:{gift.code}"))
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append([_button(user.language, "common.back", "menu:home")])
    await show(event, t(user.language, "gifts.title"), InlineKeyboardMarkup(inline_keyboard=rows))


async def render_favorites(event, session, user: User) -> None:
    rows = (
        await session.execute(select(Favorite).where(Favorite.user_id == user.id, Favorite.mutual.is_(True)))
    ).scalars().all()
    if not rows:
        await show(event, t(user.language, "favorites.empty"), back_menu(user.language))
        return
    lines = [t(user.language, "favorites.title")]
    for row in rows:
        other = await session.get(User, row.favorite_user_id)
        profile = await get_profile(session, row.favorite_user_id) if other else None
        if other is None or other.is_hidden or other.status != "active":
            state = t(user.language, "favorites.unavailable")
        elif other.presence_state == "in_chat":
            state = t(user.language, "favorites.in_chat")
        elif other.is_search_paused:
            state = t(user.language, "favorites.unavailable")
        else:
            state = t(user.language, "favorites.available")
        lines.append(t(user.language, "favorites.line", nickname=profile.nickname if profile else "—", state=state))
    await show(event, "\n".join(lines), back_menu(user.language))


async def render_vip(event, session, user: User) -> None:
    until = await active_until(session, user.id)
    status = t(user.language, "vip.active", until=until.strftime("%Y-%m-%d")) if until else t(user.language, "vip.inactive")
    plans = (await session.execute(select(VipPlan).where(VipPlan.active.is_(True)))).scalars().all()
    rows = []
    for plan in plans:
        rows.append(
            [
                InlineKeyboardButton(
                    text=t(user.language, "vip.buy_coins", days=plan.days, coins=plan.coin_price),
                    callback_data=f"vipcoins:{plan.code}",
                )
            ]
        )
        channels = _pay_channels()
        if "stars" in channels or "test" in channels:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=t(user.language, "vip.buy_stars", days=plan.days, stars=plan.star_price),
                        callback_data=f"vipstars:{plan.code}",
                    )
                ]
            )
        if "crypto" in channels:
            amount, asset = quote_for_stars(plan.star_price)
            rows.append(
                [
                    InlineKeyboardButton(
                        text=t(user.language, "vip.buy_crypto", days=plan.days, amount=amount, asset=asset),
                        callback_data=f"vipcrypto:{plan.code}",
                    )
                ]
            )
    rows.append([_button(user.language, "common.back", "menu:home")])
    await show(event, t(user.language, "vip.benefits") + "\n\n" + status, InlineKeyboardMarkup(inline_keyboard=rows))


def _pay_channels() -> list[str]:
    try:
        return open_channels()
    except AppError:
        return []


async def render_topup(event, session, user: User) -> None:
    settings = get_settings()
    channels = _pay_channels()
    if not channels:
        await show(event, t(user.language, "pay.disabled"), back_menu(user.language))
        return
    packages = (await session.execute(select(CoinPackage).where(CoinPackage.active.is_(True)).order_by(CoinPackage.sort_order))).scalars().all()
    rows: list[list[InlineKeyboardButton]] = []
    for package in packages:
        if "test" in channels:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=t(user.language, "pay.package", coins=package.coins, stars=package.stars),
                        callback_data=f"pay:{package.code}:stars",
                    )
                ]
            )
            continue
        if "stars" in channels:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=t(user.language, "pay.stars_btn", coins=package.coins, stars=package.stars),
                        callback_data=f"pay:{package.code}:stars",
                    )
                ]
            )
        if "crypto" in channels:
            amount, asset = quote_for_stars(package.stars)
            rows.append(
                [
                    InlineKeyboardButton(
                        text=t(user.language, "pay.crypto_btn", coins=package.coins, amount=amount, asset=asset),
                        callback_data=f"pay:{package.code}:crypto",
                    )
                ]
            )
    rows.append([_button(user.language, "common.back", "menu:home")])
    note = t(user.language, "pay.title")
    if "stars" in channels and "crypto" in channels:
        note += "\n" + t(user.language, "pay.methods")
    if settings.payment_mode == "test":
        note += "\n" + t(user.language, "pay.test")
    await show(event, note, InlineKeyboardMarkup(inline_keyboard=rows))


def _reactions(lang: str) -> InlineKeyboardMarkup:
    codes = ("heart", "fire", "laugh", "clap", "think")
    buttons = [InlineKeyboardButton(text=t(lang, f"react.{code}"), callback_data=f"react:{code}") for code in codes]
    return InlineKeyboardMarkup(inline_keyboard=[buttons, [_button(lang, "common.back", "chat:panel")]])


def _report_kb(lang: str) -> InlineKeyboardMarkup:
    return _pairs(
        lang,
        [
            ("report.spam", "report:spam"),
            ("report.abuse", "report:abuse"),
            ("report.scam", "report:scam"),
            ("report.safety", "report:safety"),
            ("report.other", "report:other"),
        ],
        width=1,
    )


async def on_menu(query, session, state, user, rest: str) -> None:
    if rest == "home":
        await render_home(query, session, state, user)
        return
    if rest == "help":
        await show(query, t(user.language, "help.text"), back_menu(user.language))
        return
    if not await _ready(query, session, state, user):
        return
    screens = {
        "profile": render_profile,
        "balance": render_balance,
        "settings": render_settings,
        "verify": render_verify,
        "referral": render_referral,
        "tasks": render_tasks,
        "gifts": render_gifts,
        "favorites": render_favorites,
        "vip": render_vip,
        "topup": render_topup,
    }
    if rest == "search":
        await do_search(query, session, user)
    elif rest == "chat":
        if user.active_session_id:
            await show(query, t(user.language, "chat.panel"), chat_keyboard(user.language))
        else:
            await show(query, t(user.language, "chat.not_in_chat"), main_menu(user.language))
    elif rest == "media":
        await show(query, t(user.language, "media.intro"), verify_keyboard(user.language))
    elif rest in screens:
        await screens[rest](query, session, user)
    else:
        await render_home(query, session, state, user)


async def on_reg(query, session, state, user, rest: str) -> None:
    if rest == "age_yes":
        await confirm_age(session, user, True)
    elif rest == "age_no":
        await confirm_age(session, user, False)
    elif rest == "under_yes":
        await confirm_underage(session, user)
    elif rest == "under_no":
        await confirm_age(session, user, True)
    elif rest == "rules_yes":
        await accept_rules(session, user, True)
    elif rest == "rules_no":
        raise AppError("rules", "rules.declined")
    await render_step(query, session, state, user)


async def on_lang(query, session, state, user, rest: str) -> None:
    await set_language(session, user, rest)
    data = await state.get_data()
    if user.registration_completed or data.get("edit") == "language":
        await state.clear()
        await show(query, t(user.language, "settings.saved"), back_menu(user.language))
        return
    await render_step(query, session, state, user)


async def on_gender(query, session, state, user, rest: str) -> None:
    await set_gender(session, user, rest)
    await render_step(query, session, state, user)


async def on_interest(query, session, state, user, rest: str) -> None:
    if rest == "done":
        if user.registration_step == "interests":
            await finish_interests(session, user)
            await render_step(query, session, state, user)
        else:
            await show(query, t(user.language, "settings.saved"), back_menu(user.language))
        return
    await toggle_interest(session, user, int(rest))
    await show(query, *(await _interest_view(session, user)))


async def on_place(query, session, state, user, rest: str) -> None:
    if rest == "skip":
        if user.registration_step == "country":
            await set_place(session, user, "country", None)
        elif user.registration_step == "city" or await state.get_state() == Form.city.state:
            await set_place(session, user, "city", None)
        await state.clear()
        if user.registration_completed and user.registration_step == "done":
            await show(query, t(user.language, "settings.saved"), back_menu(user.language))
            return
        await render_step(query, session, state, user)


async def on_range(query, session, state, user, rest: str) -> None:
    low, high = rest.split(":")
    await set_age_range(session, user, int(low), int(high))
    if user.registration_completed and user.registration_step == "done":
        await show(query, t(user.language, "settings.saved"), back_menu(user.language))
        return
    await render_step(query, session, state, user)


async def on_prefs(query, session, state, user, rest: str) -> None:
    data = await state.get_data()
    amount = await set_search_prefs(session, user, rest)
    if data.get("edit") == "filters":
        await state.clear()
        await show(query, t(user.language, "settings.saved"), back_menu(user.language))
        return
    if user.registration_completed:
        await show(query, t(user.language, "registration.done", amount=amount), main_menu(user.language))
        return
    await render_step(query, session, state, user)


async def on_search(query, session, state, user, rest: str) -> None:
    if rest == "stop":
        await cancel_search(session, user.id)
        await session.refresh(user)
        await show(query, t(user.language, "settings.saved"), main_menu(user.language))


async def on_chat(query, session, state, user, rest: str) -> None:
    if not await _ready(query, session, state, user):
        return
    if rest == "panel":
        await show(query, t(user.language, "chat.panel"), chat_keyboard(user.language))
    elif rest == "help":
        await show(query, t(user.language, "chat.help_text"), chat_keyboard(user.language))
    elif rest == "interests":
        await show(query, await common_interest_text(session, user), chat_keyboard(user.language))
    elif rest == "stop":
        if user.active_session_id:
            await end_session(session, user.active_session_id, user.id, "stop")
        await show(query, t(user.language, "chat.stopped"), main_menu(user.language))
    elif rest == "next":
        if user.active_session_id:
            await end_session(session, user.active_session_id, user.id, "next")
        await session.refresh(user)
        await do_search(query, session, user)
    elif rest == "report":
        await show(query, t(user.language, "report.choose"), _report_kb(user.language))
    elif rest == "block":
        await show(query, t(user.language, "chat.block"), _pairs(user.language, [("chat.block", "block:yes"), ("common.cancel", "chat:panel")]))
    elif rest == "react":
        await show(query, t(user.language, "chat.reaction"), _reactions(user.language))
    elif rest == "gift":
        await render_gifts(query, session, user)
    elif rest == "reveal":
        await request_reveal(session, user)
        await show(query, t(user.language, "reveal.sent"), chat_keyboard(user.language))
    elif rest == "game":
        await show(query, t(user.language, "game.start") + "\n" + t(user.language, "game.q1"), _game_kb(user.language, "q1"))
    elif rest == "fav":
        await ask_favorite(session, user)
        await show(query, t(user.language, "reveal.sent"), chat_keyboard(user.language))


def _game_kb(lang: str, question: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t(lang, f"game.{question}{option}"), callback_data=f"game:{question}:{option}")]
            for option in ("a", "b", "c")
        ]
    )


async def on_react(query, session, state, user, rest: str) -> None:
    partner = await load_session_partner(session, user.id, user.active_session_id or "")
    emoji = t(user.language, f"react.{rest}")
    if partner:
        await enqueue(session, partner, f"{emoji}")
    await show(query, t(user.language, "chat.reaction_sent"), chat_keyboard(user.language))


async def on_gift(query, session, state, user, rest: str) -> None:
    await send_gift(session, user, rest)
    await show(query, t(user.language, "gifts.sent"), chat_keyboard(user.language))


async def on_verify(query, session, state, user, rest: str) -> None:
    if rest == "manual":
        await start_manual(session, user)
        await render_verify(query, session, user)
    elif rest == "external":
        url = await start_external(session, user)
        await show(query, t(user.language, "verify.external_started", url=url), back_menu(user.language))


async def on_reveal(query, session, state, user, rest: str) -> None:
    action, _, raw_id = rest.partition(":")
    if action == "accept":
        await answer_reveal(session, user, int(raw_id), True)
        await show(query, t(user.language, "reveal.done"), back_menu(user.language))
    elif action == "decline":
        await answer_reveal(session, user, int(raw_id), False)
        await show(query, t(user.language, "reveal.declined"), back_menu(user.language))


async def on_rematch(query, session, state, user, rest: str) -> None:
    action, _, session_id = rest.partition(":")
    if action in {"yes", "no"}:
        outcome = await choose_rematch(session, user, session_id, action)
        key = {"yes": "rematch.wait", "no": "settings.saved", "wait": "rematch.wait", "mutual": "rematch.both"}.get(outcome, "settings.saved")
        await show(query, t(user.language, key), main_menu(user.language))
    elif action == "open":
        partner_choice = await _partner_from_rematch(session, user.id, session_id)
        if partner_choice is None:
            await show(query, t(user.language, "errors.not_found"), main_menu(user.language))
            return
        await connect_pair(session, user.id, partner_choice)


async def _partner_from_rematch(session, user_id: int, session_id: str) -> int | None:
    from database.models import RematchChoice

    row = await session.scalar(
        select(RematchChoice).where(
            RematchChoice.session_id == session_id,
            RematchChoice.user_id != user_id,
            RematchChoice.choice == "yes",
        )
    )
    return row.user_id if row else None


async def on_fav(query, session, state, user, rest: str) -> None:
    await accept_favorite(session, user, int(rest.split(":")[-1]))
    await show(query, t(user.language, "favorites.added"), back_menu(user.language))


async def on_report(query, session, state, user, rest: str) -> None:
    await create_report(session, user, reason=rest)
    await show(query, t(user.language, "report.sent"), chat_keyboard(user.language))


async def on_block(query, session, state, user, rest: str) -> None:
    if rest == "yes" and user.active_session_id:
        partner = await load_session_partner(session, user.id, user.active_session_id)
        if partner:
            await block_user(session, user, partner.id)
    await show(query, t(user.language, "chat.blocked_done"), main_menu(user.language))


async def on_task(query, session, state, user, rest: str) -> None:
    amount = await claim_task(session, user, rest)
    await show(query, t(user.language, "tasks.claimed", amount=amount), back_menu(user.language))


async def on_pay(query, session, state, user, rest: str) -> None:
    code, _, channel = rest.partition(":")
    if channel not in {"stars", "crypto"}:
        channel = "stars"
    package = await session.scalar(select(CoinPackage).where(CoinPackage.code == code, CoinPackage.active.is_(True)))
    if package is None:
        raise AppError("missing", "pay.fail")
    if channel == "crypto":
        await _send_crypto(query, session, user, kind="coins", package_code=package.code, stars=package.stars, coins=package.coins)
        return
    order = await create_coin_order(session, user, package, channel="stars")
    await _send_order(query, user, order)


async def on_paytest(query, session, state, user, rest: str) -> None:
    settings = get_settings()
    if settings.payment_mode != "test" or settings.environment == "production":
        raise AppError("disabled", "pay.disabled")
    await confirm(session, rest, charge_id=f"test-{rest}", amount=(await _order_stars(session, rest)), currency="XTR")
    await show(query, t(user.language, "pay.success", coins="", package=rest), back_menu(user.language))


async def _order_stars(session, order_id: str) -> int:
    from database.models import PaymentOrder

    order = await session.get(PaymentOrder, order_id)
    if order is None:
        raise AppError("missing", "pay.fail")
    return order.stars


async def _send_crypto(query, session, user: User, *, kind: str, package_code: str, stars: int, coins: int) -> None:
    existing = await recent_crypto_order(session, user.id, kind, package_code)
    if existing and existing.pay_url and existing.quote_amount and existing.quote_asset:
        await _show_crypto_link(query, user, existing.pay_url, existing.quote_amount, existing.quote_asset)
        return
    if kind == "vip":
        plan = await get_plan(session, package_code)
        if plan is None:
            raise AppError("missing", "pay.fail")
        order = await create_vip_order(session, user, plan, channel="crypto")
        description = f"VIP {plan.days}d"
    else:
        package = await session.scalar(select(CoinPackage).where(CoinPackage.code == package_code, CoinPackage.active.is_(True)))
        if package is None:
            raise AppError("missing", "pay.fail")
        order = await create_coin_order(session, user, package, channel="crypto")
        description = f"{coins} LoveCoins"
    order = await attach_crypto_invoice(session, order, description)
    await _show_crypto_link(query, user, order.pay_url or "", order.quote_amount or "", order.quote_asset or "")


async def _show_crypto_link(query, user: User, url: str, amount: str, asset: str) -> None:
    await show(
        query,
        t(user.language, "pay.crypto_wait", amount=amount, asset=asset),
        InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=t(user.language, "pay.crypto_open"), url=url)],
                [_button(user.language, "common.back", "menu:home")],
            ]
        ),
    )


async def _send_order(query: CallbackQuery, user: User, order) -> None:
    settings = get_settings()
    if settings.payment_mode == "test" and settings.environment != "production":
        await show(
            query,
            t(user.language, "pay.test"),
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [_button(user.language, "pay.test", f"paytest:{order.id}")],
                    [_button(user.language, "common.back", "menu:home")],
                ]
            ),
        )
        return
    if query.message is None:
        return
    await query.bot.send_invoice(
        chat_id=query.message.chat.id,
        title="LoveCoins",
        description=t(user.language, "pay.invoice"),
        payload=order.id,
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="LoveCoins", amount=order.stars)],
    )


async def on_vipcoins(query, session, state, user, rest: str) -> None:
    plan = await get_plan(session, rest)
    if plan is None:
        raise AppError("missing", "pay.fail")
    try:
        sub = await grant(
            session,
            user.id,
            plan,
            source="coins",
            idempotency_key=f"vip-coins:{user.id}:{plan.code}:{int(utcnow().timestamp())}",
            charge_coins=True,
        )
    except InsufficientFunds:
        await show(query, t(user.language, "vip.need_coins"), back_menu(user.language))
        return
    await show(query, t(user.language, "vip.ok", until=sub.ends_at.strftime("%Y-%m-%d")), back_menu(user.language))


async def on_vipstars(query, session, state, user, rest: str) -> None:
    plan = await get_plan(session, rest)
    if plan is None:
        raise AppError("missing", "pay.fail")
    order = await create_vip_order(session, user, plan, channel="stars")
    await _send_order(query, user, order)


async def on_vipcrypto(query, session, state, user, rest: str) -> None:
    plan = await get_plan(session, rest)
    if plan is None:
        raise AppError("missing", "pay.fail")
    await _send_crypto(query, session, user, kind="vip", package_code=plan.code, stars=plan.star_price, coins=0)


async def on_set(query, session, state, user, rest: str) -> None:
    if not await _ready(query, session, state, user):
        return
    prefs = await get_prefs(session, user.id)
    if rest == "hide":
        user.is_hidden = not user.is_hidden
        await show(query, t(user.language, "settings.hide_on" if user.is_hidden else "settings.hide_off"))
        await render_settings(query, session, user)
    elif rest == "pause":
        user.is_search_paused = not user.is_search_paused
        await render_settings(query, session, user)
    elif rest == "gifts" and prefs:
        prefs.allow_gifts = not prefs.allow_gifts
        await show(query, t(user.language, "settings.gifts_on" if prefs.allow_gifts else "settings.gifts_off"))
    elif rest == "username" and prefs:
        prefs.allow_username_reveal = not prefs.allow_username_reveal
        await show(query, t(user.language, "settings.username_on" if prefs.allow_username_reveal else "settings.username_off"))
    elif rest == "nick":
        await state.set_state(Form.nickname)
        await state.update_data(edit="nickname")
        await show(query, t(user.language, "nickname.prompt"))
    elif rest == "bio":
        await state.set_state(Form.bio)
        await state.update_data(edit="bio")
        await show(query, t(user.language, "bio.prompt"))
    elif rest == "city":
        await state.set_state(Form.city)
        await state.update_data(edit="city")
        await show(query, t(user.language, "city.prompt"), _pairs(user.language, [("common.skip", "place:skip")]))
    elif rest == "lang":
        await state.update_data(edit="language")
        await show(query, t(user.language, "language.prompt"), _language_kb())
    elif rest == "interests":
        await show(query, *(await _interest_view(session, user)))
    elif rest == "range":
        await show(query, t(user.language, "age_range.prompt"), await _range_kb(session, user))
    elif rest == "filters":
        await state.update_data(edit="filters")
        await show(query, t(user.language, "search_prefs.prompt"), _prefs_kb(user.language))
    elif rest == "delete":
        await show(query, t(user.language, "delete.ask"), _pairs(user.language, [("delete.yes", "del:yes"), ("delete.no", "del:no")]))


async def on_promo(query, session, state, user, rest: str) -> None:
    await state.set_state(Form.promo)
    await show(query, t(user.language, "promo.prompt"))


async def on_daily(query, session, state, user, rest: str) -> None:
    created, amount = await claim_daily(session, user)
    key = "wallet.daily_ok" if created else "wallet.daily_already"
    await show(query, t(user.language, key, amount=amount), back_menu(user.language))


async def on_del(query, session, state, user, rest: str) -> None:
    if rest == "yes":
        await delete_account(session, user)
        await show(query, t(user.language, "delete.done"))
    else:
        await render_home(query, session, state, user)


async def on_appeal(query, session, state, user, rest: str) -> None:
    await state.set_state(Form.appeal)
    await show(query, t(user.language, "appeal.ask"))


async def on_game(query, session, state, user, rest: str) -> None:
    question, _, option = rest.partition(":")
    await answer_game(session, user, question, option)
    order = {"q1": "q2", "q2": "q3"}
    if question in order:
        nxt = order[question]
        await show(query, t(user.language, f"game.{nxt}"), _game_kb(user.language, nxt))
    else:
        await show(query, t(user.language, "game.wait"), chat_keyboard(user.language))


async def on_qotd(query, session, state, user, rest: str) -> None:
    key = question_of_day_key()
    await state.set_state(Form.qotd)
    await state.update_data(qotd=key)
    await show(query, t(user.language, "qotd.title") + "\n" + t(user.language, key))


DISPATCH.update(
    {
        "menu": on_menu,
        "reg": on_reg,
        "lang": on_lang,
        "gender": on_gender,
        "interest": on_interest,
        "place": on_place,
        "range": on_range,
        "prefs": on_prefs,
        "search": on_search,
        "chat": on_chat,
        "react": on_react,
        "gift": on_gift,
        "verify": on_verify,
        "reveal": on_reveal,
        "rematch": on_rematch,
        "fav": on_fav,
        "report": on_report,
        "block": on_block,
        "task": on_task,
        "pay": on_pay,
        "paytest": on_paytest,
        "vipcoins": on_vipcoins,
        "vipstars": on_vipstars,
        "vipcrypto": on_vipcrypto,
        "set": on_set,
        "promo": on_promo,
        "daily": on_daily,
        "del": on_del,
        "appeal": on_appeal,
        "game": on_game,
        "qotd": on_qotd,
    }
)


@router.callback_query()
async def callbacks(query: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    if query.from_user is None:
        return
    lock = _CLICKS.setdefault(query.from_user.id, asyncio.Lock())
    if lock.locked():
        try:
            await query.answer()
        except TelegramBadRequest:
            pass
        return
    async with lock:
        try:
            await query.answer()
        except TelegramBadRequest:
            pass
        data = query.data or ""
        prefix, _, rest = data.partition(":")
        handler = DISPATCH.get(prefix)
        if handler is None:
            return
        user = await current(session, query)
        try:
            await handler(query, session, state, user, rest)
        except AppError as exc:
            markup = verify_keyboard(user.language) if exc.message_key == "verify.locked" else back_menu(user.language)
            await show(query, t(user.language, exc.message_key, **exc.params), markup)


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, session: AsyncSession, state: FSMContext) -> None:
    raw = (command.args or "").strip()
    if raw.startswith("ref_"):
        raw = raw[4:]
    user = await open_user(session, message.from_user.id, message.from_user.username, raw or None)
    if user.registration_completed:
        await show(message, t(user.language, "welcome.text"), main_menu(user.language))
        return
    await show(message, t(user.language, "welcome.text"), _pairs(user.language, [("welcome.start", "menu:home")], width=1))


@router.message(Command("profile"))
async def cmd_profile(message: Message, session: AsyncSession, state: FSMContext) -> None:
    user = await current(session, message)
    if await _ready(message, session, state, user):
        await render_profile(message, session, user)


@router.message(Command("search"))
async def cmd_search(message: Message, session: AsyncSession, state: FSMContext) -> None:
    user = await current(session, message)
    if await _ready(message, session, state, user):
        await do_search(message, session, user)


@router.message(Command("stop"))
async def cmd_stop(message: Message, session: AsyncSession, state: FSMContext) -> None:
    user = await current(session, message)
    if user.active_session_id:
        await end_session(session, user.active_session_id, user.id, "stop")
    await show(message, t(user.language, "chat.stopped"), main_menu(user.language))


@router.message(Command("verify"))
async def cmd_verify(message: Message, session: AsyncSession, state: FSMContext) -> None:
    user = await current(session, message)
    if await _ready(message, session, state, user):
        await render_verify(message, session, user)


@router.message(Command("balance"))
async def cmd_balance(message: Message, session: AsyncSession, state: FSMContext) -> None:
    user = await current(session, message)
    if await _ready(message, session, state, user):
        await render_balance(message, session, user)


@router.message(Command("referral"))
async def cmd_referral(message: Message, session: AsyncSession, state: FSMContext) -> None:
    user = await current(session, message)
    if await _ready(message, session, state, user):
        await render_referral(message, session, user)


@router.message(Command("settings"))
async def cmd_settings(message: Message, session: AsyncSession, state: FSMContext) -> None:
    user = await current(session, message)
    if await _ready(message, session, state, user):
        await render_settings(message, session, user)


@router.message(Command("help"))
async def cmd_help(message: Message, session: AsyncSession, state: FSMContext) -> None:
    user = await current(session, message)
    await show(message, t(user.language, "help.text"), main_menu(user.language) if user.registration_completed else None)


@router.message(Command("delete_account"))
async def cmd_delete_account(message: Message, session: AsyncSession, state: FSMContext) -> None:
    user = await current(session, message)
    await show(message, t(user.language, "delete.ask"), _pairs(user.language, [("delete.yes", "del:yes"), ("delete.no", "del:no")]))


@router.message(Command("next"))
async def cmd_next(message: Message, session: AsyncSession, state: FSMContext) -> None:
    user = await current(session, message)
    if user.active_session_id:
        await end_session(session, user.active_session_id, user.id, "next")
        await session.refresh(user)
    if await _ready(message, session, state, user):
        await do_search(message, session, user)


@router.message(StateFilter(Form.age), F.text)
async def input_age(message: Message, session: AsyncSession, state: FSMContext) -> None:
    user = await current(session, message)
    try:
        age = int((message.text or "").strip())
    except ValueError:
        await message.answer(t(user.language, "user_age.invalid"))
        return
    try:
        await set_age(session, user, age)
    except AppError as exc:
        await message.answer(t(user.language, exc.message_key, **exc.params))
        return
    await render_step(message, session, state, user)


@router.message(StateFilter(Form.country), F.text)
async def input_country(message: Message, session: AsyncSession, state: FSMContext) -> None:
    user = await current(session, message)
    try:
        await set_place(session, user, "country", message.text or "")
    except AppError as exc:
        await message.answer(t(user.language, exc.message_key, **exc.params))
        return
    await render_step(message, session, state, user)


@router.message(StateFilter(Form.city), F.text)
async def input_city(message: Message, session: AsyncSession, state: FSMContext) -> None:
    user = await current(session, message)
    data = await state.get_data()
    try:
        await set_place(session, user, "city", message.text or "")
    except AppError as exc:
        await message.answer(t(user.language, exc.message_key, **exc.params))
        return
    await state.clear()
    if data.get("edit") == "city":
        await show(message, t(user.language, "settings.saved"), back_menu(user.language))
        return
    await render_step(message, session, state, user)


@router.message(StateFilter(Form.nickname), F.text)
async def input_nick(message: Message, session: AsyncSession, state: FSMContext) -> None:
    user = await current(session, message)
    data = await state.get_data()
    try:
        await set_nickname(session, user, message.text or "")
    except AppError as exc:
        await message.answer(t(user.language, exc.message_key, **exc.params))
        return
    await state.clear()
    if data.get("edit") == "nickname":
        await show(message, t(user.language, "settings.saved"), back_menu(user.language))
        return
    await render_step(message, session, state, user)


@router.message(StateFilter(Form.bio), F.text)
async def input_bio(message: Message, session: AsyncSession, state: FSMContext) -> None:
    user = await current(session, message)
    data = await state.get_data()
    try:
        await set_bio(session, user, message.text or "")
    except AppError as exc:
        await message.answer(t(user.language, exc.message_key, **exc.params))
        return
    await state.clear()
    if data.get("edit") == "bio":
        await show(message, t(user.language, "settings.saved"), back_menu(user.language))
        return
    await render_step(message, session, state, user)


@router.message(StateFilter(Form.promo), F.text)
async def input_promo(message: Message, session: AsyncSession, state: FSMContext) -> None:
    user = await current(session, message)
    try:
        amount = await redeem_promo(session, user, message.text or "")
    except AppError as exc:
        await message.answer(t(user.language, exc.message_key, **exc.params))
        return
    await state.clear()
    await show(message, t(user.language, "promo.ok", amount=amount), main_menu(user.language))


@router.message(StateFilter(Form.appeal), F.text)
async def input_appeal(message: Message, session: AsyncSession, state: FSMContext) -> None:
    user = await current(session, message)
    try:
        await submit_appeal(session, user, message.text or "")
    except AppError as exc:
        await message.answer(t(user.language, exc.message_key, **exc.params))
        return
    await state.clear()
    await show(message, t(user.language, "appeal.sent"))


@router.message(StateFilter(Form.qotd), F.text)
async def input_qotd(message: Message, session: AsyncSession, state: FSMContext) -> None:
    user = await current(session, message)
    data = await state.get_data()
    key = data.get("qotd") or question_of_day_key()
    await record_qotd(session, user, key)
    text = t(user.language, key) + "\n" + (message.text or "")
    if user.active_session_id:
        await relay(session, user, kind="text", text=text[:1000])
    await state.clear()
    await show(message, t(user.language, "qotd.thanks"), chat_keyboard(user.language) if user.active_session_id else main_menu(user.language))


@router.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def relay_text(message: Message, session: AsyncSession, state: FSMContext) -> None:
    user = await current(session, message)
    if not user.registration_completed:
        await render_step(message, session, state, user)
        return
    if user.presence_state != "in_chat":
        await show(message, t(user.language, "chat.not_in_chat"), main_menu(user.language))
        return
    result = await relay(session, user, kind="text", text=message.text or "", telegram_message_id=message.message_id)
    await _relay_feedback(message, user, result)


@router.message(StateFilter(None), F.sticker)
async def relay_sticker(message: Message, session: AsyncSession) -> None:
    user = await current(session, message)
    if user.presence_state != "in_chat" or message.sticker is None:
        return
    await relay(session, user, kind="sticker", file_id=message.sticker.file_id, telegram_message_id=message.message_id)


@router.message(F.photo)
async def on_photo(message: Message, session: AsyncSession) -> None:
    user = await current(session, message)
    photo = message.photo[-1] if message.photo else None
    if photo is None:
        return
    if user.verification_status in {"pending", "rejected"} and user.presence_state != "in_chat":
        buffer = BytesIO()
        tg_file = await message.bot.get_file(photo.file_id)
        await message.bot.download_file(tg_file.file_path, destination=buffer)
        digest = hashlib.sha256(buffer.getvalue()).hexdigest()
        buffer.close()
        try:
            await submit_photo(session, user, file_id=photo.file_id, digest=digest)
        except AppError as exc:
            await message.answer(
                t(user.language, exc.message_key, **exc.params),
                reply_markup=verify_keyboard(user.language),
            )
        else:
            await message.answer(t(user.language, "verify.need_video"), reply_markup=verify_keyboard(user.language))
        return
    if user.presence_state == "in_chat":
        result = await relay(
            session,
            user,
            kind="photo",
            file_id=photo.file_id,
            telegram_message_id=message.message_id,
            file_size=photo.file_size,
        )
        await _relay_feedback(message, user, result)
        return
    await message.answer(t(user.language, "media.intro"), reply_markup=verify_keyboard(user.language))


@router.message(F.video_note)
async def on_video_note(message: Message, session: AsyncSession) -> None:
    user = await current(session, message)
    note = message.video_note
    if note is None:
        return
    if user.presence_state != "in_chat" and user.verification_status in {"pending", "rejected", "in_review"}:
        try:
            await submit_video_note(session, user, file_id=note.file_id)
        except AppError as exc:
            await message.answer(t(user.language, exc.message_key, **exc.params), reply_markup=verify_keyboard(user.language))
        else:
            await message.answer(t(user.language, "verify.submitted"), reply_markup=main_menu(user.language))
        return
    if user.presence_state != "in_chat":
        await message.answer(t(user.language, "media.unsupported"))
        return
    result = await relay(session, user, kind="video_note", file_id=note.file_id, telegram_message_id=message.message_id)
    await _relay_feedback(message, user, result)


@router.message(F.video | F.animation | F.document | F.voice | F.audio | F.contact | F.location)
async def on_other_media(message: Message, session: AsyncSession) -> None:
    user = await current(session, message)
    kind = "document"
    file_id = None
    if message.video:
        kind, file_id = "video", message.video.file_id
    elif message.animation:
        kind, file_id = "animation", message.animation.file_id
    elif message.voice:
        kind, file_id = "voice", message.voice.file_id
    elif message.audio:
        kind, file_id = "audio", message.audio.file_id
    elif message.contact:
        kind = "contact"
    elif message.location:
        kind = "location"
    elif message.document and message.document.mime_type and message.document.mime_type.startswith("image/"):
        kind, file_id = "photo", message.document.file_id
    if user.presence_state != "in_chat":
        await message.answer(t(user.language, "media.unsupported"))
        return
    result = await relay(session, user, kind=kind, file_id=file_id, telegram_message_id=message.message_id)
    await _relay_feedback(message, user, result)


async def _relay_feedback(message: Message, user: User, result) -> None:
    if result.status == "delivered":
        if result.code == "scam":
            await message.answer(t(user.language, "scam.warn"))
        return
    if result.code == "verification_required":
        await message.answer(t(user.language, "verify.locked"), reply_markup=verify_keyboard(user.language))
    elif result.code == "photo_limit":
        await message.answer(t(user.language, "media.limit"))
    elif result.code == "child_safety":
        await message.answer(t(user.language, "safety.blocked"))
    elif result.code == "rate":
        await message.answer(t(user.language, "errors.rate_limit"))
    elif result.status == "blocked":
        await message.answer(t(user.language, "media.blocked"))


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery, session: AsyncSession) -> None:
    ok = await pre_checkout_ok(session, query.invoice_payload, query.currency, query.total_amount)
    await query.answer(ok=ok, error_message=None if ok else "Payment was not accepted")


@router.message(F.successful_payment)
async def successful_payment(message: Message, session: AsyncSession) -> None:
    payment = message.successful_payment
    if payment is None:
        return
    await confirm(
        session,
        payment.invoice_payload,
        charge_id=payment.telegram_payment_charge_id,
        amount=payment.total_amount,
        currency=payment.currency,
    )


@group_router.message(CommandStart())
async def group_start(message: Message) -> None:
    await message.answer(t("ru", "errors.private_only"))
