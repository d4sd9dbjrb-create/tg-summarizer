"""LLM-провайдеры: DeepSeek (OpenAI-совместимый) и Gemini."""
from __future__ import annotations

import json
import logging
from typing import AsyncGenerator

import httpx

from .config import get_settings

log = logging.getLogger(__name__)
settings = get_settings()


async def _resolve_creds(
    account_id: int | None,
    model: str | None,
    api_key: str | None,
) -> tuple[str, str | None, str | None]:
    """
    Возвращает (provider, api_key, model) с учётом per-account настроек.

    Логика:
    - Если api_key/model явно переданы вызывающим — приоритет у них.
    - Если account_id указан — загружаем настройки аккаунта и применяем:
        * провайдер из account.llm_provider
        * ключ из user_*_api_key_enc (если есть)
        * модель из user_*_model (если задана)
    - Иначе — серверный дефолт из settings.
    """
    provider = settings.llm_provider.lower()
    resolved_key = api_key
    resolved_mdl = model

    if account_id is not None:
        try:
            from sqlalchemy import select
            from .crypto import decrypt
            from .db import SessionLocal
            from .models import Account

            async with SessionLocal() as s:
                acc = (
                    await s.execute(select(Account).where(Account.id == account_id))
                ).scalar_one_or_none()

            if acc:
                provider = (acc.llm_provider or provider).lower()
                if provider == "gemini":
                    if resolved_key is None and acc.user_gemini_api_key_enc:
                        try:
                            resolved_key = decrypt(acc.user_gemini_api_key_enc)
                        except Exception:
                            log.warning("Failed to decrypt user gemini key for acc=%s", account_id)
                    if resolved_mdl is None and acc.user_gemini_model:
                        resolved_mdl = acc.user_gemini_model
                else:
                    if resolved_key is None and acc.user_deepseek_api_key_enc:
                        try:
                            resolved_key = decrypt(acc.user_deepseek_api_key_enc)
                        except Exception:
                            log.warning("Failed to decrypt user deepseek key for acc=%s", account_id)
                    if resolved_mdl is None and acc.user_deepseek_model:
                        resolved_mdl = acc.user_deepseek_model
        except Exception:
            log.exception("resolve_creds failed for acc=%s", account_id)

    return provider, resolved_key, resolved_mdl


SYSTEM_PROMPT = (
    "Ты — персональный аналитик Telegram-переписок. "
    "Пиши ПОДРОБНО и РАЗВЁРНУТО — пользователь хочет знать всё, а не поверхностную сводку. "
    "Для каждого факта давай контекст: кто сказал, когда, в каком чате, что именно имелось в виду. "
    "Используй markdown: ## для заголовков, ** для жирного, - для списков. "
    "Не выдумывай факты — только то, что есть в тексте. "
    "Если видишь вопрос или просьбу адресованную пользователю — обязательно выдели отдельно с полным контекстом. "
    "Минимальный объём ответа — 300 слов, если материал позволяет."
)


async def _deepseek_complete(
    prompt: str,
    system: str = SYSTEM_PROMPT,
    model: str | None = None,
    api_key: str | None = None,
) -> str:
    key = api_key or settings.deepseek_api_key
    if not key:
        return "[LLM] DEEPSEEK_API_KEY не задан."
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model or settings.deepseek_model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.3,
            },
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()


async def _gemini_complete(
    prompt: str,
    system: str = SYSTEM_PROMPT,
    model: str | None = None,
    api_key: str | None = None,
) -> str:
    key = api_key or settings.gemini_api_key
    mdl = model or settings.gemini_model
    if not key:
        return "[LLM] GEMINI_API_KEY не задан."
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{mdl}:generateContent?key={key}"
    )
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            url,
            json={
                "system_instruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.3},
            },
        )
        r.raise_for_status()
        data = r.json()
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except (KeyError, IndexError):
            log.error("Gemini bad response: %s", data)
            return "[LLM] пустой ответ от Gemini."


async def _deepseek_stream(
    prompt: str,
    system: str = SYSTEM_PROMPT,
    model: str | None = None,
    api_key: str | None = None,
) -> AsyncGenerator[str, None]:
    key = api_key or settings.deepseek_api_key
    if not key:
        yield "[LLM] DEEPSEEK_API_KEY не задан."
        return
    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream(
            "POST",
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model or settings.deepseek_model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user",   "content": prompt},
                ],
                "temperature": 0.3,
                "stream": True,
            },
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                raw = line[6:].strip()
                if raw == "[DONE]":
                    break
                try:
                    delta = json.loads(raw)["choices"][0].get("delta", {}).get("content", "")
                    if delta:
                        yield delta
                except Exception:  # noqa: BLE001
                    pass


async def _gemini_stream(
    prompt: str,
    system: str = SYSTEM_PROMPT,
    model: str | None = None,
    api_key: str | None = None,
) -> AsyncGenerator[str, None]:
    key = api_key or settings.gemini_api_key
    mdl = model or settings.gemini_model
    if not key:
        yield "[LLM] GEMINI_API_KEY не задан."
        return
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{mdl}:streamGenerateContent"
        f"?key={key}&alt=sse"
    )
    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream(
            "POST",
            url,
            json={
                "system_instruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.3},
            },
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                try:
                    data = json.loads(line[6:])
                    text = data["candidates"][0]["content"]["parts"][0]["text"]
                    if text:
                        yield text
                except Exception:  # noqa: BLE001
                    pass


async def _deepseek_stream_messages(
    messages: list[dict],
    system: str,
    model: str | None = None,
    api_key: str | None = None,
) -> AsyncGenerator[str, None]:
    """Multi-turn стриминг DeepSeek: messages = [{role, content}, ...]"""
    key = api_key or settings.deepseek_api_key
    if not key:
        yield "[LLM] DEEPSEEK_API_KEY не задан."
        return
    full = [{"role": "system", "content": system}] + messages
    async with httpx.AsyncClient(timeout=180) as client:
        async with client.stream(
            "POST",
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": model or settings.deepseek_model, "messages": full,
                  "temperature": 0.4, "stream": True},
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                raw = line[6:].strip()
                if raw == "[DONE]":
                    break
                try:
                    delta = json.loads(raw)["choices"][0].get("delta", {}).get("content", "")
                    if delta:
                        yield delta
                except Exception:
                    pass


async def _gemini_stream_messages(
    messages: list[dict],
    system: str,
    model: str | None = None,
    api_key: str | None = None,
) -> AsyncGenerator[str, None]:
    """Multi-turn стриминг Gemini: messages = [{role, content}, ...]"""
    key = api_key or settings.gemini_api_key
    mdl = model or settings.gemini_model
    if not key:
        yield "[LLM] GEMINI_API_KEY не задан."
        return
    # Gemini: assistant → model
    contents = []
    for m in messages:
        role = "model" if m["role"] == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": m["content"]}]})
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{mdl}:streamGenerateContent?key={key}&alt=sse")
    async with httpx.AsyncClient(timeout=180) as client:
        async with client.stream(
            "POST", url,
            json={"system_instruction": {"parts": [{"text": system}]},
                  "contents": contents,
                  "generationConfig": {"temperature": 0.4}},
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                try:
                    text = json.loads(line[6:])["candidates"][0]["content"]["parts"][0]["text"]
                    if text:
                        yield text
                except Exception:
                    pass


async def complete_stream_messages(
    messages: list[dict],
    system: str,
    model: str | None = None,
    api_key: str | None = None,
    account_id: int | None = None,
) -> AsyncGenerator[str, None]:
    """Multi-turn стриминг — принимает messages array вместо плоского промпта."""
    provider, key, mdl = await _resolve_creds(account_id, model, api_key)
    try:
        if provider == "gemini":
            async for chunk in _gemini_stream_messages(messages, system, model=mdl, api_key=key):
                yield chunk
        else:
            async for chunk in _deepseek_stream_messages(messages, system, model=mdl, api_key=key):
                yield chunk
    except Exception as e:
        yield f"[Ошибка LLM: {e}]"


async def complete_stream(
    prompt: str,
    system: str = SYSTEM_PROMPT,
    model: str | None = None,
    api_key: str | None = None,
    account_id: int | None = None,
) -> AsyncGenerator[str, None]:
    """Стриминговый entrypoint — возвращает AsyncGenerator чанков текста.

    model=None — берётся из конфига (по умолчанию deepseek-v4-pro).
    Можно явно передать settings.deepseek_model_fast для быстрых мап-вызовов.
    Для Gemini параметр model игнорируется.
    """
    # Если передан account_id — резолвим персональные ключи/модель
    provider, key, mdl = await _resolve_creds(account_id, model, api_key)
    try:
        if provider == "gemini":
            async for chunk in _gemini_stream(prompt, system, model=mdl, api_key=key):
                yield chunk
        else:
            async for chunk in _deepseek_stream(prompt, system, model=mdl, api_key=key):
                yield chunk
    except Exception as e:  # noqa: BLE001
        yield f"[Ошибка LLM: {e}]"


async def complete(
    prompt: str,
    system: str = SYSTEM_PROMPT,
    model: str | None = None,
    api_key: str | None = None,
    account_id: int | None = None,
) -> str:
    """Универсальный entrypoint к выбранному провайдеру."""
    provider, key, mdl = await _resolve_creds(account_id, model, api_key)
    try:
        if provider == "gemini":
            return await _gemini_complete(prompt, system, model=mdl, api_key=key)
        return await _deepseek_complete(prompt, system, model=mdl, api_key=key)
    except Exception as e:  # noqa: BLE001
        log.exception("LLM error")
        return f"[LLM error] {e}"
