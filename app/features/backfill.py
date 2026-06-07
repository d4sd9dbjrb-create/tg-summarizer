"""Загрузка истории сообщений из всех чатов (backfill).

Запускается один раз после регистрации или вручную.
Прогресс отслеживается в памяти через _state dict.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TypedDict

from sqlalchemy import select
from telethon import TelegramClient
from telethon.tl.types import (
    Channel,
    Chat as TLChat,
    MessageMediaDocument,
    MessageMediaPhoto,
    User as TLUser,
)

from ..db import SessionLocal
from ..models import Chat, Message

log = logging.getLogger(__name__)

LIMIT_PER_CHAT = 300
MAX_DIALOGS = 200


class BackfillState(TypedDict):
    status: str          # idle | running | done | error
    chats_done: int
    chats_total: int
    messages_added: int
    last_error: str


_state: dict[int, BackfillState] = {}


def get_state(account_id: int) -> BackfillState:
    return _state.get(account_id, {
        "status": "idle",
        "chats_done": 0,
        "chats_total": 0,
        "messages_added": 0,
        "last_error": "",
    })


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


def _sender_name(sender) -> str | None:
    if not sender:
        return None
    return (
        getattr(sender, "username", None)
        or " ".join(
            filter(None, [
                getattr(sender, "first_name", None),
                getattr(sender, "last_name", None),
            ])
        )
        or None
    )


async def _save_message(account_id: int, msg, chat_entity) -> bool:
    """Сохраняет одно историческое сообщение. Возвращает True если новое."""
    tg_chat_id = getattr(chat_entity, "id", None)
    if tg_chat_id is None:
        return False

    has_media, mtype = _media_type(msg)
    text = msg.message or ""

    title = getattr(chat_entity, "title", None) or " ".join(
        filter(None, [
            getattr(chat_entity, "first_name", None),
            getattr(chat_entity, "last_name", None),
        ])
    ) or ""
    ctype = _entity_type(chat_entity)
    username = getattr(chat_entity, "username", None)

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

    date = msg.date or datetime.now(timezone.utc)
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)

    async with SessionLocal() as s:
        chat_row = (
            await s.execute(
                select(Chat).where(
                    Chat.account_id == account_id,
                    Chat.tg_chat_id == tg_chat_id,
                )
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
            s.add(chat_row)
            await s.flush()
        else:
            chat_row.title = title or chat_row.title

        # пропускаем дубликаты
        existing = (
            await s.execute(
                select(Message.id).where(
                    Message.account_id == account_id,
                    Message.chat_id == chat_row.id,
                    Message.tg_message_id == msg.id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            await s.commit()
            return False

        sender = getattr(msg, "sender", None)
        s.add(Message(
            account_id=account_id,
            chat_id=chat_row.id,
            tg_message_id=msg.id,
            sender_id=getattr(sender, "id", None) if sender else None,
            sender_name=_sender_name(sender),
            date=date,
            text=text,
            reply_to_msg_id=msg.reply_to_msg_id,
            has_media=has_media,
            media_type=mtype,
            is_outgoing=bool(msg.out),
            mentions_me=bool(getattr(msg, "mentioned", False)),
            forward_from=fwd_from,
        ))
        await s.commit()
    return True


def _dialog_entity_type(entity) -> str:
    if isinstance(entity, Channel):
        return "channel" if getattr(entity, "broadcast", False) else "group"
    if isinstance(entity, TLChat):
        return "group"
    if isinstance(entity, TLUser):
        return "user"
    return "unknown"


async def run_backfill(
    account_id: int,
    client: TelegramClient,
    limit_per_chat: int | None = None,
    max_dialogs: int | None = None,
    include_dms: bool | None = None,
    include_groups: bool | None = None,
    include_channels: bool | None = None,
    skip_bots: bool | None = None,
    skip_archived: bool | None = None,
) -> None:
    """Фоновая задача: загружает историю. Настройки берутся из Account если не переданы явно."""
    from ..db import SessionLocal as _SL
    from ..models import Account as _Acc

    # Загружаем настройки аккаунта
    async with _SL() as s:
        acc = await s.get(_Acc, account_id)
        if acc:
            limit_per_chat = limit_per_chat if limit_per_chat is not None else acc.scan_limit_per_chat
            max_dialogs = max_dialogs if max_dialogs is not None else acc.scan_max_dialogs
            include_dms = include_dms if include_dms is not None else acc.scan_include_dms
            include_groups = include_groups if include_groups is not None else acc.scan_include_groups
            include_channels = include_channels if include_channels is not None else acc.scan_include_channels
            skip_bots = skip_bots if skip_bots is not None else acc.scan_skip_bots
            skip_archived = skip_archived if skip_archived is not None else acc.scan_skip_archived
        else:
            limit_per_chat = limit_per_chat or LIMIT_PER_CHAT
            max_dialogs = max_dialogs or MAX_DIALOGS
            include_dms = True if include_dms is None else include_dms
            include_groups = True if include_groups is None else include_groups
            include_channels = True if include_channels is None else include_channels
            skip_bots = True if skip_bots is None else skip_bots
            skip_archived = True if skip_archived is None else skip_archived

    _state[account_id] = {
        "status": "running",
        "chats_done": 0,
        "chats_total": 0,
        "messages_added": 0,
        "skipped": 0,
        "last_error": "",
    }
    st = _state[account_id]

    try:
        all_dialogs = await client.get_dialogs(limit=max_dialogs, archived=False)
        if not skip_archived:
            try:
                archived = await client.get_dialogs(limit=max_dialogs, archived=True)
                all_dialogs = all_dialogs + archived
            except Exception:  # noqa: BLE001
                pass

        # Фильтруем по типу
        filtered = []
        for d in all_dialogs:
            e = d.entity
            etype = _dialog_entity_type(e)
            if not include_dms and etype == "user":
                st["skipped"] += 1
                continue
            if not include_groups and etype == "group":
                st["skipped"] += 1
                continue
            if not include_channels and etype == "channel":
                st["skipped"] += 1
                continue
            if skip_bots and isinstance(e, TLUser) and getattr(e, "bot", False):
                st["skipped"] += 1
                continue
            filtered.append(d)

        st["chats_total"] = len(filtered)
        log.info(
            "Backfill account=%s: %d dialogs (skipped %d), limit=%d/chat",
            account_id, len(filtered), st["skipped"], limit_per_chat,
        )

        for dialog in filtered:
            entity = dialog.entity
            added = 0
            try:
                async for msg in client.iter_messages(entity, limit=limit_per_chat):
                    if not msg or not msg.id:
                        continue
                    if await _save_message(account_id, msg, entity):
                        added += 1
                    await asyncio.sleep(0)
            except Exception:  # noqa: BLE001
                log.exception("Backfill: skip dialog %s", getattr(entity, "id", "?"))

            st["messages_added"] += added
            st["chats_done"] += 1
            log.debug(
                "Backfill account=%s: %d/%d chats, +%d msgs",
                account_id, st["chats_done"], st["chats_total"], added,
            )
            await asyncio.sleep(0.1)

        st["status"] = "done"
        log.info(
            "Backfill account=%s done: %d chats, %d messages",
            account_id, st["chats_done"], st["messages_added"],
        )
    except Exception as e:  # noqa: BLE001
        st["status"] = "error"
        st["last_error"] = str(e)
        log.exception("Backfill account=%s failed", account_id)
