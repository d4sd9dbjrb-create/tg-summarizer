"""Минимальный управляющий бот.

Цель: только запуск Mini App + автоматические уведомления (дайджесты, алерты).
Все взаимодействия с данными — через Mini App.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from html import escape

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message as AioMessage,
    WebAppInfo,
)
from sqlalchemy import select

from . import account_manager
from .config import get_settings
from .db import SessionLocal
from .models import Account

log = logging.getLogger(__name__)
settings = get_settings()

bot = Bot(
    settings.control_bot_token,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

import time as _time
# Уникальный токен версии — обновляется при каждом перезапуске бота.
# Нужен чтобы Telegram сбрасывал кеш Mini App, видя «новый» URL.
_APP_VERSION = str(int(_time.time()))


def _app_url() -> str:
    # Версию кладём в PATH (не в query) — Telegram кеширует Mini App по pathname,
    # query-параметры он игнорирует. Новый путь = принудительный refetch.
    return f"{settings.public_base_url.rstrip('/')}/app/v{_APP_VERSION}"


def _open_app_kb(text: str = "✨ Открыть SecondMe") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text=text, web_app=WebAppInfo(url=_app_url()))
        ]]
    )


WELCOME = (
    "✨ <b>Привет! Я SecondMe</b>\n\n"
    "Твой персональный аналитик Telegram-чатов.\n"
    "Все функции — внутри приложения.\n\n"
    "Жми кнопку ниже, чтобы открыть 👇"
)


async def send_long(chat_id: int, text: str, reply_markup=None):
    """Send long text split on newline boundaries."""
    MAX = 3800
    if not text:
        return
    if len(text) <= MAX:
        await bot.send_message(chat_id, text, reply_markup=reply_markup)
        return
    chunk_lines: list[str] = []
    chunk_len = 0
    parts: list[str] = []
    for line in text.split("\n"):
        line_len = len(line) + 1
        if chunk_lines and chunk_len + line_len > MAX:
            parts.append("\n".join(chunk_lines))
            chunk_lines = [line]
            chunk_len = line_len
        else:
            chunk_lines.append(line)
            chunk_len += line_len
    if chunk_lines:
        parts.append("\n".join(chunk_lines))

    # markup только на последней части
    for i, p in enumerate(parts):
        kb = reply_markup if i == len(parts) - 1 else None
        await bot.send_message(chat_id, p, reply_markup=kb)


async def notify(tg_user_id: int, text: str, reply_markup=None) -> None:
    try:
        await bot.send_message(tg_user_id, text, reply_markup=reply_markup)
    except Exception:  # noqa: BLE001
        log.exception("notify failed")


# ---------------------------------------------------------------------------
#  /start (deep-link + обычный)
# ---------------------------------------------------------------------------

@dp.message(CommandStart(deep_link=True))
async def cmd_start_token(m: AioMessage, command: CommandObject):
    """Привязывает уже зарегистрированный через Mini App аккаунт к боту."""
    token = (command.args or "").strip()
    if not token:
        await m.answer(WELCOME, reply_markup=_open_app_kb())
        return

    async with SessionLocal() as s:
        acc = (
            await s.execute(select(Account).where(Account.link_token == token))
        ).scalar_one_or_none()

        if not acc:
            await m.answer(
                "❌ Неверный или истёкший deep-link.\n\n"
                "Открой приложение и пройди регистрацию заново.",
                reply_markup=_open_app_kb(),
            )
            return
        if acc.link_token_expires_at and acc.link_token_expires_at < datetime.now(timezone.utc):
            await m.answer(
                "❌ Срок ссылки истёк.",
                reply_markup=_open_app_kb(),
            )
            return
        if acc.tg_user_id != m.from_user.id:
            await m.answer(
                "⚠️ Этот deep-link предназначен для другого Telegram-аккаунта.\n"
                "Открой бота с того же аккаунта, который подключал в приложении."
            )
            return

        acc.bot_linked_at = datetime.now(timezone.utc)
        acc.link_token = None
        acc.link_token_expires_at = None
        await s.commit()

    await m.answer(
        "✅ <b>Аккаунт подключён</b>\n\n"
        "Я начинаю собирать твои сообщения. Все сводки и статистика — в приложении.",
        reply_markup=_open_app_kb(),
    )


@dp.message(CommandStart())
async def cmd_start(m: AioMessage):
    await m.answer(WELCOME, reply_markup=_open_app_kb())


# ---------------------------------------------------------------------------
#  Алерты от userbot'ов (упоминания и keywords)
# ---------------------------------------------------------------------------

async def on_alert(account_id: int, alert_type: str, msg_row, chat_title: str):
    async with SessionLocal() as s:
        acc = await s.get(Account, account_id)
        if not acc or not acc.bot_linked_at:
            return
    label = (
        "📣 <b>Упоминание</b>"
        if alert_type == "mention"
        else f"🔔 <b>Keyword:</b> <code>{escape(alert_type.split(':', 1)[-1])}</code>"
    )
    text = (
        f"{label}\n"
        f"<b>{escape(chat_title)}</b> · {escape(msg_row.sender_name or '?')}\n"
        f"{escape((msg_row.text or '')[:500])}"
    )
    try:
        await bot.send_message(acc.tg_user_id, text, reply_markup=_open_app_kb("📊 Открыть"))
    except Exception:  # noqa: BLE001
        log.exception("alert delivery failed")


# ---------------------------------------------------------------------------
#  Lifecycle
# ---------------------------------------------------------------------------

async def start_bot() -> None:
    account_manager.set_alert_callback(on_alert)
    try:
        # Кнопка меню рядом с полем ввода → запуск Mini App
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="📊 Открыть приложение",
                web_app=WebAppInfo(url=_app_url()),
            )
        )
        # Никаких / -команд кроме /start
        await bot.set_my_commands([])
        log.info("Menu button → %s", _app_url())
    except Exception:
        log.exception("Failed to set menu button")
    log.info("Control bot polling (Mini-App-only mode)...")
    await dp.start_polling(bot)
