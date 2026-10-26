"""Encryption at rest for tokens the owner pastes into the dashboard (BotFather tokens).

Fernet (AES-128-CBC + HMAC-SHA256) with a key from the NOESIS_SECRET_KEY env var. The
key never lives in the database, Git, logs or the dashboard bundle. Bots never get a
decrypt path: only the Telegram poller and the reply sender call `decrypt`.

  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
from __future__ import annotations

import os
import re


class SecretKeyMissing(RuntimeError):
    pass


def _fernet():
    from cryptography.fernet import Fernet
    key = os.environ.get("NOESIS_SECRET_KEY", "").strip()
    if not key:
        raise SecretKeyMissing("NOESIS_SECRET_KEY is not set. Generate one with: python -c \"from cryptography.fernet "
                               "import Fernet; print(Fernet.generate_key().decode())\" and add it to .env")
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as e:
        raise SecretKeyMissing("NOESIS_SECRET_KEY is not a valid Fernet key (44 url-safe base64 characters)") from e


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    from cryptography.fernet import InvalidToken
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as e:
        raise SecretKeyMissing("stored token can't be decrypted: NOESIS_SECRET_KEY changed since it was saved") from e


BOT_TOKEN_RE = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{30,64}$")


def valid_bot_token(token: str) -> bool:
    return bool(BOT_TOKEN_RE.match((token or "").strip()))


def mask_token(token: str) -> str:
    """'123456789:AAH...' -> '123456789:••••'. The bot id part is public (it's in the bot's links)."""
    t = (token or "").strip()
    return f"{t.split(':', 1)[0]}:••••" if ":" in t else "••••"
