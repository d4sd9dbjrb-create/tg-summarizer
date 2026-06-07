"""Утилиты для Telegram Mini App: верификация initData."""
from __future__ import annotations

import hashlib
import hmac
import json
from urllib.parse import parse_qsl, unquote


def verify_init_data(init_data: str, bot_token: str) -> dict | None:
    """Верифицирует initData от Telegram Mini App.

    Возвращает распарсенные данные или None если подпись неверна.
    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    """
    try:
        params = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = params.pop("hash", None)
        if not received_hash:
            return None

        data_check_string = "\n".join(
            f"{k}={v}" for k, v in sorted(params.items())
        )
        secret_key = hmac.new(key=b"WebAppData", msg=bot_token.encode(), digestmod=hashlib.sha256).digest()
        expected = hmac.new(key=secret_key, msg=data_check_string.encode(), digestmod=hashlib.sha256).hexdigest()

        if not hmac.compare_digest(expected, received_hash):
            return None

        result = dict(params)
        if "user" in result:
            result["user"] = json.loads(unquote(result["user"]))
        return result
    except Exception:  # noqa: BLE001
        return None
