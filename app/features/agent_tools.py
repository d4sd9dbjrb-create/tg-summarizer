"""Инструменты (Tools) для Agentic LLM.

Все функции возвращают читаемый текст (не raw JSON), чтобы LLM мог
легко понять содержимое без лишнего парсинга.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, select

from ..db import SessionLocal
from ..models import Chat, Message

log = logging.getLogger(__name__)

_PAGE_SIZE = 50  # Сколько сообщений возвращать за один вызов


def _fmt_date(dt: datetime | None) -> str:
    if not dt:
        return "—"
    return dt.strftime("%d.%m %H:%M")


def _truncate(text: str, n: int = 120) -> str:
    if not text:
        return ""
    text = text.replace("\n", " ").strip()
    return text[:n] + "…" if len(text) > n else text


async def get_chats_overview(account_id: int, period_hours: int = 24) -> str:
    """
    Возвращает список активных чатов за период с превью последних сообщений.
    Используй это ПЕРВЫМ ДЕЛОМ чтобы понять что вообще есть и куда смотреть.

    Формат строки: [chat_id=N] ТИП «Название» — X сообщ. — последнее: «текст»
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=period_hours)

    async with SessionLocal() as s:
        # Чаты + количество сообщений
        q_chats = (
            select(
                Chat.id,
                Chat.title,
                Chat.type,
                func.count(Message.id).label("msg_count"),
                func.max(Message.date).label("last_date"),
            )
            .join(Message, Chat.id == Message.chat_id)
            .where(
                Chat.account_id == account_id,
                Message.account_id == account_id,
                Message.date >= start,
                Message.date <= end,
            )
            .group_by(Chat.id, Chat.title, Chat.type)
            .order_by(func.count(Message.id).desc())
            .limit(15)
        )
        rows = (await s.execute(q_chats)).all()

        if not rows:
            return f"За последние {period_hours}ч сообщений не найдено."

        # Для каждого чата — последнее сообщение
        chat_ids = [r.id for r in rows]
        subq = (
            select(Message.chat_id, func.max(Message.id).label("max_id"))
            .where(Message.chat_id.in_(chat_ids), Message.account_id == account_id)
            .group_by(Message.chat_id)
            .subquery()
        )
        q_last = (
            select(Message)
            .join(subq, Message.id == subq.c.max_id)
        )
        last_msgs = {m.chat_id: m for m in (await s.execute(q_last)).scalars().all()}

    total_msgs = sum(r.msg_count for r in rows)
    lines = [
        f"Найдено {len(rows)} активных чатов за последние {period_hours}ч "
        f"(всего {total_msgs} сообщений).\n"
    ]

    type_emoji = {"private": "👤", "group": "👥", "channel": "📢"}
    for r in rows:
        em = type_emoji.get(r.type, "💬")
        last = last_msgs.get(r.id)
        preview = ""
        if last:
            who = "Я" if last.is_outgoing else (last.sender_name or "?")
            preview = f"{who}: «{_truncate(last.text, 80)}»"
        lines.append(
            f"[chat_id={r.id}] {em} {r.type} «{r.title}» — "
            f"{r.msg_count} сообщ. — {_fmt_date(r.last_date)} — {preview}"
        )

    return "\n".join(lines)


async def read_chat_messages(
    account_id: int,
    chat_id: int,
    limit: int = _PAGE_SIZE,
    offset: int = 0,
    date_from: str | None = None,
    date_to: str | None = None,
    period_hours: int = 48,
) -> str:
    """
    Читает сообщения из конкретного чата (chat_id из get_chats_overview).

    Параметры:
    - limit: сколько сообщений (макс 100, по умолчанию 50)
    - offset: пропустить первые N сообщений (для пагинации)
    - period_hours: за сколько часов назад читать (по умолчанию 48). Используется
      если date_from не задан. Передай 24 для дневного дайджеста, 168 для недельного.
    - date_from / date_to: точный фильтр по дате ISO 8601 (переопределяет period_hours)

    Если в конце написано «(ещё N сообщений)» — вызови снова с offset += limit.

    Формат каждой строки: [DD.MM HH:MM] КТО: текст
    """
    async with SessionLocal() as s:
        # Проверяем что чат принадлежит аккаунту и берём его заголовок
        chat_row = (
            await s.execute(select(Chat).where(Chat.id == chat_id, Chat.account_id == account_id))
        ).scalar_one_or_none()
        if not chat_row:
            return f"Чат chat_id={chat_id} не найден для данного аккаунта."

        cond = [
            Message.account_id == account_id,
            Message.chat_id == chat_id,
        ]
        if date_from:
            try:
                cond.append(Message.date >= datetime.fromisoformat(date_from))
            except ValueError:
                pass
        else:
            # Если дата не задана — читаем только за period_hours, не всё время!
            cond.append(Message.date >= datetime.now(timezone.utc) - timedelta(hours=period_hours))
        if date_to:
            try:
                cond.append(Message.date <= datetime.fromisoformat(date_to))
            except ValueError:
                pass

        # Считаем всего
        total = (
            await s.execute(
                select(func.count()).select_from(Message).where(and_(*cond))
            )
        ).scalar_one()

        rows = (
            await s.execute(
                select(Message)
                .where(and_(*cond))
                .order_by(Message.date.asc())
                .limit(min(limit, 100))
                .offset(offset)
            )
        ).scalars().all()

    if not rows:
        return f"Сообщений в чате «{chat_row.title}» по заданным фильтрам не найдено."

    lines = [
        f"=== Чат «{chat_row.title}» ({chat_row.type}) ===",
        f"Показано {offset + 1}–{offset + len(rows)} из {total} сообщений",
        "",
    ]
    for m in rows:
        who = "Я" if m.is_outgoing else (m.sender_name or "Собеседник")
        media_note = f" [{m.media_type or 'медиа'}]" if m.has_media and not m.text else ""
        fwd = f" (переслано от {m.forward_from})" if m.forward_from else ""
        lines.append(f"[{_fmt_date(m.date)}] {who}: {m.text}{media_note}{fwd}")

    remaining = total - (offset + len(rows))
    if remaining > 0:
        lines.append(f"\n(ещё {remaining} сообщений — вызови снова с offset={offset + len(rows)})")

    return "\n".join(lines)


async def search_messages(
    account_id: int,
    query_str: str,
    period_hours: int = 48,
    is_me: bool | None = None,
    limit: int = 30,
) -> str:
    """
    Ищет сообщения по ключевым словам за период.
    Полезно для поиска обещаний, договорённостей, упоминаний конкретных тем.

    Параметры:
    - query_str: слово или фраза для поиска
    - period_hours: за сколько часов искать (по умолчанию 48)
    - is_me: true — только мои сообщения, false — только чужие, null — все
    - limit: макс. количество результатов
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=period_hours)

    async with SessionLocal() as s:
        cond = [
            Message.account_id == account_id,
            Message.date >= start,
            Message.date <= end,
            Message.text.ilike(f"%{query_str}%"),
        ]
        if is_me is not None:
            cond.append(Message.is_outgoing == is_me)

        rows = (
            await s.execute(
                select(Message, Chat.title, Chat.type)
                .join(Chat, Message.chat_id == Chat.id)
                .where(and_(*cond))
                .order_by(Message.date.desc())
                .limit(limit)
            )
        ).all()

    if not rows:
        return f"По запросу «{query_str}» за {period_hours}ч ничего не найдено."

    lines = [f"Найдено {len(rows)} сообщений по запросу «{query_str}»:\n"]
    for msg, chat_title, chat_type in rows:
        who = "Я" if msg.is_outgoing else (msg.sender_name or "?")
        lines.append(
            f"[{_fmt_date(msg.date)}] [{chat_title}] {who}: {_truncate(msg.text, 150)}"
        )
    return "\n".join(lines)


async def get_chat_stats(
    account_id: int,
    chat_id: int,
    period_hours: int = 168,
) -> str:
    """
    Быстрая статистика по одному чату: топ отправителей, активность по дням.
    Полезно чтобы решить — стоит ли читать полные сообщения этого чата.
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=period_hours)

    async with SessionLocal() as s:
        chat_row = (
            await s.execute(select(Chat).where(Chat.id == chat_id, Chat.account_id == account_id))
        ).scalar_one_or_none()
        if not chat_row:
            return f"Чат chat_id={chat_id} не найден."

        cond = [
            Message.account_id == account_id,
            Message.chat_id == chat_id,
            Message.date >= start,
        ]

        # Топ отправителей
        top_senders = (
            await s.execute(
                select(Message.sender_name, func.count(Message.id).label("cnt"))
                .where(and_(*cond))
                .group_by(Message.sender_name)
                .order_by(func.count(Message.id).desc())
                .limit(10)
            )
        ).all()

        # Всего
        total = (
            await s.execute(select(func.count()).select_from(Message).where(and_(*cond)))
        ).scalar_one()

        # Мои vs чужие
        my_count = (
            await s.execute(
                select(func.count())
                .select_from(Message)
                .where(and_(*cond, Message.is_outgoing == True))  # noqa: E712
            )
        ).scalar_one()

    lines = [
        f"=== Статистика «{chat_row.title}» ({chat_row.type}) за {period_hours}ч ===",
        f"Всего сообщений: {total} (мои: {my_count}, входящие: {total - my_count})",
        "",
        "Топ отправителей:",
    ]
    for name, cnt in top_senders:
        label = "Я" if name is None else name
        lines.append(f"  {label}: {cnt} сообщений")

    return "\n".join(lines)
