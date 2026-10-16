"""Scheduler: turns schedules into tasks and runs housekeeping.

* Each firing has an idempotency key (schedule id + slot), so two scheduler
  processes or a restart can never enqueue the same run twice.
* Without allow_overlap, a firing is skipped while the previous run is unfinished.
* Housekeeping reclaims expired task/message leases (crashed workers), flags
  stalled bots and sends deadline reminders via Personal Ops.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import os
from zoneinfo import ZoneInfo

from .contracts import TERMINAL_TASK_STATES, TaskSpec
from .core.db import Database, get_db, loads
from .core.queue import BotDisabled, TaskQueue
from .core.state import audit, exec_log
from .core.util import now
from .messaging.bus import MessageBus

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def next_run(s: dict, after: float) -> float:
    if s["every_s"]:
        base = s["next_run"] or after
        nxt = base + s["every_s"]
        while nxt <= after:
            nxt += s["every_s"]
        return nxt
    tz = ZoneInfo(s["tz"])
    hh, mm = (int(x) for x in s["at_time"].split(":"))
    days = [d.strip() for d in (s["weekdays"] or "").split(",") if d.strip()]
    d = datetime.fromtimestamp(after, tz)
    for i in range(0, 8):
        cand = (d + timedelta(days=i)).replace(hour=hh, minute=mm, second=0, microsecond=0)
        if cand.timestamp() <= after:
            continue
        if days and DAYS[cand.weekday()] not in days:
            continue
        return cand.timestamp()
    raise ValueError("no next run found")


class Scheduler:
    def __init__(self, db: Database | None = None):
        self.db = db or get_db()
        self.queue = TaskQueue(self.db)
        self.bus = MessageBus(self.db)
        self.started = now()

    def tick(self, t: float | None = None) -> list[str]:
        t = t or now()
        fired = []
        for s in self.db.all("SELECT s.* FROM schedules s JOIN bots b ON b.id=s.bot_id WHERE s.enabled=1 AND b.enabled=1"):
            if s["next_run"] is None:
                # first sight: interval schedules fire now, daily ones at their next slot
                first = t if s["every_s"] else next_run(s, t)
                self.db.execute("UPDATE schedules SET next_run=? WHERE id=?", (first, s["id"]))
                s["next_run"] = first
            if s["next_run"] > t:
                continue
            slot = int(s["next_run"])
            nxt = next_run(s, t)
            if not s["allow_overlap"] and s["last_task_id"]:
                st = self.db.scalar("SELECT status FROM tasks WHERE id=?", (s["last_task_id"],))
                if st and st not in TERMINAL_TASK_STATES:
                    skipped = (s.get("skipped") or 0) + 1
                    exec_log(self.db, "warning" if skipped >= SKIP_ALERT else "info",
                             f"schedule {s['id']} skipped: previous run still {st} ({skipped} in a row)", bot_id=s["bot_id"])
                    self.db.execute("UPDATE schedules SET next_run=?, skipped=? WHERE id=?", (nxt, skipped, s["id"]))
                    if skipped == SKIP_ALERT:
                        owner_alert(self.db, f"stuck:{s['id']}:{s['last_task_id']}",
                                    f"⚠️ A scheduled job is stuck\n\n{s['bot_id']} has skipped “{s['name']}” {skipped} times "
                                    f"because the previous run is still {st.replace('_', ' ')}. Check /inbox for a request "
                                    "waiting on you, or the dashboard Tasks page.", urgent=True)
                    continue
            try:
                tid, created = self.queue.enqueue(TaskSpec(
                    bot_id=s["bot_id"], kind=s["kind"], payload={**loads(s["payload_json"], {}), "_schedule": s["id"]},
                    requested_by="scheduler", idempotency_key=f"sched:{s['id']}:{slot}"))
            except BotDisabled:
                continue
            self.db.execute("UPDATE schedules SET next_run=?, last_run=?, last_task_id=?, skipped=0 WHERE id=?", (nxt, t, tid, s["id"]))
            if created:
                fired.append(tid)
        return fired

    def housekeeping(self) -> dict:
        out = {"tasks_reclaimed": self.queue.reclaim_expired(), "messages_reclaimed": self.bus.reclaim_expired(),
               "messages_expired": self.bus.expire_stale()}
        # bots with no heartbeat for 5 minutes are offline; tell the owner once per bot per day
        t = now()
        gone = self.db.all("SELECT id, name FROM bots WHERE enabled=1 AND state NOT IN ('paused','offline') AND "
                           "(last_heartbeat IS NULL OR last_heartbeat < ?)", (t - 300,))
        for b in gone:
            self.db.execute("UPDATE bots SET state='offline' WHERE id=?", (b["id"],))
            hb = self.db.scalar("SELECT last_heartbeat FROM bots WHERE id=?", (b["id"],))
            # no alert while the stack is starting up, or for a bot that was never alive
            if t - self.started < 600 or hb is None:
                continue
            owner_alert(self.db, f"offline:{b['id']}:{int(t // 86400)}",
                        f"⚠️ {b['name']} stopped responding\n\nNo heartbeat for 5 minutes. The worker restarts it "
                        "automatically. If this keeps happening, check the logs with: docker compose logs worker", urgent=True)
        out["bots_offline"] = len(gone)
        out["approvals_expired"] = self.expire_approvals()
        out["workflows_finished"] = self.finish_workflows()
        out["pruned"] = self.prune_if_due()
        return out

    def expire_approvals(self) -> int:
        """Approvals nobody answered: expire them so the waiting task and its schedule are not stuck forever."""
        ttl = float(os.environ.get("NOESIS_APPROVAL_TTL_H") or 72) * 3600
        rows = self.db.all("SELECT * FROM approvals WHERE status='pending' AND created_at < ?", (now() - ttl,))
        for a in rows:
            self.db.execute("UPDATE approvals SET status='expired', decided_by='system', decided_at=? WHERE id=? AND status='pending'",
                            (now(), a["id"]))
            if a["task_id"]:
                self.db.execute("UPDATE tasks SET status='cancelled', error='approval expired unanswered', finished_at=?, "
                                "updated_at=? WHERE id=? AND status='waiting_approval'", (now(), now(), a["task_id"]))
            audit(self.db, "system", "approval.expired", a["task_id"], {"approval_id": a["id"]})
            owner_alert(self.db, f"apprexp:{a['id']}", f"⌛ A request expired without an answer\n\n"
                                                      f"Nothing was done: {a['summary'][:150]}")
        return len(rows)

    def finish_workflows(self) -> int:
        """Close workflows whose tasks are all finished, and tell whoever asked."""
        done = 0
        for w in self.db.all("SELECT * FROM workflows WHERE status='running'"):
            sts = [r["status"] for r in self.db.all("SELECT status FROM tasks WHERE workflow_id=?", (w["id"],))]
            if not sts or any(s_ not in TERMINAL_TASK_STATES for s_ in sts):
                continue
            ok = all(s_ == "succeeded" for s_ in sts)
            self.db.execute("UPDATE workflows SET status=?, updated_at=? WHERE id=?", ("succeeded" if ok else "failed", now(), w["id"]))
            done += 1
            if w["requested_by"] in ("telegram", "dashboard", "user", "orchestrator"):
                owner_alert(self.db, f"wfdone:{w['id']}", workflow_report(self.db, w, ok),
                            urgent=w["requested_by"] in ("telegram", "dashboard", "user"))
        return done

    def prune_if_due(self) -> dict | None:
        last = float(self.db.scalar("SELECT value FROM system_state WHERE key='last_prune'") or 0)
        if now() - last < 86400:
            return None
        out = prune(self.db)
        self.db.execute("INSERT INTO system_state(key, value, updated_at, updated_by) VALUES ('last_prune', ?, ?, 'scheduler') "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at", (str(now()), now()))
        exec_log(self.db, "info", f"retention prune: {out}")
        return out


SKIP_ALERT = 3

# days to keep; audit_log and external_actions are the evidence trail and are kept
RETENTION_DAYS = {"exec_log": 30, "messages": 14, "tasks": 90, "notifications": 60, "model_usage": 400}


def prune(db: Database) -> dict:
    t = now()
    out = {}
    out["exec_log"] = db.execute("DELETE FROM exec_log WHERE ts < ?", (t - RETENTION_DAYS["exec_log"] * 86400,)).rowcount
    out["messages"] = db.execute("DELETE FROM messages WHERE status IN ('processed','expired','rejected','dead') AND created_at < ?",
                                 (t - RETENTION_DAYS["messages"] * 86400,)).rowcount
    cutoff = t - RETENTION_DAYS["tasks"] * 86400
    db.execute("DELETE FROM task_deps WHERE task_id IN (SELECT id FROM tasks WHERE status IN ('succeeded','dead','cancelled') "
               "AND finished_at < ?)", (cutoff,))
    out["tasks"] = db.execute("DELETE FROM tasks WHERE status IN ('succeeded','dead','cancelled') AND finished_at < ?", (cutoff,)).rowcount
    out["notifications"] = db.execute("DELETE FROM notifications WHERE status!='held' AND created_at < ?",
                                      (t - RETENTION_DAYS["notifications"] * 86400,)).rowcount
    out["model_usage"] = db.execute("DELETE FROM model_usage WHERE ts < ?", (t - RETENTION_DAYS["model_usage"] * 86400,)).rowcount
    try:
        import yaml
        from pathlib import Path
        from .core.settings import get_settings
        ttl = yaml.safe_load((Path(get_settings().config_dir) / "models.yaml").read_text()).get("cache_ttl_s", 0)
    except Exception:  # noqa: BLE001
        ttl = 86400
    out["model_cache"] = db.execute("DELETE FROM model_cache WHERE created_at < ?", (t - max(ttl, 3600),)).rowcount
    c = db.conn()
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.execute("PRAGMA optimize")
    return out


def owner_alert(db: Database, key: str, text: str, urgent: bool = False) -> None:
    """Services without Telegram access queue a notification; Personal Ops delivers urgent ones
    within minutes and the morning brief carries the rest."""
    from . import notifications as nt
    nt.register(db, "system", key, text, urgent, None)


def workflow_report(db: Database, w: dict, ok: bool) -> str:
    from .core.db import loads as _l
    name = w["name"].replace("_", " ")
    params = _l(w["spec_json"], {}).get("params", {})
    if w["name"] == "find_opportunities":
        new = db.scalar("SELECT COUNT(*) FROM opportunities WHERE discovered_at>=?", (w["created_at"],))
        good = db.scalar("SELECT COUNT(*) FROM opportunities WHERE discovered_at>=? AND COALESCE(relevance_score,0)>=0.7 "
                         "AND verification_status!='failed'", (w["created_at"],))
        return (f"✅ Search finished\n\n{new} new listing{'s' if new != 1 else ''}, {good} good "
                f"match{'es' if good != 1 else ''}. Send /opportunities to see the shortlist.")
    if w["name"] == "capture_knowledge":
        return (f"✅ Saved to your vault\n\n{params.get('url', '')}" if ok else
                f"✖ Couldn't save that link\n\n{params.get('url', '')}\nThe dashboard Logs page has the reason.")
    if w["name"] == "content_from_topic":
        return (f"✅ Content brief ready\n\nFor “{str(params.get('topic', ''))[:60]}”. Review it on the dashboard Content page."
                if ok else f"✖ Couldn't build a content brief\n\nFor “{str(params.get('topic', ''))[:60]}”. "
                "The dashboard Logs page has the reason.")
    return (f"✅ Done: {name}" if ok else f"✖ {name.capitalize()} failed\n\nThe dashboard Tasks page has the details.")
