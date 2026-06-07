"""Шифрование Telegram-сессий: Fernet (AES-128-CBC + HMAC) симметричный ключ из .env."""
from __future__ import annotations

from cryptography.fernet import Fernet

from .config import get_settings

_settings = get_settings()
_fernet = Fernet(_settings.session_encryption_key.encode())


def encrypt(plaintext: str) -> str:
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet.decrypt(token.encode()).decode()
