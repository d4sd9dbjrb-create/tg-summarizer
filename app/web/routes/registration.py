"""Мастер регистрации: invite -> ToS -> phone/QR -> code -> 2FA -> done."""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import secrets
from datetime import datetime, timedelta, timezone

import qrcode
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
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
from ...models import Account

log = logging.getLogger(__name__)
settings = get_settings()
router = APIRouter()


# In-memory pending clients for ongoing logins (по session-id из cookie).
# Не персистим — если процесс перезапустился, юзер начинает заново.
_pending: dict[str, dict] = {}


def _ensure_sid(request: Request) -> str:
    sid = request.session.get("sid")
    if not sid:
        sid = secrets.token_urlsafe(24)
        request.session["sid"] = sid
    return sid


def _state(request: Request) -> dict:
    sid = _ensure_sid(request)
    if sid not in _pending:
        _pending[sid] = {
            "invite_ok": False,
            "tos_accepted": False,
            "client": None,        # TelegramClient
            "phone": None,
            "phone_code_hash": None,
            "qr_login": None,      # QRLogin object
            "qr_task": None,
            "qr_done": False,
        }
    return _pending[sid]


def _tpl(request: Request, name: str, **ctx):
    return request.app.state.templates.TemplateResponse(name, {"request": request, **ctx})


def _make_client() -> TelegramClient:
    c = TelegramClient(StringSession(), settings.tg_api_id, settings.tg_api_hash)
    return c


# ---------------------------------------------------------------------------
#  Step 1: invite
# ---------------------------------------------------------------------------

@router.get("/invite", response_class=HTMLResponse)
async def invite_get(request: Request):
    return _tpl(request, "invite.html", error=None)


@router.post("/invite", response_class=HTMLResponse)
async def invite_post(request: Request, code: str = Form(...)):
    st = _state(request)
    if code.strip() != settings.invite_code:
        return _tpl(request, "invite.html", error="Неверный код доступа")
    st["invite_ok"] = True
    return RedirectResponse("/tos-accept", status_code=303)


# ---------------------------------------------------------------------------
#  Step 2: ToS
# ---------------------------------------------------------------------------

@router.get("/tos-accept", response_class=HTMLResponse)
async def tos_get(request: Request):
    st = _state(request)
    if not st["invite_ok"]:
        return RedirectResponse("/invite", status_code=303)
    return _tpl(request, "tos_accept.html", error=None)


@router.post("/tos-accept", response_class=HTMLResponse)
async def tos_post(
    request: Request,
    accept_tos: str = Form(default=""),
    accept_risk: str = Form(default=""),
):
    st = _state(request)
    if not st["invite_ok"]:
        return RedirectResponse("/invite", status_code=303)
    if accept_tos != "on" or accept_risk != "on":
        return _tpl(request, "tos_accept.html", error="Нужно подтвердить оба пункта")
    st["tos_accepted"] = True
    return RedirectResponse("/login", status_code=303)


# ---------------------------------------------------------------------------
#  Step 3: login (выбор QR vs phone)
# ---------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
async def login_get(request: Request):
    st = _state(request)
    if not st["tos_accepted"]:
        return RedirectResponse("/invite", status_code=303)
    return _tpl(request, "login.html")


# ---------- Phone flow ----------

@router.post("/login/phone", response_class=HTMLResponse)
async def login_phone(request: Request, phone: str = Form(...)):
    st = _state(request)
    if not st["tos_accepted"]:
        return RedirectResponse("/invite", status_code=303)

    phone = phone.strip().replace(" ", "")
    client = _make_client()
    try:
        await client.connect()
        sent = await client.send_code_request(phone)
    except PhoneNumberInvalidError:
        await client.disconnect()
        return _tpl(request, "login.html", error="Неверный номер телефона")
    except Exception as e:  # noqa: BLE001
        log.exception("send_code_request failed")
        await client.disconnect()
        return _tpl(request, "login.html", error=f"Ошибка Telegram: {e}")

    st["client"] = client
    st["phone"] = phone
    st["phone_code_hash"] = sent.phone_code_hash
    return RedirectResponse("/verify", status_code=303)


@router.get("/verify", response_class=HTMLResponse)
async def verify_get(request: Request):
    st = _state(request)
    if not st.get("client") or not st.get("phone"):
        return RedirectResponse("/login", status_code=303)
    return _tpl(request, "verify.html", phone=st["phone"], error=None)


@router.post("/verify", response_class=HTMLResponse)
async def verify_post(request: Request, code: str = Form(...)):
    st = _state(request)
    client: TelegramClient | None = st.get("client")
    if not client or not st.get("phone"):
        return RedirectResponse("/login", status_code=303)

    try:
        await client.sign_in(
            phone=st["phone"],
            code=code.strip(),
            phone_code_hash=st["phone_code_hash"],
        )
    except SessionPasswordNeededError:
        return RedirectResponse("/2fa", status_code=303)
    except PhoneCodeInvalidError:
        return _tpl(request, "verify.html", phone=st["phone"], error="Неверный код")
    except PhoneCodeExpiredError:
        return _tpl(request, "verify.html", phone=st["phone"], error="Код истёк, начните заново")
    except Exception as e:  # noqa: BLE001
        log.exception("sign_in failed")
        return _tpl(request, "verify.html", phone=st["phone"], error=f"Ошибка: {e}")

    return await _finalize(request, client)


# ---------- 2FA ----------

@router.get("/2fa", response_class=HTMLResponse)
async def tfa_get(request: Request):
    st = _state(request)
    if not st.get("client"):
        return RedirectResponse("/login", status_code=303)
    return _tpl(request, "tfa.html", error=None)


@router.post("/2fa", response_class=HTMLResponse)
async def tfa_post(request: Request, password: str = Form(...)):
    st = _state(request)
    client: TelegramClient | None = st.get("client")
    if not client:
        return RedirectResponse("/login", status_code=303)
    try:
        await client.sign_in(password=password)
    except Exception as e:  # noqa: BLE001
        log.exception("2fa failed")
        return _tpl(request, "tfa.html", error=f"Ошибка: {e}")
    return await _finalize(request, client)


# ---------- QR flow ----------

@router.get("/login/qr", response_class=HTMLResponse)
async def login_qr_get(request: Request):
    st = _state(request)
    if not st["tos_accepted"]:
        return RedirectResponse("/invite", status_code=303)

    if st.get("client") is None:
        client = _make_client()
        await client.connect()
        st["client"] = client

    client = st["client"]
    qr = await client.qr_login()
    st["qr_login"] = qr

    # Запускаем фоновый wait в task, чтобы при сканировании сразу финализировать.
    async def _wait_qr():
        try:
            while True:
                try:
                    res = await qr.wait(timeout=30)
                    if res:
                        st["qr_done"] = True
                        return
                except asyncio.TimeoutError:
                    # рефреш токена
                    try:
                        await qr.recreate()
                    except Exception:  # noqa: BLE001
                        return
                except SessionPasswordNeededError:
                    st["qr_done"] = "2fa"
                    return
        except Exception:  # noqa: BLE001
            log.exception("qr wait crashed")

    if st.get("qr_task") is None or st["qr_task"].done():
        st["qr_task"] = asyncio.create_task(_wait_qr())

    img = qrcode.make(qr.url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    qr_b64 = base64.b64encode(buf.getvalue()).decode()
    return _tpl(request, "qr.html", qr_b64=qr_b64)


@router.get("/login/qr/status")
async def login_qr_status(request: Request):
    st = _state(request)
    done = st.get("qr_done")
    if done == "2fa":
        return {"status": "2fa"}
    if done is True:
        return {"status": "ok"}
    return {"status": "wait"}


@router.post("/login/qr/finalize", response_class=HTMLResponse)
async def login_qr_finalize(request: Request):
    st = _state(request)
    client = st.get("client")
    if not client:
        return RedirectResponse("/login", status_code=303)
    if st.get("qr_done") == "2fa":
        return RedirectResponse("/2fa", status_code=303)
    if st.get("qr_done") is True:
        return await _finalize(request, client)
    return RedirectResponse("/login/qr", status_code=303)


# ---------------------------------------------------------------------------
#  Final: persist Account, выдать link-token, показать deep-link на бот
# ---------------------------------------------------------------------------

async def _finalize(request: Request, client: TelegramClient) -> HTMLResponse:
    # Проверяем, что действительно авторизованы
    if not await client.is_user_authorized():
        return _tpl(request, "login.html", error="Авторизация не завершена")

    encrypted = encrypt(client.session.save())

    # Создаём Account (через account_manager — он же поднимет клиент в hot-set)
    acc = await account_manager.persist_account(client, encrypted)

    # Генерим one-time link-token
    token = secrets.token_urlsafe(24)
    async with SessionLocal() as s:
        a = await s.get(Account, acc.id)
        a.link_token = token
        a.link_token_expires_at = datetime.now(timezone.utc) + timedelta(minutes=30)
        a.accepted_tos_at = datetime.now(timezone.utc)
        await s.commit()

    # Чистим pending для этой сессии
    sid = request.session.get("sid")
    _pending.pop(sid, None)
    request.session.clear()

    deep_link = f"https://t.me/{settings.control_bot_username}?start={token}"
    return _tpl(
        request,
        "done.html",
        deep_link=deep_link,
        bot=settings.control_bot_username,
    )
