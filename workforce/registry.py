"""Bot registry: config/bots/*.yaml -> validated BotConfig -> bots table.

Config files are the human-edited source of truth for a bot's identity, tools
and permissions. Runtime state (enabled/paused, heartbeat, metrics) lives in the
DB. Syncing never re-enables a bot a human disabled from the dashboard."""
from __future__ import annotations

from pathlib import Path

import yaml

from .contracts import BotConfig
from .core.db import Database, dumps, get_db, loads
from .core.settings import get_settings
from .core.state import audit
from .core.util import now, sha


def parse_every(s: str) -> float:
    unit = s[-1].lower()
    n = float(s[:-1])
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


class BotRegistry:
    def __init__(self, db: Database | None = None, config_dir: str | None = None):
        self.db = db or get_db()
        self.config_dir = Path(config_dir or get_settings().config_dir) / "bots"

    def load_files(self) -> list[BotConfig]:
        out = []
        for f in sorted(self.config_dir.glob("*.yaml")):
            data = yaml.safe_load(f.read_text(encoding="utf-8"))
            out.append(BotConfig.model_validate(data))
        ids = [b.id for b in out]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate bot ids in config/bots")
        return out

    def instructions_version(self, cfg: BotConfig) -> str:
        if not cfg.instructions_file:
            return ""
        p = Path(get_settings().prompts_dir) / cfg.instructions_file
        return sha(p.read_text(encoding="utf-8"))[:12] if p.exists() else "missing"

    def sync(self) -> list[str]:
        """Upsert every configured bot. Returns ids synced."""
        t = now()
        cfgs = self.load_files()
        for cfg in cfgs:
            cj = dumps(cfg.model_dump())
            ver = sha(cj)[:12]
            existing = self.db.one("SELECT id, enabled FROM bots WHERE id=?", (cfg.id,))
            if existing:
                self.db.execute(
                    "UPDATE bots SET name=?, purpose=?, config_json=?, config_version=?, instructions_version=?, updated_at=?"
                    + (", enabled=0" if not cfg.enabled else "") + " WHERE id=?",
                    (cfg.name, cfg.purpose, cj, ver, self.instructions_version(cfg), t, cfg.id))
            else:
                self.db.execute(
                    "INSERT INTO bots(id, name, purpose, config_json, config_version, instructions_version, enabled, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (cfg.id, cfg.name, cfg.purpose, cj, ver, self.instructions_version(cfg), int(cfg.enabled), t, t))
            self.db.execute("DELETE FROM subscriptions WHERE bot_id=?", (cfg.id,))
            for topic in cfg.subscribe_topics:
                self.db.execute("INSERT OR IGNORE INTO subscriptions(bot_id, topic) VALUES (?,?)", (cfg.id, topic))
            self._sync_schedules(cfg)
        return [c.id for c in cfgs]

    def _sync_schedules(self, cfg: BotConfig) -> None:
        keep = set()
        for s in cfg.schedules:
            sid = f"{cfg.id}:{s.name}"
            keep.add(sid)
            every = parse_every(s.every) if s.every else None
            row = self.db.one("SELECT id FROM schedules WHERE id=?", (sid,))
            if row:
                self.db.execute(
                    "UPDATE schedules SET kind=?, payload_json=?, every_s=?, at_time=?, weekdays=?, tz=?, allow_overlap=?, enabled=? WHERE id=?",
                    (s.kind, dumps(s.payload), every, s.at, s.weekdays, get_settings().timezone, int(s.allow_overlap), int(s.enabled), sid))
            else:
                self.db.execute(
                    "INSERT INTO schedules(id, bot_id, name, kind, payload_json, every_s, at_time, weekdays, tz, allow_overlap, enabled) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (sid, cfg.id, s.name, s.kind, dumps(s.payload), every, s.at, s.weekdays, get_settings().timezone,
                     int(s.allow_overlap), int(s.enabled)))
        for r in self.db.all("SELECT id FROM schedules WHERE bot_id=?", (cfg.id,)):
            if r["id"] not in keep:
                self.db.execute("DELETE FROM schedules WHERE id=?", (r["id"],))

    # ---- runtime lookups / human controls --------------------------------
    def get(self, bot_id: str) -> BotConfig | None:
        r = self.db.one("SELECT config_json FROM bots WHERE id=?", (bot_id,))
        return BotConfig.model_validate(loads(r["config_json"])) if r else None

    def all(self) -> list[dict]:
        return self.db.all("SELECT * FROM bots ORDER BY id")

    def set_enabled(self, bot_id: str, enabled: bool, by: str) -> None:
        self.db.execute("UPDATE bots SET enabled=?, state=?, updated_at=? WHERE id=?",
                        (int(enabled), "idle" if enabled else "offline", now(), bot_id))
        audit(self.db, by, "bot.enabled" if enabled else "bot.disabled", details={"bot_id": bot_id})

    def set_paused(self, bot_id: str, paused: bool, by: str) -> None:
        self.db.execute("UPDATE bots SET state=?, updated_at=? WHERE id=?", ("paused" if paused else "idle", now(), bot_id))
        audit(self.db, by, "bot.paused" if paused else "bot.resumed", details={"bot_id": bot_id})

    def heartbeat(self, bot_id: str, state: str | None = None, current_task: str | None = None) -> None:
        if state:
            self.db.execute(
                "UPDATE bots SET last_heartbeat=?, state=CASE WHEN state='paused' THEN state ELSE ? END, current_task_id=? WHERE id=?",
                (now(), state, current_task, bot_id))
        else:
            self.db.execute("UPDATE bots SET last_heartbeat=?, state=CASE WHEN state='offline' THEN 'idle' ELSE state END WHERE id=?",
                            (now(), bot_id))
