"""Полезные функции, которых нет в Telegram.

Содержит:
- silent_contacts: кому давно не писал
- promises_prompt:  поиск невыполненных обещаний (для стрима через LLM)
- contact_card_prompt: AI-карточка контакта (для стрима)
- personal_analytics: цифры/гистограммы для дашборда аналитики
- export_chat_md: рендер чата в markdown
- extract_links: все ссылки за период с контекстом
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator

from sqlalchemy import and_, desc, func, select

from ..db import SessionLocal
from ..models import Chat, Message


# ════════════════════════════════════════════════════════════════════════════
#                           1) Кому давно не писал
# ════════════════════════════════════════════════════════════════════════════

async def silent_contacts(
    account_id: int,
    limit: int = 50,
    only_dm: bool = True,
) -> list[dict]:
    """
    Возвращает чаты отсортированные по давности ПОСЛЕДНЕГО ИСХОДЯЩЕГО сообщения.
    Самые «забытые» — сверху.
    """
    async with SessionLocal() as s:
        # last outgoing message per chat
        last_out_subq = (
            select(
                Message.chat_id,
                func.max(Message.date).label("last_out_at"),
            )
            .where(
                Message.account_id == account_id,
                Message.is_outgoing.is_(True),
            )
            .group_by(Message.chat_id)
            .subquery()
        )

        # last any message per chat (чтобы понять — есть ли вообще активность)
        last_any_subq = (
            select(
                Message.chat_id,
                func.max(Message.date).label("last_any_at"),
            )
            .where(Message.account_id == account_id)
            .group_by(Message.chat_id)
            .subquery()
        )

        cond = [
            Chat.account_id == account_id,
            Chat.is_tracked.is_(True),
        ]
        if only_dm:
            cond.append(Chat.type == "user")

        rows = (
            await s.execute(
                select(
                    Chat.id,
                    Chat.title,
                    Chat.type,
                    Chat.username,
                    last_out_subq.c.last_out_at,
                    last_any_subq.c.last_any_at,
                )
                .join(last_any_subq, last_any_subq.c.chat_id == Chat.id)
                .outerjoin(last_out_subq, last_out_subq.c.chat_id == Chat.id)
                .where(and_(*cond))
                .order_by(last_out_subq.c.last_out_at.asc().nulls_first())
                .limit(limit)
            )
        ).all()

    now = datetime.now(timezone.utc)
    out = []
    for r in rows:
        last_out = r.last_out_at
        last_any = r.last_any_at
        days_silent = None
        if last_out:
            days_silent = (now - last_out).days
        out.append({
            "chat_id": r.id,
            "title": r.title or "—",
            "type": r.type,
            "username": r.username,
            "last_out_at": last_out.isoformat() if last_out else None,
            "last_any_at": last_any.isoformat() if last_any else None,
            "days_silent": days_silent,
            "never_replied": last_out is None,
        })
    return out


# ════════════════════════════════════════════════════════════════════════════
#                       2) Невыполненные обещания
# ════════════════════════════════════════════════════════════════════════════

PROMISES_KEYWORDS = (
    "обещаю", "обещал", "сделаю", "пришлю", "отправлю", "напишу", "позвоню",
    "вышлю", "скину", "перезвоню", "проверю", "посмотрю", "разберусь",
    "уточню", "договоримся", "договорился", "сделать", "забуду", "не забуду",
)

PROMISES_SYSTEM = (
    "Ты помогаешь пользователю найти его НЕВЫПОЛНЕННЫЕ обещания в личных переписках. "
    "Ты получаешь его исходящие сообщения с контекстом чата. "
    "Найди обещания/обязательства (\"сделаю\", \"пришлю\", \"перезвоню\" и т.п.) "
    "и пометь те, по которым НЕТ follow-up — то есть пользователь так и не сделал."
)

PROMISES_PROMPT = """Проанализируй мои исходящие сообщения за последние {days} дней и найди:

1. **Открытые обещания** — где я обещал что-то сделать/прислать/ответить и НЕ ВИДНО подтверждения что я это выполнил.
2. Для каждого: цитата + кому + когда + что именно обещал + почему считаешь невыполненным.

Структурируй ответ как markdown-список под заголовком ## 🎯 Незакрытые обещания.
Если пусто — напиши "Все обещания выполнены 🎉".
Не выдумывай — только то что есть в тексте.

Сообщения:
{transcript}
"""


async def promises_prompt(account_id: int, days: int = 14) -> tuple[str, str | None]:
    """
    Готовит промпт для LLM-стрима «невыполненные обещания».
    Возвращает (prompt, error). prompt = None если ошибка.
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)

    async with SessionLocal() as s:
        # Берём только исходящие, где есть подозрительные ключевые слова
        kw_clause = " OR ".join([f"LOWER(text) LIKE '%{k}%'" for k in PROMISES_KEYWORDS])
        # SQLAlchemy ORM — соберём через or_
        from sqlalchemy import or_
        kw_filters = [func.lower(Message.text).like(f"%{k}%") for k in PROMISES_KEYWORDS]

        rows = (
            await s.execute(
                select(Message, Chat.title, Chat.type)
                .join(Chat, Chat.id == Message.chat_id)
                .where(
                    Message.account_id == account_id,
                    Message.is_outgoing.is_(True),
                    Message.date >= start,
                    Message.date < end,
                    Message.text != "",
                    or_(*kw_filters),
                )
                .order_by(Message.date.asc())
                .limit(500)
            )
        ).all()

    if not rows:
        return "", "Обещаний не найдено за выбранный период."

    # Группируем по чату для контекста
    lines = [f"# Мои сообщения за последние {days} дней\n"]
    last_chat = None
    for m, title, ctype in rows:
        if title != last_chat:
            kind = "DM" if ctype == "user" else ctype
            lines.append(f"\n## {kind}: {title}")
            last_chat = title
        d = m.date.strftime("%Y-%m-%d %H:%M")
        text = (m.text or "").strip().replace("\n", " ")[:300]
        lines.append(f"- [{d}] {text}")

    transcript = "\n".join(lines)
    if len(transcript) > 60000:
        transcript = transcript[-60000:]

    return PROMISES_PROMPT.format(days=days, transcript=transcript), None


# ════════════════════════════════════════════════════════════════════════════
#                       3) Карточка контакта (CRM)
# ════════════════════════════════════════════════════════════════════════════

CONTACT_CARD_PROMPT = """Составь подробную карточку этого контакта на основе нашей переписки.

Структурируй как markdown:

## 👤 Кто это
Кто такой человек, как мы общаемся, какой тон.

## 🔑 Ключевые факты
Что важного я знаю о нём (день рождения, семья, работа, увлечения, привычки) — только то что упоминалось в чате.

## 💬 О чём обычно общаемся
3–7 главных тем нашего общения с примерами.

## 📌 Незакрытые вопросы
Что осталось без ответа, что я ему обещал, что он мне обещал.

## 🎯 Совет для следующего разговора
Что освежить, о чём спросить, что не забыть.

Не выдумывай. Если данных мало — честно скажи.

Имя контакта: {name}
Период: {period}
Всего сообщений: {n_msgs} (мои: {n_out}, его: {n_in})

=== ПЕРЕПИСКА ===
{transcript}
"""


async def contact_card_prompt(
    account_id: int,
    chat_id: int,
    days: int = 365,
) -> tuple[str, str | None]:
    """Готовит промпт для AI-карточки контакта."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)

    async with SessionLocal() as s:
        chat = (
            await s.execute(
                select(Chat).where(Chat.id == chat_id, Chat.account_id == account_id)
            )
        ).scalar_one_or_none()
        if not chat:
            return "", "Чат не найден"

        msgs = (
            await s.execute(
                select(Message)
                .where(
                    Message.account_id == account_id,
                    Message.chat_id == chat_id,
                    Message.date >= start,
                    Message.date < end,
                )
                .order_by(Message.date.asc())
                .limit(2000)
            )
        ).scalars().all()

    if not msgs:
        return "", "Нет сообщений за выбранный период."

    n_out = sum(1 for m in msgs if m.is_outgoing)
    n_in = len(msgs) - n_out

    lines = []
    for m in msgs:
        d = m.date.strftime("%m-%d %H:%M")
        who = "Я" if m.is_outgoing else (chat.title or "Он")
        text = (m.text or "").strip().replace("\n", " ")[:400]
        if not text and m.has_media:
            text = f"[{m.media_type or 'медиа'}]"
        if not text:
            continue
        lines.append(f"[{d}] {who}: {text}")

    transcript = "\n".join(lines)
    if len(transcript) > 80000:
        transcript = transcript[-80000:]

    return CONTACT_CARD_PROMPT.format(
        name=chat.title or "—",
        period=f"последние {days} дней",
        n_msgs=len(msgs),
        n_out=n_out,
        n_in=n_in,
        transcript=transcript,
    ), None


# ════════════════════════════════════════════════════════════════════════════
#                          4) Personal analytics
# ════════════════════════════════════════════════════════════════════════════

async def personal_analytics(account_id: int, days: int = 90) -> dict:
    """
    Возвращает агрегаты для графиков:
    - by_hour: 24 значения (сообщений в этом часу)
    - by_weekday: 7 значений (Mon..Sun)
    - top_contacts: топ-10 по числу сообщений за период
    - totals: outgoing / incoming / total
    - avg_response_minutes: среднее время моего ответа на входящие
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)

    by_hour = [0] * 24
    by_weekday = [0] * 7

    async with SessionLocal() as s:
        msgs = (
            await s.execute(
                select(Message.date, Message.is_outgoing, Message.chat_id)
                .where(
                    Message.account_id == account_id,
                    Message.date >= start,
                    Message.date < end,
                )
                .order_by(Message.date.asc())
            )
        ).all()

        # top contacts
        tops = (
            await s.execute(
                select(Chat.id, Chat.title, Chat.type, func.count(Message.id).label("n"))
                .join(Message, Message.chat_id == Chat.id)
                .where(
                    Message.account_id == account_id,
                    Message.date >= start,
                    Message.date < end,
                    Chat.type == "user",  # только DM
                )
                .group_by(Chat.id, Chat.title, Chat.type)
                .order_by(desc("n"))
                .limit(10)
            )
        ).all()

    # Заполняем гистограммы + считаем response time
    n_out = n_in = 0
    last_incoming_per_chat: dict[int, datetime] = {}
    response_deltas: list[float] = []

    for d, is_out, chat_id in msgs:
        local_hour = d.hour  # UTC; для простоты, можно потом учесть TZ
        by_hour[local_hour] += 1
        by_weekday[d.weekday()] += 1
        if is_out:
            n_out += 1
            # если есть последний входящий — считаем дельту
            t_in = last_incoming_per_chat.get(chat_id)
            if t_in:
                delta_sec = (d - t_in).total_seconds()
                if 5 < delta_sec < 24 * 3600:  # игнорируем мгновенные и >24h
                    response_deltas.append(delta_sec / 60.0)
                last_incoming_per_chat.pop(chat_id, None)
        else:
            n_in += 1
            last_incoming_per_chat[chat_id] = d

    avg_response_minutes = (
        round(sum(response_deltas) / len(response_deltas), 1)
        if response_deltas else None
    )

    return {
        "period_days": days,
        "totals": {"outgoing": n_out, "incoming": n_in, "total": len(msgs)},
        "by_hour": by_hour,
        "by_weekday": by_weekday,
        "top_contacts": [
            {"chat_id": t.id, "title": t.title or "—", "count": t.n}
            for t in tops
        ],
        "avg_response_minutes": avg_response_minutes,
        "response_samples": len(response_deltas),
    }


# ════════════════════════════════════════════════════════════════════════════
#                           5) Экспорт чата в markdown
# ════════════════════════════════════════════════════════════════════════════

async def export_chat_md(
    account_id: int,
    chat_id: int,
    days: int = 365,
) -> tuple[str, str]:
    """
    Возвращает (filename, markdown_text).
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)

    async with SessionLocal() as s:
        chat = (
            await s.execute(
                select(Chat).where(Chat.id == chat_id, Chat.account_id == account_id)
            )
        ).scalar_one_or_none()
        if not chat:
            return "", ""

        msgs = (
            await s.execute(
                select(Message)
                .where(
                    Message.account_id == account_id,
                    Message.chat_id == chat_id,
                    Message.date >= start,
                    Message.date < end,
                )
                .order_by(Message.date.asc())
            )
        ).scalars().all()

    title = chat.title or "chat"
    safe_title = re.sub(r"[^\w\d_-]+", "_", title).strip("_") or "chat"
    fname = f"{safe_title}_{end.strftime('%Y-%m-%d')}.md"

    lines = [
        f"# Переписка: {title}",
        f"_Период: {start.date()} — {end.date()}_",
        f"_Всего сообщений: {len(msgs)}_",
        "",
    ]

    last_date = None
    for m in msgs:
        d = m.date.date()
        if d != last_date:
            lines.append(f"\n## {d.isoformat()}")
            last_date = d
        time = m.date.strftime("%H:%M")
        who = "**Я**" if m.is_outgoing else f"**{m.sender_name or title}**"
        text = (m.text or "").strip()
        if not text and m.has_media:
            text = f"_[{m.media_type or 'медиа'}]_"
        if not text:
            continue
        lines.append(f"- `{time}` {who}: {text}")

    return fname, "\n".join(lines)


# ════════════════════════════════════════════════════════════════════════════
#                            6) Сборка ссылок
# ════════════════════════════════════════════════════════════════════════════

URL_RE = re.compile(r"https?://[^\s<>\"'`)]+", re.IGNORECASE)


async def extract_links(account_id: int, days: int = 30, limit: int = 200) -> list[dict]:
    """Все URL из сообщений за период с контекстом."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)

    async with SessionLocal() as s:
        rows = (
            await s.execute(
                select(Message, Chat.title, Chat.type)
                .join(Chat, Chat.id == Message.chat_id)
                .where(
                    Message.account_id == account_id,
                    Message.date >= start,
                    Message.date < end,
                    Message.text != "",
                )
                .order_by(Message.date.desc())
                .limit(5000)
            )
        ).all()

    out = []
    seen_urls: set[str] = set()
    for m, title, ctype in rows:
        urls = URL_RE.findall(m.text or "")
        if not urls:
            continue
        for u in urls:
            u_clean = u.rstrip(".,);!?\"'")
            if u_clean in seen_urls:
                continue
            seen_urls.add(u_clean)
            # короткий контекст: текст без URL, первые 200 chars
            ctx = (m.text or "").replace(u, "").strip()[:200]
            out.append({
                "url": u_clean,
                "chat_title": title,
                "chat_type": ctype,
                "sender": "Я" if m.is_outgoing else (m.sender_name or title),
                "date": m.date.isoformat(),
                "context": ctx,
            })
            if len(out) >= limit:
                return out
    return out
