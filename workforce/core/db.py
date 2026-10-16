"""SQLite access layer with WAL, migrations and small JSON helpers.

SQLite in WAL mode is the default store: it is free, needs no extra container,
survives restarts, and comfortably handles a single-user workforce with several
worker processes (many readers, one writer at a time, busy timeout for waits).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .settings import ROOT, get_settings

MIGRATIONS_DIR = ROOT / "database" / "migrations"


class Database:
    def __init__(self, path: str | None = None):
        self.path = path or get_settings().db_path
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA busy_timeout=30000")
            self._local.conn = c
        return c

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Write transaction. BEGIN IMMEDIATE takes the write lock up front so
        claim-style read-then-update sequences are atomic across processes."""
        c = self.conn()
        for i in range(20):
            try:
                c.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as e:  # pragma: no cover - contention path
                if "locked" not in str(e) or i == 19:
                    raise
                time.sleep(0.05 * (i + 1))
        try:
            yield c
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        return self.conn().execute(sql, params)

    def one(self, sql: str, params: tuple | dict = ()) -> dict | None:
        r = self.conn().execute(sql, params).fetchone()
        return dict(r) if r else None

    def all(self, sql: str, params: tuple | dict = ()) -> list[dict]:
        return [dict(r) for r in self.conn().execute(sql, params).fetchall()]

    def scalar(self, sql: str, params: tuple | dict = ()) -> Any:
        r = self.conn().execute(sql, params).fetchone()
        return r[0] if r else None

    # ---- migrations -------------------------------------------------------
    def migrate(self) -> list[str]:
        c = self.conn()
        c.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at REAL NOT NULL)")
        applied = {r[0] for r in c.execute("SELECT name FROM schema_migrations")}
        done = []
        for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if f.name in applied:
                continue
            sql = f.read_text(encoding="utf-8")
            c.execute("BEGIN IMMEDIATE")
            try:
                for stmt in _split_sql(sql):
                    c.execute(stmt)
                c.execute("INSERT INTO schema_migrations(name, applied_at) VALUES (?, ?)", (f.name, time.time()))
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
            done.append(f.name)
        return done


def _split_sql(sql: str) -> list[str]:
    out, buf = [], ""
    for ln in sql.splitlines():
        ln = ln.split("--", 1)[0].rstrip()
        if not ln:
            continue
        buf += ln + "\n"
        if sqlite3.complete_statement(buf):
            out.append(buf.strip())
            buf = ""
    if buf.strip():
        raise ValueError(f"incomplete SQL statement in migration: {buf[:80]}")
    return out


def dumps(v: Any) -> str:
    return json.dumps(v, default=str, ensure_ascii=False, sort_keys=True)


def loads(v: str | None, default: Any = None) -> Any:
    if v is None or v == "":
        return default
    return json.loads(v)


_db: Database | None = None


def get_db() -> Database:
    global _db
    if _db is None:
        _db = Database()
    return _db


def set_db(db: Database | None) -> None:
    global _db
    _db = db
