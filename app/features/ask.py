"""Q&A-ассистент по личным Telegram-чатам пользователя.

История диалога — в БД (ask_sessions/ask_messages).
Контекст для LLM — last N реплик + найденные по ключевым словам сообщения юзера.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator

from sqlalchemy import and_, or_, select

from ..db import SessionLocal
from ..llm import complete_stream_messages
from ..models import AskMessage, AskSession, Chat, Message

# Максимум сообщений в истории диалога, передаваемых в LLM
MAX_HISTORY_TURNS = 12
# Максимум tg-сообщений из БД, добавляемых в контекст
MAX_CONTEXT_MSGS = 60
# Глубина поиска по tg-сообщениям (дни)
SEARCH_DAYS = 90

ASK_SYSTEM_PROMPT = (
    "Ты — персональный ИИ-ассистент SecondMe, который помогает пользователю "
    "разобраться в его собственных Telegram-чатах. "
    "Тебе передают релевантные сообщения из чатов пользователя и историю диалога. "
    "Отвечай конкретно, ссылайся на чаты и людей по именам, цитируй важные фрагменты. "
    "Если в контексте нет ответа — честно скажи 'в твоих чатах я этого не нашёл'. "
    "Не выдумывай факты. Используй markdown: ## заголовки, **жирный**, - списки."
)


_STOPWORDS = {
    "что", "как", "где", "когда", "почему", "зачем", "кто", "это", "был", "была", "было",
    "есть", "для", "при", "над", "под", "про", "без", "или", "и", "а", "но", "же", "ли",
    "мне", "мой", "моя", "моё", "мои", "ты", "вы", "он", "она", "оно", "они", "его", "её",
    "их", "нас", "вас", "не", "ни", "да", "уж", "то", "так", "там", "тут", "вот",
    "the", "and", "but", "for", "with", "what", "when", "where", "why", "how", "who",
}


def _extract_keywords(question: str) -> list[str]:
    """Достаём содержательные слова из вопроса для поиска по messages."""
    words = re.findall(r"[\wа-яёА-ЯЁ]{3,}", question.lower())
    seen = set()
    out: list[str] = []
    for w in words:
        if w in _STOPWORDS or w in seen:
            continue
        seen.add(w)
        out.append(w)
        if len(out) >= 6:
            break
    return out


def _fmt_msg(msg: Message, chat_title: str) -> str:
    """Форматирует одно сообщение для контекста LLM."""
    ts = msg.date.strftime("%Y-%m-%d %H:%M")
    # ВАЖНО: исходящие — это "Я", входящие — имя контакта/чата
    if msg.is_outgoing:
        sender = "Я"
    else:
        sender = msg.sender_name or chat_title or "Собеседник"
    text = (msg.text or "").replace("\n", " ").strip()
    if not text and msg.has_media:
        text = f"[{msg.media_type or 'медиа'}]"
    text = text[:500]
    return f"[{ts}] {sender}: {text}"


_CHAT_MENTION_RE = re.compile(
    r"(?:чат[ае]?|переписк[уаи]|общени[ея]|диалог[ае]?|контакт[ае]?)\s+(?:с|с\s+)?\s*([А-ЯЁA-Z][а-яёa-z]+(?:\s+[А-ЯЁA-Z][а-яёa-z]+)?)",
    re.IGNORECASE,
)
_PERSON_RE = re.compile(
    r"\b([А-ЯЁ][а-яё]{2,}(?:\s+[А-ЯЁ][а-яё]{2,}){0,2})\b"
)


async def _find_chat_by_name(account_id: int, name_hint: str) -> Chat | None:
    """Ищет чат с максимально похожим названием по подстроке."""
    async with SessionLocal() as s:
        rows = (
            await s.execute(
                select(Chat)
                .where(
                    Chat.account_id == account_id,
                    Chat.is_tracked.is_(True),
                    Chat.title.ilike(f"%{name_hint.strip()}%"),
                )
                .order_by(Chat.title)
                .limit(3)
            )
        ).scalars().all()
    return rows[0] if rows else None


async def _retrieve_full_chat(account_id: int, chat: Chat, days: int = 180, limit: int = 300) -> str:
    """Читает все сообщения конкретного чата — полный контекст переписки."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    async with SessionLocal() as s:
        msgs = (
            await s.execute(
                select(Message)
                .where(
                    Message.account_id == account_id,
                    Message.chat_id == chat.id,
                    Message.date >= cutoff,
                )
                .order_by(Message.date.asc())
                .limit(limit)
            )
        ).scalars().all()
    if not msgs:
        return ""
    lines = [f"# Полная переписка: {chat.title} (последние {days} дн., {len(msgs)} сообщ.)\n"]
    n_out = sum(1 for m in msgs if m.is_outgoing)
    n_in = len(msgs) - n_out
    lines.append(f"# Статистика: Я написал {n_out}, {chat.title or 'собеседник'} написал {n_in}\n")
    for m in msgs:
        lines.append(_fmt_msg(m, chat.title))
    return "\n".join(lines)


async def _retrieve_context(account_id: int, question: str) -> tuple[str, str | None]:
    """
    Возвращает (context_text, chat_name_if_found).
    Если вопрос про конкретный контакт — читает весь чат.
    Иначе — keyword-поиск по всем чатам.
    """
    # Пробуем найти упоминание конкретного человека/чата в вопросе
    chat_hint: str | None = None
    m = _CHAT_MENTION_RE.search(question)
    if m:
        chat_hint = m.group(1)
    else:
        # Ищем имена собственные (Иван Петров, Саша Батыршин и т.д.)
        names = _PERSON_RE.findall(question)
        for n in names:
            if len(n) >= 4 and n.lower() not in _STOPWORDS:
                chat_hint = n
                break

    if chat_hint:
        chat = await _find_chat_by_name(account_id, chat_hint)
        if chat:
            ctx = await _retrieve_full_chat(account_id, chat)
            if ctx:
                return ctx, chat.title

    # Fallback: keyword search по всем чатам
    keywords = _extract_keywords(question)
    if not keywords:
        return "", None

    cutoff = datetime.now(timezone.utc) - timedelta(days=SEARCH_DAYS)
    conds = [Message.text.ilike(f"%{kw}%") for kw in keywords]
    async with SessionLocal() as s:
        rows = (
            await s.execute(
                select(Message, Chat.title)
                .join(Chat, Message.chat_id == Chat.id)
                .where(
                    and_(
                        Message.account_id == account_id,
                        Message.date >= cutoff,
                        or_(*conds),
                    )
                )
                .order_by(Message.date.desc())
                .limit(MAX_CONTEXT_MSGS)
            )
        ).all()

    if not rows:
        return "", None

    lines: list[str] = []
    for msg, chat_title in reversed(rows):
        lines.append(_fmt_msg(msg, chat_title))

    return "\n".join(lines), None


async def _build_system_with_context(account_id: int, question: str) -> str:
    """System-промпт = базовый + найденный контекст из Telegram."""
    ctx, chat_name = await _retrieve_context(account_id, question)
    if ctx:
        header = (
            f"# Полная переписка с контактом «{chat_name}»\n"
            if chat_name else
            "# Релевантные сообщения из Telegram-чатов пользователя\n"
        )
        return (
            ASK_SYSTEM_PROMPT + "\n\n"
            + header
            + "(формат: [дата время] Отправитель: текст)\n"
            + "ВАЖНО: 'Я' = сам пользователь (исходящие), остальные имена = собеседники.\n\n"
            + ctx
        )
    return ASK_SYSTEM_PROMPT + "\n\n(По данному вопросу релевантных сообщений в чатах не найдено.)"


def _history_to_messages(history: list[AskMessage]) -> list[dict]:
    """Конвертирует историю БД в OpenAI-совместимый messages array."""
    msgs = []
    for m in history[-MAX_HISTORY_TURNS * 2:]:  # последние N пар
        msgs.append({"role": m.role, "content": m.content})
    return msgs


# ---------------------------------------------------------------------------
#  Public API
# ---------------------------------------------------------------------------

async def list_sessions(account_id: int) -> list[dict]:
    async with SessionLocal() as s:
        rows = (
            await s.execute(
                select(AskSession)
                .where(AskSession.account_id == account_id)
                .order_by(AskSession.updated_at.desc())
                .limit(50)
            )
        ).scalars().all()
        return [
            {
                "id": r.id,
                "title": r.title,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ]


async def create_session(account_id: int, title: str = "Новый диалог") -> int:
    async with SessionLocal() as s:
        sess = AskSession(account_id=account_id, title=title[:255] or "Новый диалог")
        s.add(sess)
        await s.commit()
        await s.refresh(sess)
        return sess.id


async def get_history(account_id: int, session_id: int) -> list[dict]:
    async with SessionLocal() as s:
        sess = await s.get(AskSession, session_id)
        if not sess or sess.account_id != account_id:
            return []
        rows = (
            await s.execute(
                select(AskMessage)
                .where(AskMessage.session_id == session_id)
                .order_by(AskMessage.id.asc())
            )
        ).scalars().all()
        return [
            {"role": r.role, "content": r.content, "id": r.id}
            for r in rows
        ]


async def delete_session(account_id: int, session_id: int) -> bool:
    async with SessionLocal() as s:
        sess = await s.get(AskSession, session_id)
        if not sess or sess.account_id != account_id:
            return False
        await s.delete(sess)
        await s.commit()
        return True


async def ask_stream(
    account_id: int, session_id: int | None, question: str
) -> AsyncGenerator[tuple[str, str | int], None]:
    """
    Стримит ответ ассистента.

    Yields:
      ('session', session_id)  — отправляется первой (особенно важно для новой сессии)
      ('status',  text)        — статусы прогресса
      ('chunk',   text)        — фрагменты ответа LLM
      ('error',   text)        — фатальная ошибка
    """
    question = (question or "").strip()
    if not question:
        yield ("error", "Пустой вопрос")
        return

    # 1) Резолвим / создаём сессию
    async with SessionLocal() as s:
        sess: AskSession | None = None
        if session_id:
            sess = await s.get(AskSession, session_id)
            if sess and sess.account_id != account_id:
                sess = None
        if not sess:
            title = (question[:60] + "…") if len(question) > 60 else question
            sess = AskSession(account_id=account_id, title=title)
            s.add(sess)
            await s.commit()
            await s.refresh(sess)
        session_id = sess.id

        # 2) Сохраняем вопрос пользователя
        s.add(AskMessage(session_id=session_id, role="user", content=question))
        sess.updated_at = datetime.now(timezone.utc)
        await s.commit()

        # Если у сессии всё ещё дефолтный title, обновим
        if sess.title in ("Новый диалог", "") or sess.title is None:
            sess.title = (question[:60] + "…") if len(question) > 60 else question
            await s.commit()

        # 3) Загружаем историю для контекста
        history_rows = (
            await s.execute(
                select(AskMessage)
                .where(AskMessage.session_id == session_id)
                .order_by(AskMessage.id.asc())
            )
        ).scalars().all()

    yield ("session", session_id)
    yield ("status", "Ищу релевантные сообщения в твоих чатах…")

    # 4) Строим system-промпт с контекстом + messages array с историей
    # history_rows включает только-что добавленный user-вопрос в конце
    history_for_messages = list(history_rows)  # все, включая текущий user
    system_with_ctx = await _build_system_with_context(account_id, question)
    messages = _history_to_messages(history_for_messages)

    yield ("status", "Думаю над ответом…")

    # 5) Стримим LLM-ответ + параллельно копим в буфер чтобы сохранить
    buf: list[str] = []
    try:
        async for chunk in complete_stream_messages(
            messages, system=system_with_ctx, account_id=account_id
        ):
            buf.append(chunk)
            yield ("chunk", chunk)
    except Exception as e:  # noqa: BLE001
        yield ("error", f"Ошибка LLM: {e}")
        return

    answer = "".join(buf).strip()
    if not answer:
        yield ("error", "Пустой ответ от модели")
        return

    # 6) Сохраняем ответ ассистента
    async with SessionLocal() as s:
        s.add(AskMessage(session_id=session_id, role="assistant", content=answer))
        sess = await s.get(AskSession, session_id)
        if sess:
            sess.updated_at = datetime.now(timezone.utc)
        await s.commit()
