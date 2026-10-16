"""Single-owner authentication: PBKDF2 password hash + HMAC-signed session cookie.

No signup endpoint exists. Login attempts are rate limited per client IP.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from collections import defaultdict, deque

COOKIE = "noesis_session"
SESSION_TTL = 7 * 86400


def hash_password(password: str, iterations: int = 390_000) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    # ':' separators (not '$') so the value survives docker compose's .env interpolation unquoted
    return f"pbkdf2_sha256:{iterations}:{base64.b64encode(salt).decode()}:{base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, it, salt, dk = stored.replace("$", ":").split(":")
        if algo != "pbkdf2_sha256":
            return False
        got = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(it))
        return hmac.compare_digest(got, base64.b64decode(dk))
    except (ValueError, TypeError):
        return False


def _secret() -> bytes:
    s = os.environ.get("NOESIS_SESSION_SECRET", "")
    if len(s) < 32:
        raise RuntimeError("NOESIS_SESSION_SECRET must be set (>= 32 chars)")
    return s.encode()


def make_session() -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"sub": "owner", "exp": time.time() + SESSION_TTL,
                                                   "n": secrets.token_hex(8)}).encode()).decode()
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def check_session(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    payload, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sig, hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()):
        return False
    try:
        data = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        return False
    return data.get("sub") == "owner" and data.get("exp", 0) > time.time()


class LoginLimiter:
    def __init__(self, max_attempts: int = 5, window_s: int = 900):
        self.max, self.window = max_attempts, window_s
        self.hits: dict[str, deque] = defaultdict(deque)

    def allowed(self, key: str) -> bool:
        q = self.hits[key]
        while q and q[0] < time.time() - self.window:
            q.popleft()
        return len(q) < self.max

    def fail(self, key: str) -> None:
        self.hits[key].append(time.time())

    def reset(self, key: str) -> None:
        self.hits.pop(key, None)
