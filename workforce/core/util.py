from __future__ import annotations

import re

import hashlib
import random
import time
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def now() -> float:
    return time.time()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def sha(*parts: object) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p).encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


def backoff(attempt: int, base: float = 5.0, cap: float = 3600.0) -> float:
    """Exponential backoff with full jitter (attempt starts at 1)."""
    ceiling = min(cap, base * (2 ** max(0, attempt - 1)))
    return random.uniform(ceiling / 2, ceiling)


def iso(ts: float | None, tz: str = "Asia/Kolkata") -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, ZoneInfo(tz)).replace(microsecond=0).isoformat()


def utc_iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).replace(microsecond=0).isoformat()


_SECRET_PATTERNS = [
    (re.compile(r"bot\d{5,}:[A-Za-z0-9_-]{20,}"), "bot<redacted>"),                    # Telegram bot token in URLs
    (re.compile(r"(?<![A-Za-z0-9])\d{5,15}:[A-Za-z0-9_-]{30,64}"), "<redacted-bot-token>"),  # a bare BotFather token
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{12,}"), r"\1<redacted>"),
    (re.compile(r"(?i)((?:access_token|refresh_token|client_secret|api_key|key|token)=)[^&\s\"']+"), r"\1<redacted>"),
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}"), "sk-ant-<redacted>"),
    (re.compile(r"gsk_[A-Za-z0-9]{20,}"), "gsk_<redacted>"),
    (re.compile(r"AIza[0-9A-Za-z_-]{30,}"), "AIza<redacted>"),
    (re.compile(r"1//[0-9A-Za-z_-]{30,}"), "<redacted-google-refresh-token>"),
]


def redact(text: str) -> str:
    """Remove credentials from text before it reaches logs or the database."""
    if not text:
        return text
    for pat, rep in _SECRET_PATTERNS:
        text = pat.sub(rep, text)
    return text
