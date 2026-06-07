"""Менеджер мульти-Telethon: один TelegramClient на каждый Account.

Все клиенты живут в одном asyncio loop. Сессии хранятся в БД зашифрованно
через Fernet и материализуются как StringSession.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from sqlalchemy import select
from telethon import TelegramClient
from telethon.errors import AuthKeyUnregisteredError, UserDeactivatedBanError
from telethon.sessions import StringSession

from .config import get_settings
from .crypto import decrypt, encrypt
from .db import SessionLocal
from .models import Account
from .userbot_handlers import attach_handlers

log = logging.getLogger(__name__)
settings = get_settings()

# account_id -> TelegramClient
_clients: dict[int, TelegramClient] = {}

# callback для алертов (mention/keyword)
_alert_cb: Callable[[int, str, object, str], Awaitable[None]] | None = None


def set_alert_callback(cb):
    global _alert_cb
    _alert_cb = cb


async def _alert_proxy(account_id, alert_type, msg_row, chat_title):
    if _alert_cb:
        await _alert_cb(account_id, alert_type, msg_row, chat_title)


def _make_client(session_str: str) -> TelegramClient:
    return TelegramClient(
        StringSession(session_str),
        settings.tg_api_id,
        settings.tg_api_hash,
        device_model="TG Summarizer",
        system_version="1.0",
        app_version="1.0",
    )


async def start_account(account: Account) -> bool:
    """Поднимает клиент для аккаунта. Возвращает True если ОК."""
    if account.id in _clients:
        return True
    try:
        session_str = decrypt(account.encrypted_session)
        client = _make_client(session_str)
        await client.connect()
        if not await client.is_user_authorized():
            log.warning("Account %s session unauthorized — disabling", account.id)
            await _disable(account.id, "logged_out")
            await client.disconnect()
            return False
        attach_handlers(client, account.id, _alert_proxy)
        _clients[account.id] = client
        log.info("Started Telethon client for account %s (tg_id=%s)", account.id, account.tg_user_id)
        return True
    except (AuthKeyUnregisteredError, UserDeactivatedBanError) as e:
        log.warning("Account %s auth invalid: %s", account.id, e)
        await _disable(account.id, "logged_out")
        return False
    except Exception:  # noqa: BLE001
        log.exception("Failed to start account %s", account.id)
        return False


async def stop_account(account_id: int) -> None:
    client = _clients.pop(account_id, None)
    if client:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            log.exception("disconnect failed")


async def _disable(account_id: int, status: str = "disabled") -> None:
    async with SessionLocal() as s:
        a = await s.get(Account, account_id)
        if a:
            a.status = status
            await s.commit()


async def start_all() -> None:
    """Поднимает все active аккаунты."""
    async with SessionLocal() as s:
        rows = (
            await s.execute(select(Account).where(Account.status == "active"))
        ).scalars().all()
    log.info("Starting %d userbot clients...", len(rows))
    for acc in rows:
        await start_account(acc)


async def stop_all() -> None:
    for aid in list(_clients):
        await stop_account(aid)


def get_client(account_id: int) -> TelegramClient | None:
    return _clients.get(account_id)


# ---------------------------------------------------------------------------
#  Регистрация: создание новых сессий
# ---------------------------------------------------------------------------

async def create_pending_client() -> TelegramClient:
    """Создаёт новый клиент с пустой сессией для процесса логина (web)."""
    client = _make_client("")
    await client.connect()
    return client


async def session_to_encrypted(client: TelegramClient) -> str:
    """Сериализует сессию авторизованного клиента в зашифрованную строку."""
    s = client.session.save()
    return encrypt(s)


async def persist_account(client: TelegramClient, encrypted_session: str) -> Account:
    """Создаёт/обновляет Account из авторизованного клиента и поднимает в hot-set."""
    me = await client.get_me()
    async with SessionLocal() as s:
        existing = (
            await s.execute(select(Account).where(Account.tg_user_id == me.id))
        ).scalar_one_or_none()
        if existing:
            existing.encrypted_session = encrypted_session
            existing.tg_username = getattr(me, "username", None)
            existing.tg_first_name = getattr(me, "first_name", None)
            existing.tg_phone = getattr(me, "phone", None)
            existing.status = "active"
            acc = existing
        else:
            acc = Account(
                tg_user_id=me.id,
                tg_username=getattr(me, "username", None),
                tg_first_name=getattr(me, "first_name", None),
                tg_phone=getattr(me, "phone", None),
                encrypted_session=encrypted_session,
                status="active",
                daily_digest_hour=settings.daily_digest_hour,
                ingest_mode=settings.ingest_mode,
                llm_provider=settings.llm_provider,
            )
            s.add(acc)
        await s.commit()
        await s.refresh(acc)

    # Стартуем в hot-set
    if acc.id not in _clients:
        await start_account(acc)
    return acc
