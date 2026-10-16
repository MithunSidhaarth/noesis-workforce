"""Owner notifications: urgent things now, everything else in the morning brief.

Every notification is stored (deduplicated by key). It is delivered immediately
only when it is urgent, outside quiet hours and under the daily cap; otherwise
it is held and either flushed when quiet hours end (urgent) or folded into the
next morning brief (non-urgent). Delivery itself still goes through the
ActionGateway, so it is audited, idempotent and respects the emergency pause.
"""
from __future__ import annotations

import os
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from .core.db import Database, dumps, loads
from .core.settings import get_settings
from .core.util import new_id, now

DEFAULT_QUIET = "23:00-07:30"
DEFAULT_CAP = 8


def prefs() -> dict:
    from .sources.registry import load_profile
    p = (load_profile().get("notifications") or {})
    # blank env values (as in .env.example) fall back to the profile, then the defaults
    quiet = os.environ.get("NOESIS_QUIET_HOURS") or p.get("quiet_hours") or DEFAULT_QUIET
    cap = int(os.environ.get("NOESIS_MAX_PINGS_PER_DAY") or p.get("max_immediate_per_day") or DEFAULT_CAP)
    return {"quiet_hours": quiet, "max_immediate_per_day": cap}


def _parse_quiet(spec: str | None) -> tuple[dtime, dtime] | None:
    if not spec or str(spec).strip().lower() in ("off", "none", ""):
        return None
    a, b = str(spec).split("-")
    h1, m1 = map(int, a.strip().split(":"))
    h2, m2 = map(int, b.strip().split(":"))
    return dtime(h1, m1), dtime(h2, m2)


def in_quiet_hours(t: float | None = None, spec: str | None = None) -> bool:
    q = _parse_quiet(spec if spec is not None else prefs()["quiet_hours"])
    if not q:
        return False
    local = datetime.fromtimestamp(t or now(), ZoneInfo(get_settings().timezone)).time()
    start, end = q
    return (start <= local or local < end) if start > end else (start <= local < end)


def day_start(t: float | None = None) -> float:
    d = datetime.fromtimestamp(t or now(), ZoneInfo(get_settings().timezone))
    return datetime.combine(d.date(), dtime(0, 0), d.tzinfo).timestamp()


def sent_today(db: Database) -> int:
    return db.scalar("SELECT COUNT(*) FROM notifications WHERE status='sent' AND sent_at>=?", (day_start(),))


def register(db: Database, bot_id: str, key: str, text: str, urgent: bool, buttons: list | None) -> tuple[dict, bool]:
    """Store the notification. Returns (row, created)."""
    row = db.one("SELECT * FROM notifications WHERE key=?", (key,))
    if row:
        return row, False
    db.execute("INSERT OR IGNORE INTO notifications(id, key, bot_id, text, buttons_json, urgent, status, created_at) "
               "VALUES (?,?,?,?,?,?,?,?)", (new_id("ntf"), key, bot_id, text, dumps(buttons) if buttons else None,
                                            int(urgent), "held", now()))
    return db.one("SELECT * FROM notifications WHERE key=?", (key,)), True


def should_send_now(db: Database, urgent: bool) -> bool:
    if not urgent:
        return False
    p = prefs()
    return not in_quiet_hours(spec=p["quiet_hours"]) and sent_today(db) < p["max_immediate_per_day"]


def mark(db: Database, nid: str, status: str) -> None:
    db.execute("UPDATE notifications SET status=?, sent_at=? WHERE id=?", (status, now(), nid))


def held(db: Database, urgent_only: bool = False, limit: int = 50) -> list[dict]:
    rows = db.all("SELECT * FROM notifications WHERE status='held'" + (" AND urgent=1" if urgent_only else "")
                  + " ORDER BY urgent DESC, created_at LIMIT ?", (limit,))
    for r in rows:
        r["buttons"] = loads(r.pop("buttons_json"), None)
    return rows


def next_quiet_end(t: float | None = None) -> float:
    q = _parse_quiet(prefs()["quiet_hours"])
    if not q:
        return t or now()
    tz = ZoneInfo(get_settings().timezone)
    d = datetime.fromtimestamp(t or now(), tz)
    end = datetime.combine(d.date(), q[1], tz)
    if end <= d:
        end += timedelta(days=1)
    return end.timestamp()
