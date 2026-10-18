from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from core.config import get_settings
from core.i18n import t

MENU_ACTIONS = (
    "search",
    "chat",
    "profile",
    "verify",
    "media",
    "gifts",
    "balance",
    "topup",
    "vip",
    "referral",
    "tasks",
    "favorites",
    "settings",
    "help",
)


def _rows(buttons: list[InlineKeyboardButton], width: int = 2) -> list[list[InlineKeyboardButton]]:
    return [buttons[index : index + width] for index in range(0, len(buttons), width)]


def kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def main_menu(lang: str) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text=t(lang, f"menu.{action}"), callback_data=f"menu:{action}") for action in MENU_ACTIONS
    ]
    rows = _rows(buttons, 1)
    url = get_settings().miniapp_url.strip()
    if url:
        rows.append([InlineKeyboardButton(text=t(lang, "miniapp.open"), web_app=WebAppInfo(url=url))])
    return kb(rows)


def chat_keyboard(lang: str) -> InlineKeyboardMarkup:
    return kb(
        [
            [
                InlineKeyboardButton(text=t(lang, "chat.next"), callback_data="chat:next"),
                InlineKeyboardButton(text=t(lang, "chat.stop"), callback_data="chat:stop"),
            ],
            [
                InlineKeyboardButton(text=t(lang, "chat.report"), callback_data="chat:report"),
                InlineKeyboardButton(text=t(lang, "chat.block"), callback_data="chat:block"),
            ],
            [
                InlineKeyboardButton(text=t(lang, "chat.interests"), callback_data="chat:interests"),
                InlineKeyboardButton(text=t(lang, "chat.reaction"), callback_data="chat:react"),
            ],
            [
                InlineKeyboardButton(text=t(lang, "chat.gift"), callback_data="chat:gift"),
                InlineKeyboardButton(text=t(lang, "chat.reveal"), callback_data="chat:reveal"),
            ],
            [
                InlineKeyboardButton(text=t(lang, "game.title"), callback_data="chat:game"),
                InlineKeyboardButton(text=t(lang, "qotd.answer"), callback_data="qotd:ask"),
            ],
            [
                InlineKeyboardButton(text=t(lang, "favorites.ask"), callback_data="chat:fav"),
                InlineKeyboardButton(text=t(lang, "chat.help"), callback_data="chat:help"),
            ],
            [InlineKeyboardButton(text=t(lang, "common.back"), callback_data="menu:home")],
        ]
    )


def verify_keyboard(lang: str) -> InlineKeyboardMarkup:
    return kb(
        [
            [InlineKeyboardButton(text=t(lang, "verify.button"), callback_data="menu:verify")],
            [InlineKeyboardButton(text=t(lang, "common.back"), callback_data="menu:home")],
        ]
    )


def back_menu(lang: str) -> InlineKeyboardMarkup:
    return kb([[InlineKeyboardButton(text=t(lang, "common.back"), callback_data="menu:home")]])
