"""Агентский цикл (Agentic Loop): DeepSeek (function calling) + Gemini (native tools)."""
from __future__ import annotations

import json
import logging
import re
from typing import AsyncGenerator

import httpx

from ..llm import _resolve_creds, settings
from . import agent_tools

log = logging.getLogger(__name__)

_DSML_RE = re.compile(r'<｜｜DSML｜｜[\s\S]*', re.DOTALL)


def _strip_dsml(text: str) -> str:
    """Убирает DeepSeek DSML tool_calls из текста (иногда модель выводит их в content)."""
    return _DSML_RE.sub('', text or '').strip()


async def _stream_clean(
    client: "httpx.AsyncClient",
    url: str,
    headers: dict,
    payload: dict,
) -> AsyncGenerator[tuple[str, str], None]:
    """
    Прогрессивный стриминг с защитой от DSML.
    Держит SAFE_MARGIN байт в буфере — если там появится начало DSML,
    останавливаемся. Текст идёт к пользователю сразу, без ожидания [DONE].
    """
    SAFE = 20  # минимальная длина маркера DSML с запасом
    buf = ""
    try:
        async with client.stream("POST", url, headers=headers, json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                raw = line[6:].strip()
                if raw == "[DONE]":
                    break
                try:
                    delta = (
                        json.loads(raw)["choices"][0]
                        .get("delta", {})
                        .get("content", "")
                    )
                    if not delta:
                        continue
                except Exception:
                    continue

                buf += delta

                # Как только DSML появился — обрезаем и выходим
                if "｜｜DSML｜｜" in buf or "<｜｜" in buf:
                    clean = _strip_dsml(buf)
                    if clean:
                        yield ("chunk", clean)
                    return

                # Выдаём всё кроме последних SAFE символов (хвост может быть частью DSML)
                if len(buf) > SAFE:
                    yield ("chunk", buf[:-SAFE])
                    buf = buf[-SAFE:]

    except Exception as e:
        yield ("error", f"Ошибка стриминга: {e}")
        return

    # Flush остатка
    if buf:
        clean = _strip_dsml(buf)
        if clean:
            yield ("chunk", clean)

# ---------------------------------------------------------------------------
# Шаблоны вывода по видам дайджеста
# ---------------------------------------------------------------------------
_OUTPUT_TEMPLATES: dict[str, str] = {
    "daily": """\
Ты составляешь тематический дайджест — как редактор новостного дайджеста для занятого человека.
НЕ пересказывай чаты по одному. ГРУППИРУЙ всё по темам и рассказывай развёрнуто.

Структура ответа (строго соблюдай):

## � Темы дня
[Перечисли 3–6 тем одной строкой каждая, например: � Технологии · 🌍 Политика · 🎮 Игры]

---

[Для КАЖДОЙ темы создай отдельный блок:]

## [эмодзи] [Название темы]
**Что произошло:** [2–4 предложения — суть событий, новости, анонсы]
**Споры и реакция:** [Если были конфликты, дебаты, разные мнения — объясни кто и что отстаивал, чем закончилось]
**Детали:** [Цифры, факты, имена, ссылки которые важно знать]
**Чаты:** [В каких чатах обсуждалось]

---

## ❓ Требует твоего ответа
[Вопросы и просьбы конкретно ко МНЕ. Если ничего нет — напиши «Ничего срочного»]

## 🔗 Ссылки дня
[Только реальные URL из сообщений. Если не было — пропусти раздел]

ВАЖНО: Минимум 3–4 предложения на каждую тему. Не скупись на детали — человек хочет понять что произошло не читая чаты.
""",
    "weekly": """\
Структура ответа (строго соблюдай):

## 🗓 Ключевые события недели
[ТОП-7 событий с датами: **дата** — что случилось]

## 📈 Повторяющиеся темы
[Темы которые всплывали несколько раз за неделю]

## ✅ Что нужно сделать
[Action items из переписки — обещания, задачи, дедлайны]

## 👥 Активные контакты
[ТОП-5 людей с кем больше всего общался]
""",
    "catchup": """\
Структура ответа (строго соблюдай):

## 🚨 Срочное (ответить в первую очередь)
[Вопросы и просьбы ко МНЕ которые ждут ответа]

## 📥 Пропущенное
[Краткое по каждому активному чату — что пропустил]

## 🗑 Можно игнорировать
[Каналы, спам, новости — что НЕ требует действий]
""",
    "important": """\
Структура ответа (строго соблюдай):

## ⭐ ТОП-10 важных событий
[Пронумерованный список: эмодзи + название + 2 предложения контекста + чат + дата]

## ✅ Что нужно сделать на этой неделе
[Список action items из этих событий]
""",
    "actions": """\
Структура ответа (строго соблюдай):

## 📌 Мои обязательства
[Что Я пообещал сделать — с именем собеседника и чатом]

## 📬 Ожидаю от других
[Что другие пообещали МНЕ — с именем и чатом]

## ❓ Вопросы без ответа
[Вопросы ко мне на которые я не ответил]
""",
    "topics": """\
Структура ответа (строго соблюдай):

## 🗂 Темы
[Для каждой темы: **Название** — краткое описание — кто участвовал — статус (активна/закрыта/требует ответа)]
""",
    "timeline": """\
Структура ответа — хронология по дням:

[**ДД.ММ** — краткое описание событий этого дня по чатам]
""",
}

_DEFAULT_TEMPLATE = """\
Отвечай структурированно: используй ## заголовки, ** жирный текст, маркированные списки.
Минимум 200 слов. НИКОГДА не выдумывай сообщений которых нет в данных.
"""

# ---------------------------------------------------------------------------
# Определения инструментов (OpenAI-формат для DeepSeek)
# ---------------------------------------------------------------------------
_TOOLS_OPENAI = [
    {
        "type": "function",
        "function": {
            "name": "get_chats_overview",
            "description": (
                "Получить список активных чатов за период с превью последних сообщений. "
                "ВСЕГДА вызывай ПЕРВЫМ — это карта данных, из неё берёшь chat_id для дальнейших вызовов."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "period_hours": {
                        "type": "integer",
                        "description": "Период в часах (24 = день, 168 = неделя)",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_chat_messages",
            "description": (
                "Читает сообщения из конкретного чата постранично. "
                "chat_id берётся из get_chats_overview. "
                "ОБЯЗАТЕЛЬНО передавай period_hours чтобы не читать всю историю! "
                "Для daily=24, weekly=168, catchup=hours из задачи. "
                "Если в ответе написано «(ещё N сообщений)» — вызови снова с offset увеличенным на limit."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "chat_id": {
                        "type": "integer",
                        "description": "ID чата из get_chats_overview",
                    },
                    "period_hours": {
                        "type": "integer",
                        "description": "За сколько часов читать сообщения. Для daily=24, weekly=168. ВСЕГДА указывай!",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Сколько сообщений (по умолчанию 50, макс 100)",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Пропустить N первых сообщений (пагинация, по умолчанию 0)",
                    },
                    "date_from": {
                        "type": "string",
                        "description": "Точное начало периода ISO 8601 (опционально, переопределяет period_hours)",
                    },
                    "date_to": {
                        "type": "string",
                        "description": "Точный конец периода ISO 8601 (опционально)",
                    },
                },
                "required": ["chat_id", "period_hours"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_chat_stats",
            "description": (
                "Быстрая статистика чата: топ отправителей, соотношение входящих/исходящих. "
                "Вызывай ПЕРЕД read_chat_messages чтобы понять — стоит ли читать полностью."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "chat_id": {"type": "integer", "description": "ID чата"},
                    "period_hours": {
                        "type": "integer",
                        "description": "Период в часах (по умолчанию 168)",
                    },
                },
                "required": ["chat_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_messages",
            "description": (
                "Ищет сообщения по ключевым словам. Используй для поиска обещаний, "
                "договорённостей, упоминаний конкретных тем или людей."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query_str": {
                        "type": "string",
                        "description": "Слово или фраза для поиска",
                    },
                    "period_hours": {
                        "type": "integer",
                        "description": "Период в часах (по умолчанию 48)",
                    },
                    "is_me": {
                        "type": "boolean",
                        "description": "true — только мои, false — только чужие, null — все",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Макс. результатов (по умолчанию 30)",
                    },
                },
                "required": ["query_str"],
            },
        },
    },
]

# Gemini-формат function_declarations (извлекаем из OpenAI-определений)
_TOOLS_GEMINI = [
    {
        "function_declarations": [
            {
                "name": t["function"]["name"],
                "description": t["function"]["description"],
                "parameters": t["function"].get("parameters", {}),
            }
            for t in _TOOLS_OPENAI
        ]
    }
]


# ---------------------------------------------------------------------------
# Выполнение инструмента
# ---------------------------------------------------------------------------
async def _execute_tool(account_id: int, name: str, args: dict) -> str:
    """Вызывает Python-функцию инструмента и возвращает читаемый текст."""
    try:
        fn_map = {
            "get_chats_overview": agent_tools.get_chats_overview,
            "read_chat_messages": agent_tools.read_chat_messages,
            "get_chat_stats": agent_tools.get_chat_stats,
            "search_messages": agent_tools.search_messages,
        }
        fn = fn_map.get(name)
        if fn is None:
            return f"[Ошибка] Инструмент «{name}» не существует."
        result = await fn(account_id, **args)
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    except Exception as e:
        log.exception("Tool %s failed", name)
        return f"[Ошибка инструмента {name}]: {e}"


# ---------------------------------------------------------------------------
# Системный промпт
# ---------------------------------------------------------------------------
def _build_system(kind: str) -> str:
    template = _OUTPUT_TEMPLATES.get(kind, _DEFAULT_TEMPLATE)
    return (
        "Ты — аналитик Telegram-переписок. У тебя есть инструменты для чтения базы данных.\n\n"
        "ПРАВИЛА:\n"
        "1. Сначала ВСЕГДА вызови get_chats_overview с нужным period_hours (24=день, 168=неделя).\n"
        "2. Для каждого важного чата вызови read_chat_messages с тем же period_hours — НЕ читай всю историю!\n"
        "3. Если нужно больше сообщений за тот же период — используй пагинацию (offset += limit).\n"
        "4. НИКОГДА не выдумывай факты — только то, что есть в инструментах.\n"
        "5. «Я» в сообщениях — это сам пользователь (is_outgoing=true).\n"
        "6. Не трать шаги на чаты с 0 сообщений за период — пропускай их.\n\n"
        f"ФОРМАТ ОТВЕТА:\n{template}"
    )


_PREFILL: dict[str, str] = {
    "daily":     "## 📰 Темы дня\n",
    "weekly":    "## 🗓 Ключевые события недели\n",
    "important": "## 🔥 Важное\n",
    "actions":   "## ✅ Задачи и договорённости\n",
    "catchup":   "## ⏪ Что пропустил\n",
    "topics":    "## 🗂 Темы\n",
    "timeline":  "## 📅 Хронология\n",
}


def _trim_tool_results(messages: list[dict], max_chars: int = 400) -> list[dict]:
    """Обрезает tool-результаты до max_chars чтобы не перегружать финальный контекст."""
    out = []
    for m in messages:
        if m.get("role") == "tool":
            content = m.get("content", "")
            if len(content) > max_chars:
                m = {**m, "content": content[:max_chars] + "\n…[обрезано]"}
        out.append(m)
    return out


# ---------------------------------------------------------------------------
# DeepSeek / OpenAI-совместимый цикл (с реальным стримингом финала)
# ---------------------------------------------------------------------------
async def _deepseek_loop(
    account_id: int,
    prompt: str,
    kind: str,
    key: str,
    mdl: str | None,
    max_steps: int,
) -> AsyncGenerator[tuple[str, str], None]:
    messages: list[dict] = [
        {"role": "system", "content": _build_system(kind)},
        {"role": "user", "content": prompt},
    ]
    model = mdl or settings.deepseek_model

    async with httpx.AsyncClient(timeout=180) as client:
        for step in range(max_steps):
            log.info("DeepSeek agent step %d/%d", step + 1, max_steps)

            # На последних 2 шагах принудительно переходим к финальному ответу
            force_final = step >= max_steps - 2
            if force_final:
                yield ("status", "Формирую отчёт…")
                messages.append({
                    "role": "user",
                    "content": "Данные собраны. Напиши финальный структурированный отчёт строго по шаблону из системного промпта.",
                })
                # Обрезаем tool-результаты чтобы не перегрузить контекст
                trimmed = _trim_tool_results(messages)
                # Prefill: форсируем модель начать сразу с отчёта, без внутренних рассуждений
                prefill_hdr = _PREFILL.get(kind, "## 📋 Отчёт\n")
                trimmed = trimmed + [{"role": "assistant", "content": prefill_hdr}]
                yield ("chunk", prefill_hdr)
                async for ev in _stream_clean(
                    client,
                    "https://api.deepseek.com/chat/completions",
                    {"Authorization": f"Bearer {key}"},
                    {"model": model, "messages": trimmed, "temperature": 0.3, "stream": True},
                ):
                    yield ev
                return

            # Обычный шаг с инструментами
            try:
                r = await client.post(
                    "https://api.deepseek.com/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json={
                        "model": model,
                        "messages": messages,
                        "tools": _TOOLS_OPENAI,
                        "tool_choice": "auto",
                        "temperature": 0.2,
                    },
                )
                r.raise_for_status()
                data = r.json()
            except Exception as e:
                yield ("error", f"Ошибка LLM на шаге {step + 1}: {e}")
                return

            response_msg = data["choices"][0]["message"]
            tool_calls = response_msg.get("tool_calls")
            messages.append(response_msg)

            if tool_calls:
                names = [tc["function"]["name"] for tc in tool_calls]
                yield ("status", f"Читаю данные: {', '.join(names)}…")

                for tc in tool_calls:
                    fname = tc["function"]["name"]
                    try:
                        args = json.loads(tc["function"]["arguments"])
                    except json.JSONDecodeError:
                        args = {}
                    log.info("Agent tool call: %s(%s)", fname, args)
                    tool_result = await _execute_tool(account_id, fname, args)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "name": fname,
                        "content": tool_result,
                    })
            else:
                yield ("status", "Формирую отчёт…")

                # Если модель уже вернула готовый текст — используем его напрямую
                existing = _strip_dsml(response_msg.get("content") or "")
                if existing:
                    STEP = 150
                    for i in range(0, len(existing), STEP):
                        yield ("chunk", existing[i:i + STEP])
                    return

                # Иначе — запрос без tools + явная инструкция «пиши отчёт»
                final_messages = [
                    m for m in messages
                    if not (m.get("role") == "assistant" and _strip_dsml(m.get("content") or "") == "")
                ]
                final_messages.append({
                    "role": "user",
                    "content": "Данные собраны. Напиши финальный структурированный отчёт строго по шаблону из системного промпта.",
                })
                # Prefill: форсируем начало отчёта без thinking-текста
                prefill_hdr = _PREFILL.get(kind, "## 📋 Отчёт\n")
                prefilled = _trim_tool_results(final_messages) + [
                    {"role": "assistant", "content": prefill_hdr}
                ]
                yield ("chunk", prefill_hdr)
                async for ev in _stream_clean(
                    client,
                    "https://api.deepseek.com/chat/completions",
                    {"Authorization": f"Bearer {key}"},
                    {"model": model, "messages": prefilled, "temperature": 0.3, "stream": True},
                ):
                    yield ev
                return

        yield ("error", "Агент превысил лимит шагов. Попробуй ещё раз.")


# ---------------------------------------------------------------------------
# Gemini цикл (нативный function calling)
# ---------------------------------------------------------------------------
async def _gemini_loop(
    account_id: int,
    prompt: str,
    kind: str,
    key: str,
    mdl: str | None,
    max_steps: int,
) -> AsyncGenerator[tuple[str, str], None]:
    model = mdl or settings.gemini_model
    base_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}"

    # История в Gemini-формате
    contents: list[dict] = [{"role": "user", "parts": [{"text": prompt}]}]
    system_instruction = {"parts": [{"text": _build_system(kind)}]}

    async with httpx.AsyncClient(timeout=180) as client:
        for step in range(max_steps):
            log.info("Gemini agent step %d/%d", step + 1, max_steps)

            try:
                r = await client.post(
                    f"{base_url}:generateContent?key={key}",
                    json={
                        "system_instruction": system_instruction,
                        "contents": contents,
                        "tools": _TOOLS_GEMINI,
                        "tool_config": {"function_calling_config": {"mode": "AUTO"}},
                        "generationConfig": {"temperature": 0.2},
                    },
                )
                r.raise_for_status()
                data = r.json()
            except Exception as e:
                yield ("error", f"Ошибка Gemini на шаге {step + 1}: {e}")
                return

            parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
            model_content = {"role": "model", "parts": parts}
            contents.append(model_content)

            # Ищем function calls среди частей ответа
            func_calls = [p["functionCall"] for p in parts if "functionCall" in p]

            if func_calls:
                names = [fc["name"] for fc in func_calls]
                yield ("status", f"Читаю данные: {', '.join(names)}…")

                tool_responses = []
                for fc in func_calls:
                    fname = fc["name"]
                    args = fc.get("args", {})
                    log.info("Gemini tool call: %s(%s)", fname, args)
                    result = await _execute_tool(account_id, fname, args)
                    tool_responses.append({
                        "functionResponse": {
                            "name": fname,
                            "response": {"content": result},
                        }
                    })
                contents.append({"role": "user", "parts": tool_responses})
            else:
                # Финальный текст — стримим через streamGenerateContent
                yield ("status", "Формирую отчёт…")
                try:
                    async with client.stream(
                        "POST",
                        f"{base_url}:streamGenerateContent?key={key}&alt=sse",
                        json={
                            "system_instruction": system_instruction,
                            "contents": contents,
                            "generationConfig": {"temperature": 0.3},
                        },
                    ) as resp:
                        resp.raise_for_status()
                        async for line in resp.aiter_lines():
                            if not line.startswith("data: "):
                                continue
                            try:
                                chunk_data = json.loads(line[6:])
                                text = (
                                    chunk_data.get("candidates", [{}])[0]
                                    .get("content", {})
                                    .get("parts", [{}])[0]
                                    .get("text", "")
                                )
                                if text:
                                    yield ("chunk", text)
                            except Exception:
                                pass
                except Exception as e:
                    # Стриминг не удался — отдаём накопленный текст
                    fallback = "".join(
                        p.get("text", "") for p in parts if "text" in p
                    )
                    if fallback:
                        yield ("chunk", fallback)
                    else:
                        yield ("error", f"Ошибка стриминга Gemini: {e}")
                return

        yield ("error", "Агент превысил лимит шагов. Попробуй ещё раз.")


# ---------------------------------------------------------------------------
# Direct-stream: prefetch data → ONE streaming LLM call (no round-trips)
# ---------------------------------------------------------------------------
_KIND_HOURS: dict[str, int] = {
    "daily": 24, "topics": 24, "actions": 24,
    "weekly": 168, "timeline": 168, "important": 168,
    "catchup": 12,
}

_DIRECT_SYSTEM = (
    "Ты — аналитик Telegram-переписок пользователя. "
    "Тебе уже предоставлены все данные из чатов.\n"
    "ВАЖНО: 'Я' в сообщениях = сам пользователь (исходящие). "
    "Остальные имена — собеседники.\n"
    "Не выдумывай факты — только то, что есть в данных. "
    "Используй markdown: ## заголовки, **жирный**, - списки."
)


async def _prefetch_context(account_id: int, kind: str) -> str:
    """Загружает данные напрямую из DB — без LLM round-trips."""
    period_hours = _KIND_HOURS.get(kind, 24)

    overview = await agent_tools.get_chats_overview(account_id, period_hours)

    # Извлекаем chat_id из строк вида [chat_id=N]
    chat_ids = re.findall(r'\[chat_id=(\d+)\]', overview)

    parts = [f"=== Обзор активных чатов ===\n{overview}"]

    # Читаем топ-6 чатов
    for cid_str in chat_ids[:6]:
        try:
            msgs = await agent_tools.read_chat_messages(
                account_id, int(cid_str), limit=50, period_hours=period_hours
            )
            parts.append(msgs)
        except Exception:  # noqa: BLE001
            pass

    return "\n\n".join(parts)


async def _direct_stream(
    account_id: int,
    prompt: str,
    kind: str,
    key: str,
    mdl: str | None,
    provider: str,
) -> AsyncGenerator[tuple[str, str], None]:
    """Один стриминговый вызов без agentic loop."""
    from ..llm import _deepseek_stream_messages, _gemini_stream_messages

    yield ("status", "Читаю переписку…")
    try:
        context = await _prefetch_context(account_id, kind)
    except Exception as e:  # noqa: BLE001
        yield ("error", f"Ошибка загрузки данных: {e}")
        return

    if not context or "не найдено" in context:
        yield ("error", "Нет сообщений за указанный период.")
        return

    prefill = _PREFILL.get(kind, "## 📋 Отчёт\n")
    system = _DIRECT_SYSTEM + f"\n\n{context}"
    messages = [
        {"role": "user", "content": prompt},
        {"role": "assistant", "content": prefill},
    ]

    yield ("status", "Формирую отчёт…")
    yield ("chunk", prefill)

    try:
        if provider == "gemini":
            async for chunk in _gemini_stream_messages(messages, system, model=mdl, api_key=key):
                if chunk:
                    yield ("chunk", chunk)
        else:
            async for chunk in _deepseek_stream_messages(messages, system, model=mdl, api_key=key):
                if chunk:
                    yield ("chunk", chunk)
    except Exception as e:  # noqa: BLE001
        yield ("error", f"Ошибка LLM: {e}")


# ---------------------------------------------------------------------------
# Публичный entrypoint
# ---------------------------------------------------------------------------
async def run_agentic_loop(
    account_id: int,
    prompt: str,
    kind: str = "daily",
    max_steps: int = 4,
) -> AsyncGenerator[tuple[str, str], None]:
    """
    Основной цикл. Yields ('status'|'chunk'|'error', text).
    Для стандартных дайджестов использует direct-stream (без tool-call round-trips).
    """
    provider, key, mdl = await _resolve_creds(account_id, None, None)

    if provider == "gemini":
        resolved_key = key or settings.gemini_api_key
        if not resolved_key:
            yield ("error", "GEMINI_API_KEY не задан.")
            return
    else:
        resolved_key = key or settings.deepseek_api_key
        if not resolved_key:
            yield ("error", "DEEPSEEK_API_KEY не задан.")
            return

    # Для стандартных дайджестов — прямой стриминг без agent loop
    if kind in _KIND_HOURS:
        async for event in _direct_stream(account_id, prompt, kind, resolved_key, mdl, provider):
            yield event
        return

    # Для прочего — полный agentic loop
    if provider == "gemini":
        yield ("status", "Агент начинает анализ (Gemini)…")
        async for event in _gemini_loop(account_id, prompt, kind, resolved_key, mdl, max_steps):
            yield event
    else:
        yield ("status", "Агент начинает анализ (DeepSeek)…")
        async for event in _deepseek_loop(account_id, prompt, kind, resolved_key, mdl, max_steps):
            yield event
