"""Global system state (pause / dry-run), audit log and structured execution log."""
from __future__ import annotations

import json
import logging
import sys

from .db import Database, dumps, get_db, loads
from .settings import get_settings
from .util import now

log = logging.getLogger("noesis")


def configure_logging(level: str = "INFO") -> None:
    class JsonFmt(logging.Formatter):
        def format(self, r: logging.LogRecord) -> str:
            d = {"ts": round(r.created, 3), "level": r.levelname, "logger": r.name, "msg": r.getMessage()}
            for k in ("bot_id", "task_id", "correlation_id"):
                if hasattr(r, k):
                    d[k] = getattr(r, k)
            if r.exc_info:
                d["exc"] = self.formatException(r.exc_info)
            return json.dumps(d, default=str)

    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(JsonFmt())
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(level)


class SystemState:
    """Key flags live in the DB so every process sees the same values.
    Only humans (dashboard / Telegram owner / CLI) may change them; the API layer
    enforces that bots never get a handle to these setters."""

    PAUSED = "paused"
    DRY_RUN = "dry_run"

    def __init__(self, db: Database | None = None):
        self.db = db or get_db()

    def get(self, key: str, default=None):
        r = self.db.one("SELECT value FROM system_state WHERE key=?", (key,))
        return loads(r["value"]) if r else default

    def set(self, key: str, value, by: str) -> None:
        self.db.execute(
            "INSERT INTO system_state(key, value, updated_at, updated_by) VALUES (?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at, updated_by=excluded.updated_by",
            (key, dumps(value), now(), by),
        )
        audit(self.db, by, f"system.{key}.set", details={"value": value})

    @property
    def paused(self) -> bool:
        return bool(self.get(self.PAUSED, False))

    @property
    def dry_run(self) -> bool:
        return bool(self.get(self.DRY_RUN, get_settings().dry_run_default))


def audit(db: Database | None, actor: str, event: str, correlation_id: str | None = None, details: dict | None = None) -> None:
    db = db or get_db()
    db.execute(
        "INSERT INTO audit_log(ts, actor, event, correlation_id, details_json) VALUES (?,?,?,?,?)",
        (now(), actor, event, correlation_id, dumps(details or {})),
    )


def exec_log(db: Database | None, level: str, message: str, bot_id: str | None = None, task_id: str | None = None,
             correlation_id: str | None = None, data: dict | None = None) -> None:
    db = db or get_db()
    db.execute(
        "INSERT INTO exec_log(ts, level, bot_id, task_id, correlation_id, message, data_json) VALUES (?,?,?,?,?,?,?)",
        (now(), level, bot_id, task_id, correlation_id, message, dumps(data) if data else None),
    )
    log.log(getattr(logging, level.upper(), logging.INFO), message,
            extra={"bot_id": bot_id, "task_id": task_id, "correlation_id": correlation_id})


def set_integration_status(db: Database | None, name: str, status: str, detail: str = "") -> None:
    db = db or get_db()
    t = now()
    ok = status in ("ok", "mock")
    db.execute(
        "INSERT INTO integrations(name, status, detail, last_ok, last_error, updated_at) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET status=excluded.status, detail=excluded.detail, "
        "last_ok=COALESCE(excluded.last_ok, integrations.last_ok), "
        "last_error=COALESCE(excluded.last_error, integrations.last_error), updated_at=excluded.updated_at",
        (name, status, detail[:500], t if ok else None, None if ok else t, t),
    )
