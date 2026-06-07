"""JSON API для Telegram Mini App."""
from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import datetime, timezone

import json as _json

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from telethon import TelegramClient
from telethon.errors import (
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

from ... import account_manager
from ...config import get_settings
from ...crypto import encrypt
from ...db import SessionLocal
from ...features import backfill as backfill_mod
from ...models import Account
from ..utils import verify_init_data

log = logging.getLogger(__name__)
settings = get_settings()
router = APIRouter(prefix="/api")

# Pending clients keyed by session_id (из cookie)
_pending: dict[str, dict] = {}


def _ok(**data):
    return JSONResponse({"ok": True, **data})


def _err(msg: str, status: int = 400):
    return JSONResponse({"ok": False, "error": msg}, status_code=status)


def _sid(request: Request) -> str:
    sid = request.session.get("sid")
    if not sid:
        sid = secrets.token_urlsafe(24)
        request.session["sid"] = sid
    return sid


def _state(request: Request) -> dict:
    sid = _sid(request)
    if sid not in _pending:
        _pending[sid] = {
            "tg_user_id": None,
            "invite_ok": False,
            "tos_ok": False,
            "client": None,
            "phone": None,
            "phone_code_hash": None,
            "qr_login": None,
            "qr_task": None,
            "qr_done": False,
        }
    return _pending[sid]


def _make_client() -> TelegramClient:
    return TelegramClient(
        StringSession(), settings.tg_api_id, settings.tg_api_hash
    )


# ---------------------------------------------------------------------------
#  Auth check
# ---------------------------------------------------------------------------

@router.post("/auth")
async def auth(request: Request, body: dict = Body(...)):
    """Верифицирует initData и возвращает статус аккаунта."""
    init_data = body.get("initData", "")

    # В dev-режиме (пустой initData) — не ломаем
    if init_data:
        data = verify_init_data(init_data, settings.control_bot_token)
        if not data:
            return _err("Invalid initData", 403)
        user = data.get("user", {})
        tg_user_id = user.get("id")
    else:
        tg_user_id = None

    if tg_user_id:
        st = _state(request)
        st["tg_user_id"] = tg_user_id

    async with SessionLocal() as s:
        acc = None
        if tg_user_id:
            acc = (
                await s.execute(select(Account).where(Account.tg_user_id == tg_user_id))
            ).scalar_one_or_none()

    if acc and acc.status == "active" and acc.bot_linked_at:
        return _ok(status="registered", name=acc.tg_first_name or "")
    if acc and acc.status == "active":
        return _ok(status="pending_link", name=acc.tg_first_name or "")

    return _ok(status="unregistered")


# ---------------------------------------------------------------------------
#  Registration steps
# ---------------------------------------------------------------------------

@router.post("/register/invite")
async def reg_invite(request: Request, body: dict = Body(...)):
    if body.get("code", "").strip().lower() != settings.invite_code.strip().lower():
        return _err("Неверный код доступа")
    _state(request)["invite_ok"] = True
    return _ok()


@router.post("/register/tos")
async def reg_tos(request: Request):
    st = _state(request)
    if not st["invite_ok"]:
        return _err("Сначала введи invite-код")
    st["tos_ok"] = True
    return _ok()


# ---------- Phone ----------

@router.post("/register/phone/send")
async def reg_phone_send(request: Request, body: dict = Body(...)):
    st = _state(request)
    if not st["tos_ok"]:
        return _err("Не принято соглашение")

    phone = body.get("phone", "").strip().replace(" ", "")
    client = _make_client()
    try:
        await client.connect()
        sent = await client.send_code_request(phone)
    except PhoneNumberInvalidError:
        await client.disconnect()
        return _err("Неверный номер телефона")
    except Exception as e:  # noqa: BLE001
        await client.disconnect()
        return _err(f"Ошибка Telegram: {e}")

    st["client"] = client
    st["phone"] = phone
    st["phone_code_hash"] = sent.phone_code_hash
    return _ok(phone=phone)


@router.post("/register/phone/verify")
async def reg_phone_verify(request: Request, body: dict = Body(...)):
    st = _state(request)
    client: TelegramClient | None = st.get("client")
    if not client:
        return _err("Сессия устарела, начни заново")

    try:
        await client.sign_in(
            phone=st["phone"],
            code=body.get("code", "").strip(),
            phone_code_hash=st["phone_code_hash"],
        )
    except SessionPasswordNeededError:
        return _ok(needs_2fa=True)
    except PhoneCodeInvalidError:
        return _err("Неверный код")
    except PhoneCodeExpiredError:
        return _err("Код истёк, начни заново")
    except Exception as e:  # noqa: BLE001
        return _err(f"Ошибка: {e}")

    return await _finalize(request, client)


@router.post("/register/2fa")
async def reg_2fa(request: Request, body: dict = Body(...)):
    st = _state(request)
    client: TelegramClient | None = st.get("client")
    if not client:
        return _err("Сессия устарела")
    try:
        await client.sign_in(password=body.get("password", ""))
    except Exception as e:  # noqa: BLE001
        return _err(f"Неверный пароль: {e}")
    return await _finalize(request, client)


# ---------- QR ----------

@router.post("/register/qr/start")
async def reg_qr_start(request: Request):
    import base64, io
    import qrcode

    st = _state(request)
    if not st["tos_ok"]:
        return _err("Не принято соглашение")

    # Always create a fresh client — reusing a broken one causes MTProto security errors
    old_client = st.get("client")
    if old_client:
        try:
            await old_client.disconnect()
        except Exception:  # noqa: BLE001
            pass
    client = _make_client()
    await client.connect()
    st["client"] = client
    st["qr_done"] = False
    st["qr_task"] = None

    qr = await client.qr_login()
    st["qr_login"] = qr

    async def _wait():
        try:
            while True:
                try:
                    res = await qr.wait(timeout=30)
                    if res:
                        st["qr_done"] = True
                        return
                except asyncio.TimeoutError:
                    try:
                        await qr.recreate()
                        st["qr_url"] = qr.url
                    except Exception:  # noqa: BLE001
                        return
                except SessionPasswordNeededError:
                    st["qr_done"] = "2fa"
                    return
        except Exception as e:  # noqa: BLE001
            log.warning("QR wait task crashed: %s", e)
            st["qr_done"] = "error"

    if not st.get("qr_task") or st["qr_task"].done():
        st["qr_task"] = asyncio.create_task(_wait())

    st["qr_url"] = qr.url
    buf = io.BytesIO()
    qrcode.make(qr.url).save(buf, format="PNG")
    qr_b64 = base64.b64encode(buf.getvalue()).decode()
    return _ok(qr_b64=qr_b64, url=qr.url)


@router.get("/register/qr/status")
async def reg_qr_status(request: Request):
    st = _state(request)
    done = st.get("qr_done")
    if done == "2fa":
        return _ok(status="2fa")
    if done is True:
        client = st.get("client")
        if client:
            result = await _finalize(request, client)
            return result
    return _ok(status="wait")


# ---------------------------------------------------------------------------
#  Finalize: persist account, mark bot_linked (tg_user_id уже известен из initData)
# ---------------------------------------------------------------------------

async def _finalize(request: Request, client: TelegramClient):
    if not await client.is_user_authorized():
        return _err("Авторизация не завершена")

    encrypted = encrypt(client.session.save())
    acc = await account_manager.persist_account(client, encrypted)

    # Если tg_user_id был передан через initData — сразу помечаем bot_linked
    st = _state(request)
    tg_user_id_from_init = st.get("tg_user_id")

    async with SessionLocal() as s:
        a = await s.get(Account, acc.id)
        # initData tg_user_id должен совпасть с tg_user_id аккаунта
        if tg_user_id_from_init and int(tg_user_id_from_init) == a.tg_user_id:
            a.bot_linked_at = datetime.now(timezone.utc)
        a.accepted_tos_at = datetime.now(timezone.utc)
        await s.commit()

    sid = request.session.get("sid")
    _pending.pop(sid, None)
    request.session.clear()

    return _ok(status="done", name=acc.tg_first_name or "")


# ---------------------------------------------------------------------------
#  Dashboard stats (быстрый обзор для Mini App)
# ---------------------------------------------------------------------------

@router.post("/dashboard")
async def dashboard(request: Request, body: dict = Body(...)):
    """Возвращает key-метрики для главного экрана Mini App."""
    from ...features import stats as st_module

    init_data = body.get("initData", "")
    tg_user_id = None
    if init_data:
        data = verify_init_data(init_data, settings.control_bot_token)
        if data:
            tg_user_id = data.get("user", {}).get("id")

    if not tg_user_id:
        return _err("Unauthorized", 403)

    async with SessionLocal() as s:
        acc = (
            await s.execute(select(Account).where(Account.tg_user_id == tg_user_id))
        ).scalar_one_or_none()

    if not acc or acc.status != "active":
        return _err("Account not found", 404)

    totals = await st_module.total_counts(acc.id)
    chats_7 = await st_module.top_chats(acc.id, 7, 5)
    contacts_7 = await st_module.top_contacts(acc.id, 7, 5)
    output = await st_module.my_output(acc.id, 7)
    hourly = await st_module.hourly_activity(acc.id, 7)
    peak = hourly.index(max(hourly)) if any(hourly) else 0

    return _ok(
        totals=totals,
        top_chats=chats_7,
        top_contacts=contacts_7,
        output=output,
        peak_hour=peak,
        name=acc.tg_first_name or "",
    )


# ---------------------------------------------------------------------------
#  Backfill
# ---------------------------------------------------------------------------

async def _resolve_account(body: dict) -> Account | None:
    init_data = body.get("initData", "")
    tg_user_id = None
    if init_data:
        data = verify_init_data(init_data, settings.control_bot_token)
        if data:
            tg_user_id = data.get("user", {}).get("id")
    if not tg_user_id:
        return None
    async with SessionLocal() as s:
        return (
            await s.execute(select(Account).where(Account.tg_user_id == tg_user_id))
        ).scalar_one_or_none()


@router.post("/scan/settings/get")
async def scan_settings_get(body: dict = Body(...)):
    acc = await _resolve_account(body)
    if not acc:
        return _err("Unauthorized", 403)
    return _ok(
        scan_limit_per_chat=acc.scan_limit_per_chat,
        scan_max_dialogs=acc.scan_max_dialogs,
        scan_include_dms=acc.scan_include_dms,
        scan_include_groups=acc.scan_include_groups,
        scan_include_channels=acc.scan_include_channels,
        scan_skip_bots=acc.scan_skip_bots,
        scan_skip_archived=acc.scan_skip_archived,
    )


@router.post("/scan/settings/save")
async def scan_settings_save(body: dict = Body(...)):
    acc = await _resolve_account(body)
    if not acc:
        return _err("Unauthorized", 403)

    _int_fields = ("scan_limit_per_chat", "scan_max_dialogs")
    _bool_fields = (
        "scan_include_dms", "scan_include_groups", "scan_include_channels",
        "scan_skip_bots", "scan_skip_archived",
    )
    _limits = {
        "scan_limit_per_chat": (50, 5000),
        "scan_max_dialogs": (10, 1000),
    }
    async with SessionLocal() as s:
        a = await s.get(Account, acc.id)
        for f in _int_fields:
            if f in body:
                val = int(body[f])
                lo, hi = _limits.get(f, (1, 9999))
                setattr(a, f, max(lo, min(hi, val)))
        for f in _bool_fields:
            if f in body:
                setattr(a, f, bool(body[f]))
        await s.commit()
    return _ok()


@router.post("/stats")
async def get_stats(body: dict = Body(...)):
    """Статистика по разделу и периоду."""
    acc = await _resolve_account(body)
    if not acc:
        return _err("Unauthorized", 403)

    section = body.get("section", "overview")
    days    = max(1, min(365, int(body.get("days", 7))))

    from ...features import stats as sm
    try:
        if section == "overview":
            t  = await sm.total_counts(acc.id)
            mo = await sm.my_output(acc.id, days)
            pm = await sm.my_output(acc.id, days * 2)
            data = {
                "totals": t,
                "output": mo,
                "prev_sent":  max(pm["sent"]     - mo["sent"],     0),
                "prev_recv":  max(pm["received"]  - mo["received"], 0),
            }
        elif section == "contacts":
            rows = await sm.top_contacts(acc.id, days, 20)
            data = {"rows": [{"name": n, "count": c} for n, c in rows]}
        elif section == "chats":
            rows = await sm.top_chats(acc.id, days, 20)
            data = {"rows": [{"name": n, "count": c} for n, c in rows]}
        elif section == "words":
            words = await sm.top_words(acc.id, days, 25)
            hashtags = await sm.top_hashtags(acc.id, days, 10)
            data = {
                "words":    [{"word": w, "count": c} for w, c in words],
                "hashtags": [{"tag":  h, "count": c} for h, c in hashtags],
            }
        elif section == "time":
            hourly  = await sm.hourly_activity(acc.id, days)
            weekday = await sm.weekday_activity(acc.id, days)
            peak    = hourly.index(max(hourly)) if any(hourly) else 0
            data = {"hourly": hourly, "weekday": weekday, "peak_hour": peak}
        elif section == "media":
            media  = await sm.media_breakdown(acc.id, days)
            emojis = await sm.top_emojis(acc.id, days, 15)
            links  = await sm.top_links(acc.id, days, 10)
            data = {
                "media":  [{"type": t, "count": c} for t, c in media],
                "emojis": [{"emoji": e, "count": c} for e, c in emojis],
                "links":  [{"url": u, "count": c} for u, c in links],
            }
        elif section == "me":
            mo    = await sm.my_output(acc.id, days)
            words = await sm.top_words(acc.id, days, 15)
            data  = {"output": mo, "words": [{"word": w, "count": c} for w, c in words]}
        elif section == "silent":
            rows = await sm.silent_chats(acc.id, 14, 30)
            data = {"chats": [{"title": t, "last_date": d.isoformat() if d else None} for t, d in rows]}
        else:
            return _err("Unknown section")
    except Exception as e:  # noqa: BLE001
        return _err(f"Ошибка: {e}")

    return _ok(section=section, days=days, data=data)


@router.post("/search")
async def search_messages(body: dict = Body(...)):
    """Полнотекстовый поиск по сообщениям."""
    acc = await _resolve_account(body)
    if not acc:
        return _err("Unauthorized", 403)

    q = (body.get("q") or "").strip()
    if len(q) < 2:
        return _err("Запрос слишком короткий")

    from ...features import stats as sm
    rows = await sm.search(acc.id, q)
    results = [
        {"chat": c, "sender": s, "date": d.strftime("%d.%m %H:%M"), "text": t}
        for c, s, d, t in rows[:30]
    ]
    return _ok(results=results, total=len(results))


@router.post("/account/settings")
async def get_account_settings(body: dict = Body(...)):
    """Возвращает настройки аккаунта."""
    acc = await _resolve_account(body)
    if not acc:
        return _err("Unauthorized", 403)
    def _mask(enc: str | None) -> str:
        # Не возвращаем реальный ключ на фронт. Отдаём признак «есть/нет».
        return "•••• set" if enc else ""

    return _ok(
        llm_provider              = acc.llm_provider,
        daily_digest_hour         = acc.daily_digest_hour,
        auto_tag_enabled          = acc.auto_tag_enabled,
        sentiment_enabled         = acc.sentiment_enabled,
        ingest_mode               = acc.ingest_mode,
        # Персональные LLM-настройки
        user_deepseek_key_set     = bool(acc.user_deepseek_api_key_enc),
        user_deepseek_key_masked  = _mask(acc.user_deepseek_api_key_enc),
        user_deepseek_model       = acc.user_deepseek_model or "",
        user_gemini_key_set       = bool(acc.user_gemini_api_key_enc),
        user_gemini_key_masked    = _mask(acc.user_gemini_api_key_enc),
        user_gemini_model         = acc.user_gemini_model or "",
    )


@router.post("/account/reset")
async def account_reset(request: Request, body: dict = Body(...)):
    """Удаляет аккаунт и сессию — позволяет пройти регистрацию заново."""
    from sqlalchemy import delete as sa_delete
    from ...models import Message, Chat, Summary

    acc = await _resolve_account(body)
    if not acc:
        return _err("Unauthorized", 403)

    # Останавливаем userbot-клиент
    try:
        from ... import account_manager as am
        await am.stop_client(acc.id)
    except Exception:
        pass

    async with SessionLocal() as s:
        await s.execute(sa_delete(Summary).where(Summary.account_id == acc.id))
        await s.execute(sa_delete(Message).where(Message.account_id == acc.id))
        await s.execute(sa_delete(Chat).where(Chat.account_id == acc.id))
        await s.execute(sa_delete(Account).where(Account.id == acc.id))
        await s.commit()

    # Сбрасываем state сессии
    _state(request).update({
        "tg_user_id": None, "invite_ok": False, "tos_ok": False,
        "client": None, "phone": None, "phone_code_hash": None,
        "qr_login": None, "qr_task": None, "qr_done": False,
    })

    return _ok()


@router.post("/account/settings/save")
async def save_account_settings(body: dict = Body(...)):
    """Сохраняет настройки аккаунта."""
    acc = await _resolve_account(body)
    if not acc:
        return _err("Unauthorized", 403)
    async with SessionLocal() as s:
        a = await s.get(Account, acc.id)
        if body.get("llm_provider") in ("deepseek", "gemini"):
            a.llm_provider = body["llm_provider"]
        if "daily_digest_hour" in body:
            a.daily_digest_hour = max(0, min(23, int(body["daily_digest_hour"])))
        if "auto_tag_enabled" in body:
            a.auto_tag_enabled = bool(body["auto_tag_enabled"])
        if "sentiment_enabled" in body:
            a.sentiment_enabled = bool(body["sentiment_enabled"])

        # ── Персональные LLM-ключи и модель ──
        # Ключи шифруем Fernet'ом перед сохранением.
        # Спецзначение "" (пустая строка) — сброс ключа на серверный дефолт.
        from ...crypto import encrypt as _enc
        if "user_deepseek_api_key" in body:
            raw = (body["user_deepseek_api_key"] or "").strip()
            a.user_deepseek_api_key_enc = _enc(raw) if raw else None
        if "user_deepseek_model" in body:
            m = (body["user_deepseek_model"] or "").strip() or None
            a.user_deepseek_model = m
        if "user_gemini_api_key" in body:
            raw = (body["user_gemini_api_key"] or "").strip()
            a.user_gemini_api_key_enc = _enc(raw) if raw else None
        if "user_gemini_model" in body:
            m = (body["user_gemini_model"] or "").strip() or None
            a.user_gemini_model = m

        await s.commit()
    return _ok()


@router.post("/chats/senders")
async def chats_senders(body: dict = Body(...)):
    """Топ участников чата для фильтра."""
    acc = await _resolve_account(body)
    if not acc:
        return _err("Unauthorized", 403)
    chat_id = body.get("chat_id")
    if not chat_id:
        return _err("chat_id required")
    from ...models import Message
    from sqlalchemy import func
    async with SessionLocal() as s:
        rows = (await s.execute(
            select(Message.sender_name, func.count(Message.id).label("cnt"))
            .where(
                Message.account_id == acc.id,
                Message.chat_id == chat_id,
                Message.sender_name.isnot(None),
                Message.sender_name != "",
                Message.is_outgoing.is_(False),
            )
            .group_by(Message.sender_name)
            .order_by(func.count(Message.id).desc())
            .limit(20)
        )).all()
    return _ok(senders=[{"name": r.sender_name, "count": r.cnt} for r in rows])


# ---------------------------------------------------------------------------
#  Ask (Q&A с памятью)
# ---------------------------------------------------------------------------

@router.post("/ask/sessions")
async def ask_sessions(body: dict = Body(...)):
    """Список диалогов пользователя."""
    acc = await _resolve_account(body)
    if not acc:
        return _err("Unauthorized", 403)
    from ...features import ask as ask_mod
    return _ok(sessions=await ask_mod.list_sessions(acc.id))


@router.post("/ask/history")
async def ask_history(body: dict = Body(...)):
    """Все реплики конкретного диалога."""
    acc = await _resolve_account(body)
    if not acc:
        return _err("Unauthorized", 403)
    sid = body.get("session_id")
    if not sid:
        return _err("session_id required")
    from ...features import ask as ask_mod
    return _ok(messages=await ask_mod.get_history(acc.id, int(sid)))


@router.post("/ask/delete")
async def ask_delete(body: dict = Body(...)):
    """Удалить диалог целиком."""
    acc = await _resolve_account(body)
    if not acc:
        return _err("Unauthorized", 403)
    sid = body.get("session_id")
    if not sid:
        return _err("session_id required")
    from ...features import ask as ask_mod
    ok = await ask_mod.delete_session(acc.id, int(sid))
    return _ok() if ok else _err("Not found", 404)


@router.post("/ask/stream")
async def ask_stream_endpoint(body: dict = Body(...)):
    """SSE: стриминг ответа ассистента + сохранение в БД."""
    acc = await _resolve_account(body)
    if not acc:
        return _err("Unauthorized", 403)

    question   = body.get("question", "")
    session_id = body.get("session_id")
    session_id = int(session_id) if session_id else None

    def sse(type_: str, payload) -> str:
        return f"data: {_json.dumps({'type': type_, 'payload': payload}, ensure_ascii=False)}\n\n"

    async def event_gen():
        try:
            from ...features import ask as ask_mod
            async for ev_type, ev_payload in ask_mod.ask_stream(
                acc.id, session_id, question
            ):
                yield sse(ev_type, ev_payload)
                if ev_type == "error":
                    return
            yield sse("done", "")
        except Exception as e:  # noqa: BLE001
            yield sse("error", f"Ошибка: {e}")

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/summary/stream")
async def summary_stream_endpoint(body: dict = Body(...)):
    """SSE-стриминг результата анализа прямо в браузер."""
    acc = await _resolve_account(body)
    if not acc:
        return _err("Unauthorized", 403)

    kind        = body.get("kind", "daily")
    chat_id     = body.get("chat_id")
    hours       = int(body.get("hours", 12))
    senders     = body.get("senders") or []   # list of sender_names
    force       = bool(body.get("force", False))  # пропустить кеш

    def sse(type_: str, text: str = "") -> str:
        return f"data: {_json.dumps({'type': type_, 'text': text}, ensure_ascii=False)}\n\n"

    async def event_gen():
        try:
            from ...features import summary as sum_mod, stats as stats_mod
            from ...models import Summary
            from datetime import timezone, timedelta

            # Mentions — без LLM, отдаём сразу
            if kind == "mentions":
                yield sse("status", "Ищу упоминания…")
                rows = await stats_mod.mentions_of_me(acc.id, 1)
                if not rows:
                    yield sse("chunk", "Упоминаний за последние сутки не найдено.")
                else:
                    yield sse("chunk", f"📣 Упоминания за сутки ({len(rows)} шт.)\n\n")
                    for chat_title, sender, msg_text, _ in rows:
                        yield sse("chunk", f"**{chat_title}** · {sender}\n{(msg_text or '')[:300]}\n\n")
                yield sse("done", "")
                return

            # Все остальные виды — через full_stream (статусы + стриминг LLM)
            accumulated = []
            async for ev_type, ev_text in sum_mod.full_stream(
                acc.id, kind, chat_id, hours, senders or None, force=force
            ):
                yield sse(ev_type, ev_text)
                if ev_type == "chunk":
                    accumulated.append(ev_text)
                if ev_type == "error":
                    return

            # Сохраняем отчёт в Summary
            full_text = "".join(accumulated).strip()
            if full_text:
                now = datetime.now(timezone.utc)
                period_hours = {"daily": 24, "weekly": 168, "catchup": 12,
                                "important": 168, "actions": 24, "topics": 24}.get(kind, 24)
                async with SessionLocal() as s:
                    s.add(Summary(
                        account_id=acc.id,
                        chat_id=chat_id,
                        kind=kind,
                        period_start=now - timedelta(hours=period_hours),
                        period_end=now,
                        content=full_text,
                    ))
                    await s.commit()

            yield sse("done", "")

        except Exception as e:  # noqa: BLE001
            yield sse("error", f"Ошибка: {e}")

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/summary/run")
async def summary_run(body: dict = Body(...)):
    """Запускает сводку нужного типа и возвращает текст прямо в Mini App."""
    acc = await _resolve_account(body)
    if not acc:
        return _err("Unauthorized", 403)

    kind   = body.get("kind", "daily")   # daily|weekly|catchup|important|actions|topics
    chat_id = body.get("chat_id")        # int — для анализа конкретного чата
    hours  = int(body.get("hours", 12))

    from ...features import summary as sum_mod, stats as stats_mod
    try:
        if kind == "daily":
            text = await sum_mod.daily_summary(acc.id, chat_id)
        elif kind == "weekly":
            text = await sum_mod.weekly_summary(acc.id, chat_id)
        elif kind == "catchup":
            text = await sum_mod.catchup_summary(acc.id, hours)
        elif kind == "important":
            text = await sum_mod.important_week(acc.id)
        elif kind == "actions":
            text = await sum_mod.action_items(acc.id)
        elif kind == "topics":
            text = await sum_mod.topics(acc.id, 1, chat_id)
        elif kind == "mentions":
            rows = await stats_mod.mentions_of_me(acc.id, 1)
            if not rows:
                text = "Упоминаний за последние сутки не найдено."
            else:
                lines = [f"📣 Упоминания за сутки ({len(rows)} шт.)\n"]
                for chat_title, sender, msg_text, _ in rows:
                    lines.append(f"**{chat_title}** · {sender}\n{(msg_text or '')[:300]}")
                text = "\n\n".join(lines)
        else:
            return _err("Unknown kind")
    except Exception as e:  # noqa: BLE001
        return _err(f"Ошибка анализа: {e}")

    return _ok(text=text, kind=kind)


@router.post("/backfill/start")
async def backfill_start(body: dict = Body(...)):
    acc = await _resolve_account(body)
    if not acc:
        return _err("Unauthorized", 403)
    st = backfill_mod.get_state(acc.id)
    if st["status"] == "running":
        return _ok(already_running=True, **st)
    client = account_manager.get_client(acc.id)
    if not client:
        return _err("Userbot не запущен")
    asyncio.create_task(backfill_mod.run_backfill(acc.id, client))
    return _ok(started=True)


@router.post("/backfill/status")
async def backfill_status(body: dict = Body(...)):
    acc = await _resolve_account(body)
    if not acc:
        return _err("Unauthorized", 403)
    st = backfill_mod.get_state(acc.id)
    return _ok(**st)


# ---------------------------------------------------------------------------
#  Chats management
# ---------------------------------------------------------------------------

@router.post("/chats/list")
async def chats_list(body: dict = Body(...)):
    from sqlalchemy import func as sqlfunc
    acc = await _resolve_account(body)
    if not acc:
        return _err("Unauthorized", 403)

    search = body.get("search", "").lower().strip()
    offset = int(body.get("offset", 0))
    limit = min(int(body.get("limit", 50)), 100)

    from ...models import Chat as ChatModel, Message as MsgModel
    async with SessionLocal() as s:
        q = (
            select(
                ChatModel.id,
                ChatModel.tg_chat_id,
                ChatModel.title,
                ChatModel.type,
                ChatModel.is_tracked,
                ChatModel.is_muted,
                ChatModel.is_priority,
                ChatModel.ignore_reason,
                sqlfunc.count(MsgModel.id).label("msg_count"),
            )
            .outerjoin(MsgModel, MsgModel.chat_id == ChatModel.id)
            .where(ChatModel.account_id == acc.id)
            .group_by(ChatModel.id)
            .order_by(sqlfunc.count(MsgModel.id).desc())
        )
        if search:
            from sqlalchemy import cast
            q = q.where(ChatModel.title.ilike(f"%{search}%"))
        rows = (await s.execute(q.offset(offset).limit(limit))).all()

    chats = [
        {
            "id": r.id,
            "tg_chat_id": r.tg_chat_id,
            "title": r.title or "(без названия)",
            "type": r.type,
            "is_tracked": r.is_tracked,
            "is_muted": r.is_muted,
            "is_priority": r.is_priority,
            "ignore_reason": r.ignore_reason,
            "msg_count": r.msg_count,
        }
        for r in rows
    ]
    return _ok(chats=chats, offset=offset, has_more=len(rows) == limit)


@router.post("/chats/toggle")
async def chats_toggle(body: dict = Body(...)):
    acc = await _resolve_account(body)
    if not acc:
        return _err("Unauthorized", 403)

    chat_id = body.get("chat_id")
    action = body.get("action")  # track | untrack | mute | unmute

    from ...models import Chat as ChatModel
    async with SessionLocal() as s:
        chat = (
            await s.execute(
                select(ChatModel).where(
                    ChatModel.id == chat_id,
                    ChatModel.account_id == acc.id,
                )
            )
        ).scalar_one_or_none()
        if not chat:
            return _err("Chat not found")

        if action == "track":
            chat.is_tracked = True
            chat.ignore_reason = None
        elif action == "untrack":
            chat.is_tracked = False
            chat.ignore_reason = "manual"
        elif action == "mute":
            chat.is_muted = True
        elif action == "unmute":
            chat.is_muted = False
        elif action == "priority":
            chat.is_priority = True
        elif action == "unpriority":
            chat.is_priority = False
        else:
            return _err("Unknown action")
        await s.commit()

    return _ok(is_tracked=chat.is_tracked, is_muted=chat.is_muted, is_priority=chat.is_priority)


@router.post("/chats/delete_data")
async def chats_delete_data(body: dict = Body(...)):
    """Удаляет все сообщения конкретного чата из БД (сам чат остаётся)."""
    acc = await _resolve_account(body)
    if not acc:
        return _err("Unauthorized", 403)

    chat_id = body.get("chat_id")
    from sqlalchemy import delete as sqldel
    from ...models import Chat as ChatModel, Message as MsgModel
    async with SessionLocal() as s:
        chat = (
            await s.execute(
                select(ChatModel).where(
                    ChatModel.id == chat_id,
                    ChatModel.account_id == acc.id,
                )
            )
        ).scalar_one_or_none()
        if not chat:
            return _err("Chat not found")
        deleted = await s.execute(
            sqldel(MsgModel).where(MsgModel.chat_id == chat_id)
        )
        await s.commit()
    return _ok(deleted=deleted.rowcount)


# ---------------------------------------------------------------------------
#  Privacy settings
# ---------------------------------------------------------------------------

@router.post("/privacy/get")
async def privacy_get(body: dict = Body(...)):
    acc = await _resolve_account(body)
    if not acc:
        return _err("Unauthorized", 403)
    return _ok(
        analyze_dms=acc.analyze_dms,
        analyze_groups=acc.analyze_groups,
        analyze_channels=acc.analyze_channels,
        ignore_bots=acc.ignore_bots,
    )


@router.post("/privacy/update")
async def privacy_update(body: dict = Body(...)):
    acc = await _resolve_account(body)
    if not acc:
        return _err("Unauthorized", 403)

    fields = ("analyze_dms", "analyze_groups", "analyze_channels", "ignore_bots")
    async with SessionLocal() as s:
        a = await s.get(Account, acc.id)
        for f in fields:
            if f in body:
                setattr(a, f, bool(body[f]))
        await s.commit()
    return _ok()


@router.post("/privacy/delete_all_messages")
async def privacy_delete_all(body: dict = Body(...)):
    """Удаляет все сообщения пользователя из хранилища."""
    acc = await _resolve_account(body)
    if not acc:
        return _err("Unauthorized", 403)
    from sqlalchemy import delete as sqldel
    from ...models import Message as MsgModel
    async with SessionLocal() as s:
        result = await s.execute(sqldel(MsgModel).where(MsgModel.account_id == acc.id))
        await s.commit()
    return _ok(deleted=result.rowcount)


# ════════════════════════════════════════════════════════════════════════════
#                    🛠 Полезные функции (utils)
# ════════════════════════════════════════════════════════════════════════════

def _sse(type_: str, text: str = "") -> str:
    return f"data: {_json.dumps({'type': type_, 'text': text}, ensure_ascii=False)}\n\n"


@router.post("/utils/silent")
async def utils_silent(body: dict = Body(...)):
    """Кому давно не писал."""
    acc = await _resolve_account(body)
    if not acc:
        return _err("Unauthorized", 403)
    from ...features import utils as utils_mod
    items = await utils_mod.silent_contacts(
        acc.id,
        limit=int(body.get("limit", 50)),
        only_dm=bool(body.get("only_dm", True)),
    )
    return _ok(items=items)


@router.post("/utils/promises/stream")
async def utils_promises_stream(body: dict = Body(...)):
    """Стрим LLM-ответа: невыполненные обещания."""
    acc = await _resolve_account(body)
    if not acc:
        return _err("Unauthorized", 403)

    days = int(body.get("days", 14))
    from ...features import utils as utils_mod
    from ...llm import complete_stream

    async def gen():
        try:
            yield _sse("status", "Ищу обещания в исходящих…")
            prompt, err = await utils_mod.promises_prompt(acc.id, days)
            if err:
                yield _sse("chunk", err)
                yield _sse("done", "")
                return
            yield _sse("status", "Анализирую через ИИ…")
            async for chunk in complete_stream(
                prompt,
                system=utils_mod.PROMISES_SYSTEM,
                account_id=acc.id,
            ):
                yield _sse("chunk", chunk)
            yield _sse("done", "")
        except Exception as e:  # noqa: BLE001
            yield _sse("error", f"Ошибка: {e}")

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/utils/contact-card/stream")
async def utils_contact_card_stream(body: dict = Body(...)):
    """Стрим: AI-карточка контакта."""
    acc = await _resolve_account(body)
    if not acc:
        return _err("Unauthorized", 403)

    chat_id = int(body.get("chat_id"))
    days = int(body.get("days", 365))
    from ...features import utils as utils_mod
    from ...llm import complete_stream

    async def gen():
        try:
            yield _sse("status", "Загружаю переписку…")
            prompt, err = await utils_mod.contact_card_prompt(acc.id, chat_id, days)
            if err:
                yield _sse("chunk", err)
                yield _sse("done", "")
                return
            yield _sse("status", "Составляю карточку…")
            async for chunk in complete_stream(prompt, account_id=acc.id):
                yield _sse("chunk", chunk)
            yield _sse("done", "")
        except Exception as e:  # noqa: BLE001
            yield _sse("error", f"Ошибка: {e}")

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/utils/analytics")
async def utils_analytics(body: dict = Body(...)):
    """Personal analytics — гистограммы и топ-контакты."""
    acc = await _resolve_account(body)
    if not acc:
        return _err("Unauthorized", 403)
    from ...features import utils as utils_mod
    data = await utils_mod.personal_analytics(
        acc.id, days=int(body.get("days", 90)),
    )
    return _ok(**data)


@router.post("/utils/export-chat")
async def utils_export_chat(body: dict = Body(...)):
    """Экспорт чата в markdown — возвращает текст и имя файла."""
    acc = await _resolve_account(body)
    if not acc:
        return _err("Unauthorized", 403)
    from ...features import utils as utils_mod
    fname, md = await utils_mod.export_chat_md(
        acc.id,
        chat_id=int(body.get("chat_id")),
        days=int(body.get("days", 365)),
    )
    if not fname:
        return _err("Чат не найден или пуст")
    return _ok(filename=fname, markdown=md, size=len(md))


@router.post("/utils/export-chat/send")
async def utils_export_chat_send(body: dict = Body(...)):
    """Экспорт чата: отправляет .md файл в Saved Messages пользователя через Telegram."""
    acc = await _resolve_account(body)
    if not acc:
        return _err("Unauthorized", 403)
    from ...features import utils as utils_mod
    from ... import account_manager
    import io

    fname, md = await utils_mod.export_chat_md(
        acc.id,
        chat_id=int(body.get("chat_id")),
        days=int(body.get("days", 365)),
    )
    if not fname:
        return _err("Чат не найден или пуст")

    client = account_manager.get_client(acc.id)
    if not client:
        return _err("Клиент Telegram не активен")

    try:
        file_obj = io.BytesIO(md.encode("utf-8"))
        file_obj.name = fname
        await client.send_file(
            "me",
            file_obj,
            caption=f"📥 Экспорт чата: {fname}",
            force_document=True,
        )
    except Exception as e:
        return _err(f"Ошибка отправки: {e}")

    return _ok(filename=fname, size=len(md))


# ---------------------------------------------------------------------------
#  Reports (история отчётов)
# ---------------------------------------------------------------------------

_KIND_LABELS = {
    "daily": "📅 Дайджест за сутки",
    "weekly": "🗓 Дайджест за неделю",
    "catchup": "⏪ Catch-up",
    "important": "🔥 Важное",
    "actions": "✅ Задачи",
    "topics": "🗂 Темы",
    "timeline": "📅 Хронология",
}


@router.post("/reports/list")
async def reports_list(body: dict = Body(...)):
    """Список сохранённых отчётов (без content)."""
    acc = await _resolve_account(body)
    if not acc:
        return _err("Unauthorized", 403)
    from ...models import Summary, Chat
    limit = int(body.get("limit", 50))
    async with SessionLocal() as s:
        rows = (await s.execute(
            select(Summary)
            .where(Summary.account_id == acc.id)
            .order_by(Summary.created_at.desc())
            .limit(limit)
        )).scalars().all()
        # Собираем chat titles для chat_id
        chat_ids = list({r.chat_id for r in rows if r.chat_id})
        chat_map: dict[int, str] = {}
        if chat_ids:
            chats = (await s.execute(
                select(Chat.id, Chat.title).where(Chat.id.in_(chat_ids))
            )).all()
            chat_map = {c.id: c.title for c in chats}
    items = []
    for r in rows:
        items.append({
            "id": r.id,
            "kind": r.kind,
            "label": _KIND_LABELS.get(r.kind, r.kind),
            "chat_title": chat_map.get(r.chat_id) if r.chat_id else None,
            "created_at": r.created_at.isoformat(),
            "preview": r.content[:120].replace("\n", " "),
        })
    return _ok(reports=items)


@router.post("/reports/get")
async def reports_get(body: dict = Body(...)):
    """Полный текст отчёта."""
    acc = await _resolve_account(body)
    if not acc:
        return _err("Unauthorized", 403)
    from ...models import Summary
    rid = body.get("id")
    if not rid:
        return _err("id required")
    async with SessionLocal() as s:
        row = (await s.execute(
            select(Summary).where(Summary.id == int(rid), Summary.account_id == acc.id)
        )).scalar_one_or_none()
    if not row:
        return _err("Not found", 404)
    return _ok(
        id=row.id,
        kind=row.kind,
        label=_KIND_LABELS.get(row.kind, row.kind),
        content=row.content,
        created_at=row.created_at.isoformat(),
    )


@router.post("/reports/delete")
async def reports_delete(body: dict = Body(...)):
    """Удалить отчёт."""
    acc = await _resolve_account(body)
    if not acc:
        return _err("Unauthorized", 403)
    from ...models import Summary
    rid = body.get("id")
    if not rid:
        return _err("id required")
    async with SessionLocal() as s:
        row = (await s.execute(
            select(Summary).where(Summary.id == int(rid), Summary.account_id == acc.id)
        )).scalar_one_or_none()
        if not row:
            return _err("Not found", 404)
        await s.delete(row)
        await s.commit()
    return _ok()


@router.post("/utils/links")
async def utils_links(body: dict = Body(...)):
    """Все ссылки за период с контекстом."""
    acc = await _resolve_account(body)
    if not acc:
        return _err("Unauthorized", 403)
    from ...features import utils as utils_mod
    items = await utils_mod.extract_links(
        acc.id,
        days=int(body.get("days", 30)),
        limit=int(body.get("limit", 200)),
    )
    return _ok(items=items, count=len(items))
