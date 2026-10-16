"""Scheduler: turns schedules into tasks and runs housekeeping.

* Each firing has an idempotency key (schedule id + slot), so two scheduler
  processes or a restart can never enqueue the same run twice.
* Without allow_overlap, a firing is skipped while the previous run is unfinished.
* Housekeeping reclaims expired task/message leases (crashed workers), flags
  stalled bots and sends deadline reminders via Personal Ops.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .contracts import TERMINAL_TASK_STATES, TaskSpec
from .core.db import Database, get_db, loads
from .core.queue import BotDisabled, TaskQueue
from .core.state import exec_log
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
                    exec_log(self.db, "info", f"schedule {s['id']} skipped: previous run still {st}", bot_id=s["bot_id"])
                    self.db.execute("UPDATE schedules SET next_run=? WHERE id=?", (nxt, s["id"]))
                    continue
            try:
                tid, created = self.queue.enqueue(TaskSpec(
                    bot_id=s["bot_id"], kind=s["kind"], payload={**loads(s["payload_json"], {}), "_schedule": s["id"]},
                    requested_by="scheduler", idempotency_key=f"sched:{s['id']}:{slot}"))
            except BotDisabled:
                continue
            self.db.execute("UPDATE schedules SET next_run=?, last_run=?, last_task_id=? WHERE id=?", (nxt, t, tid, s["id"]))
            if created:
                fired.append(tid)
        return fired

    def housekeeping(self) -> dict:
        out = {"tasks_reclaimed": self.queue.reclaim_expired(), "messages_reclaimed": self.bus.reclaim_expired()}
        # bots with no heartbeat for 5 minutes are offline
        self.db.execute("UPDATE bots SET state='offline' WHERE enabled=1 AND state!='paused' AND "
                        "(last_heartbeat IS NULL OR last_heartbeat < ?)", (now() - 300,))
        return out
