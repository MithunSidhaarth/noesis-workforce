from __future__ import annotations

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
