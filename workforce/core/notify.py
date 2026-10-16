"""Wake-up notifications so runtimes react immediately to new work.

With REDIS_URL set, enqueue/send publish `noesis:wake` with the target bot id
and workers wake the matching runtime. Without Redis, runtimes fall back to a
cheap indexed SQLite check every NOESIS_POLL_INTERVAL seconds. Either way no
model is ever asked whether there is work.
"""
from __future__ import annotations

import threading

from .settings import get_settings

CHANNEL = "noesis:wake"
_client = None
_lock = threading.Lock()


def _redis():
    global _client
    url = get_settings().redis_url
    if not url:
        return None
    with _lock:
        if _client is None:
            try:
                import redis
                _client = redis.Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2)
            except Exception:  # noqa: BLE001
                return None
    return _client


def wake(bot_id: str | None) -> None:
    if not bot_id:
        return
    r = _redis()
    if r is None:
        return
    try:
        r.publish(CHANNEL, bot_id)
    except Exception:  # noqa: BLE001  Redis is an optimisation, never a dependency
        pass


def subscribe(callback) -> threading.Thread | None:
    """Run callback(bot_id) for each wake message in a daemon thread."""
    r = _redis()
    if r is None:
        return None

    def loop():
        import time

        import redis
        # a subscriber sits idle for long stretches: no read timeout, or it drops every
        # few seconds and wake-ups are lost (publishing keeps the short timeout)
        sub = redis.Redis.from_url(get_settings().redis_url, socket_timeout=None, socket_connect_timeout=5,
                                   health_check_interval=30)
        while True:
            try:
                ps = sub.pubsub(ignore_subscribe_messages=True)
                ps.subscribe(CHANNEL)
                for m in ps.listen():
                    callback(m["data"].decode() if isinstance(m["data"], bytes) else str(m["data"]))
            except Exception:  # noqa: BLE001
                time.sleep(5)

    t = threading.Thread(target=loop, daemon=True, name="redis-wake")
    t.start()
    return t
