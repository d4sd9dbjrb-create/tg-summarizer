"""Хендлеры событий userbot. Навешиваются на каждый Telethon-клиент.

В отличие от прошлой версии, account_id и tg_user_id_owner передаются явно,
а изоляция данных обеспечивается через account_id во всех запросах.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from telethon import TelegramClient, events
from telethon.tl.types import (
    Channel,
    Chat as TLChat,
    MessageMediaDocument,
    MessageMediaPhoto,
    User as TLUser,
)

from .config import get_settings
from .db import SessionLocal
from .models import Account, Chat, Keyword, Message

log = logging.getLogger(__name__)
settings = get_settings()


def _media_type(msg) -> tuple[bool, str | None]:
    if not msg.media:
        return False, None
    if isinstance(msg.media, MessageMediaPhoto):
        return True, "photo"
    if isinstance(msg.media, MessageMediaDocument):
        doc = msg.media.document
        for attr in getattr(doc, "attributes", []):
            cls = attr.__class__.__name__
            if cls == "DocumentAttributeVideo":
                return True, "video"
            if cls == "DocumentAttributeAudio":
                return True, "voice" if getattr(attr, "voice", False) else "audio"
            if cls == "DocumentAttributeSticker":
                return True, "sticker"
            if cls == "DocumentAttributeAnimated":
                return True, "gif"
        return True, "document"
    return True, "other"


def _entity_type(entity) -> str:
    if isinstance(entity, Channel):
        return "channel" if getattr(entity, "broadcast", False) else "group"
    if isinstance(entity, TLChat):
        return "group"
    if isinstance(entity, TLUser):
        return "user"
    return "unknown"


async def _store_message(account_id: int, msg) -> tuple[Message | None, str | None]:
    """Сохраняет сообщение и возвращает (Message, тип_алерта|None) для пуша."""
    chat_entity = await msg.get_chat()
    sender = await msg.get_sender()

    has_media, mtype = _media_type(msg)
    text = msg.message or ""

    sender_name = None
    if sender:
        sender_name = (
            getattr(sender, "username", None)
            or " ".join(
                filter(
                    None,
                    [getattr(sender, "first_name", None), getattr(sender, "last_name", None)],
                )
            )
            or None
        )

    fwd_from = None
    if msg.forward:
        try:
            fwd_from = (
                getattr(msg.forward.chat, "title", None)
                or getattr(msg.forward.sender, "username", None)
                or "forwarded"
            )
        except Exception:  # noqa: BLE001
            fwd_from = "forwarded"

    tg_chat_id = getattr(chat_entity, "id", None)
    if tg_chat_id is None:
        return None, None

    title = getattr(chat_entity, "title", None) or " ".join(
        filter(
            None,
            [getattr(chat_entity, "first_name", None), getattr(chat_entity, "last_name", None)],
        )
    ) or ""
    username = getattr(chat_entity, "username", None)
    ctype = _entity_type(chat_entity)

    alert_type: str | None = None

    async with SessionLocal() as session:
        # Проверяем privacy-настройки аккаунта
        acc = await session.get(Account, account_id)
        if acc:
            is_bot_entity = isinstance(chat_entity, TLUser) and getattr(chat_entity, "bot", False)
            if acc.ignore_bots and is_bot_entity:
                return None, None
            if not acc.analyze_dms and ctype == "user":
                return None, None
            if not acc.analyze_groups and ctype == "group":
                return None, None
            if not acc.analyze_channels and ctype == "channel":
                return None, None

        # upsert chat
        chat_row = (
            await session.execute(
                select(Chat).where(Chat.account_id == account_id, Chat.tg_chat_id == tg_chat_id)
            )
        ).scalar_one_or_none()
        if chat_row is None:
            chat_row = Chat(
                account_id=account_id,
                tg_chat_id=tg_chat_id,
                title=title,
                type=ctype,
                username=username,
                is_tracked=True,
            )
            session.add(chat_row)
            await session.flush()
        else:
            chat_row.title = title or chat_row.title
            chat_row.type = ctype or chat_row.type
            chat_row.username = username or chat_row.username

        # ingest mode / mute / не отслеживается
        if chat_row.is_muted or not chat_row.is_tracked:
            await session.commit()
            return None, None
        # whitelist mode: пропускаем нетрекаемые чаты
        # (is_tracked=True ставится по умолчанию, юзер сам отключает)
        # для режима whitelist по умолчанию is_tracked мы выставляем False для новых чатов
        # — здесь поведение решается в setting; пока в "all" просто пишем всё подряд
        # уникальность
        existing = await session.execute(
            select(Message).where(
                Message.account_id == account_id,
                Message.chat_id == chat_row.id,
                Message.tg_message_id == msg.id,
            )
        )
        if existing.scalar_one_or_none() is not None:
            await session.commit()
            return None, None

        date = msg.date or datetime.now(timezone.utc)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)

        mentions_me = bool(getattr(msg, "mentioned", False))
        if mentions_me:
            alert_type = "mention"

        # keyword alerts
        if text and not msg.out:
            kws = (
                await session.execute(
                    select(Keyword).where(
                        Keyword.account_id == account_id, Keyword.enabled.is_(True)
                    )
                )
            ).scalars().all()
            tlow = text.lower()
            for kw in kws:
                if kw.word.lower() in tlow:
                    mentions_me = True
                    alert_type = alert_type or f"keyword:{kw.word}"
                    break

        m = Message(
            account_id=account_id,
            chat_id=chat_row.id,
            tg_message_id=msg.id,
            sender_id=getattr(sender, "id", None),
            sender_name=sender_name,
            date=date,
            text=text,
            reply_to_msg_id=msg.reply_to_msg_id,
            has_media=has_media,
            media_type=mtype,
            is_outgoing=bool(msg.out),
            mentions_me=mentions_me,
            forward_from=fwd_from,
        )
        session.add(m)
        await session.commit()
        await session.refresh(m)

    return m, alert_type


def attach_handlers(client: TelegramClient, account_id: int, on_alert):
    """on_alert(account_id, alert_type, message_row, chat_title) — для пушей в control bot."""

    @client.on(events.NewMessage(incoming=True))
    async def _on_in(event):
        try:
            m, alert = await _store_message(account_id, event.message)
            if alert and m and on_alert:
                chat = await event.message.get_chat()
                await on_alert(account_id, alert, m, getattr(chat, "title", None) or "(чат)")
        except Exception:  # noqa: BLE001
            log.exception("ingest incoming failed (account=%s)", account_id)

    @client.on(events.NewMessage(outgoing=True))
    async def _on_out(event):
        try:
            await _store_message(account_id, event.message)
        except Exception:  # noqa: BLE001
            log.exception("ingest outgoing failed (account=%s)", account_id)
