"""Per-account сводки на базе LLM (Agentic Workflow)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator

from sqlalchemy import and_, func, select

from ..db import SessionLocal
from ..models import Chat, Message, Summary
from .agent import run_agentic_loop

log = logging.getLogger(__name__)

# TTL кеша готовых сводок (сек). 1 час — баланс между свежестью и скоростью.
CACHE_TTL_SECONDS = 3600

async def _save(account_id: int, kind: str, chat_id: int | None, start, end, content: str):
    async with SessionLocal() as s:
        s.add(
            Summary(
                account_id=account_id,
                kind=kind,
                chat_id=chat_id,
                period_start=start,
                period_end=end,
                content=content,
            )
        )
        await s.commit()


async def _run_agent_sync(account_id: int, prompt: str) -> str:
    """Запускает агента и склеивает стрим в одну строку (для не-streaming эндпоинтов)."""
    result = []
    async for event_type, chunk in run_agentic_loop(account_id, prompt):
        if event_type == "chunk":
            result.append(chunk)
        elif event_type == "error":
            return f"Ошибка генерации: {chunk}"
    return "".join(result)


async def daily_summary(account_id: int, chat_id: int | None = None) -> str:
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=24)
    chat_hint = f" по чату с ID {chat_id}" if chat_id else " по всем активным чатам"
    prompt = (
        f"Сделай ежедневный дайджест Telegram-сообщений{chat_hint} за последние 24 часа. "
        "Структура:\n"
        "1. **Главное** — 3-7 буллетов про самое важное.\n"
        "2. **По чатам** — строчка на каждый активный чат.\n"
        "3. **Требует внимания** — вопросы и просьбы лично ко мне, дедлайны.\n"
        "4. **Прочее интересное** — ссылки, ресурсы.\n"
    )
    text = await _run_agent_sync(account_id, prompt)
    await _save(account_id, "daily", chat_id, start, end, text)
    return text


async def weekly_summary(account_id: int, chat_id: int | None = None) -> str:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=7)
    chat_hint = f" по чату с ID {chat_id}" if chat_id else " по всем активным чатам"
    prompt = (
        f"Сделай еженедельный дайджест{chat_hint} за последние 7 дней: ключевые темы, тренды, повторяющиеся вопросы, "
        "решения, предстоящие события."
    )
    text = await _run_agent_sync(account_id, prompt)
    await _save(account_id, "weekly", chat_id, start, end, text)
    return text


async def catchup_summary(account_id: int, hours: int = 12) -> str:
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    prompt = (
        f"Я был офлайн ~{hours}ч. Сделай catch-up: что пропустил, на что нужно "
        "ответить в первую очередь, что можно проигнорировать."
    )
    text = await _run_agent_sync(account_id, prompt)
    await _save(account_id, "catchup", None, start, end, text)
    return text


async def topics(account_id: int, days: int = 1, chat_id: int | None = None) -> str:
    chat_hint = f" в чате с ID {chat_id}" if chat_id else ""
    prompt = (
        f"Кластеризуй сообщения{chat_hint} за последние {days} дней по темам. Для каждой: название, 1-2 предложения, "
        "кто участвовал, статус (активна/закрыта/требует ответа)."
    )
    return await _run_agent_sync(account_id, prompt)


async def action_items(account_id: int, days: int = 1) -> str:
    prompt = (
        f"Выдели action-items для меня за последние {days} дней: вопросы на ответ, просьбы, обещания, дедлайны. "
        "Формат: чекбокс-список с автором и чатом."
    )
    return await _run_agent_sync(account_id, prompt)


async def important_week(account_id: int) -> str:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=7)
    prompt = (
        "Найди ТОП-10 САМЫХ ВАЖНЫХ событий за последние 7 дней в моих чатах. "
        "Важное — это: новости с серьёзным влиянием, договорённости, дедлайны, "
        "вопросы лично ко мне, конфликты, новые знакомства, важные ссылки/ресурсы. "
        "Каждый пункт: эмодзи + краткое название + 1-2 предложения контекста + чат + дата. "
        "В конце отдельным блоком — ✅ ЧТО НАДО СДЕЛАТЬ НА ЭТОЙ НЕДЕЛЕ (action items)."
    )
    text = await _run_agent_sync(account_id, prompt)
    await _save(account_id, "important", None, start, end, text)
    return text


async def full_stream(
    account_id: int,
    kind: str,
    chat_id: int | None = None,
    hours: int = 12,
    senders: list[str] | None = None,
    force: bool = False,
) -> AsyncGenerator[tuple[str, str], None]:
    """
    Стриминг ответов для веба, использует Agentic Loop.
    """
    sender_hint = f" (только сообщения от: {', '.join(senders)})" if senders else ""
    chat_hint = f" (чат ID {chat_id})" if chat_id else ""
    time_hint = f" за последние {hours} часов" if kind == "catchup" else ""

    prompts = {
        "daily": f"Сделай подробный ежедневный дайджест Telegram-сообщений{chat_hint}{sender_hint} за 24 часа. Пиши развёрнуто.",
        "weekly": f"Сделай подробный еженедельный дайджест{chat_hint}{sender_hint} за 7 дней. Пиши развёрнуто.",
        "timeline": f"Сделай хронологию переписки по дням{chat_hint}{sender_hint} за 7 дней.",
        "catchup": f"Я был офлайн ~{hours}ч. Сделай подробный catch-up что я пропустил{chat_hint}{sender_hint}.",
        "important": f"Найди ТОП-10 САМЫХ ВАЖНЫХ событий недели в моих чатах{chat_hint}{sender_hint}.",
        "actions": f"Составь подробный список action-items для меня из переписки за последние сутки{chat_hint}{sender_hint}.",
        "topics": f"Кластеризуй сообщения по темам за 24 часа{chat_hint}{sender_hint}.",
    }

    prompt = prompts.get(kind)
    if not prompt:
        yield ("error", f"Unknown kind: {kind}")
        return

    # Запускаем агентский цикл и транслируем его события
    async for event_type, chunk in run_agentic_loop(account_id, prompt, kind=kind):
        yield (event_type, chunk)

    # TODO: Реализовать сохранение в кеш после завершения успешной генерации
    # (в старой версии кеш сохранялся прямо внутри full_stream)


async def translate(text: str, lang: str = "English") -> str:
    from ..llm import complete
    return await complete(
        f"Переведи следующий текст на {lang}, сохраняя форматирование:\n\n{text}",
        system="Ты переводчик. Переводи аккуратно, сохраняй markdown.",
    )

async def prepare_for_stream(
    account_id: int,
    kind: str,
    chat_id: int | None = None,
    hours: int = 12,
    senders: list[str] | None = None,
) -> tuple[str | None, str | None]:
    """Заглушка для совместимости с ask.py / web_routes."""
    # В агентском подходе предварительная подготовка промпта (map phase) не требуется.
    # Возвращаем просто вид запроса.
    return kind, None
