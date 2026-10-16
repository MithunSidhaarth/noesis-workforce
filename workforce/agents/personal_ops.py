"""Personal Operations: reminders, deadlines, personal tasks."""
from __future__ import annotations

from ..core.queue import Task
from ..core.settings import get_settings
from ..core.util import iso, new_id, now
from .base import Agent, AgentContext, PermanentError, register


@register("personal_ops")
class PersonalOpsAgent(Agent):
    accepts = {"create_reminder", "cancel_reminder"}

    def handlers(self):
        return {"create_reminder": self.create_reminder, "cancel_reminder": self.cancel_reminder,
                "send_due_reminders": self.send_due}

    def create_reminder(self, ctx: AgentContext, task: Task) -> dict:
        p = task.payload
        title, due = str(p.get("title", ""))[:200], p.get("due_at")
        if not title or not isinstance(due, (int, float)):
            raise PermanentError("reminder needs a title and a numeric due_at")
        key = p.get("dedupe_key") or f"{title}:{int(due)}"
        rid = new_id("rem")
        ctx.db.execute("INSERT OR IGNORE INTO reminders(id, title, due_at, source, ref_id, dedupe_key, created_at) VALUES (?,?,?,?,?,?,?)",
                       (rid, title, float(due), p.get("source") or task.requested_by, p.get("ref_id"), key, now()))
        r = ctx.db.one("SELECT id FROM reminders WHERE dedupe_key=?", (key,))
        return {"reminder_id": r["id"], "due": iso(due, get_settings().timezone)}

    def cancel_reminder(self, ctx: AgentContext, task: Task) -> dict:
        n = ctx.db.execute("UPDATE reminders SET status='cancelled' WHERE (id=? OR ref_id=?) AND status='pending'",
                           (task.payload.get("reminder_id"), task.payload.get("ref_id"))).rowcount
        return {"cancelled": n}

    def send_due(self, ctx: AgentContext, task: Task) -> dict:
        from .. import notifications as nt
        due = ctx.db.all("SELECT * FROM reminders WHERE status='pending' AND due_at<=? ORDER BY due_at LIMIT 20", (now(),))
        sent = 0
        for r in due:
            # a reminder you set yourself arrives at the time you chose, even in quiet hours;
            # automatic deadline reminders wait for quiet hours to end
            mine = r["source"] in ("owner", "telegram", "dashboard", "user")
            res = ctx.notify_owner(f"🔔 Reminder: {r['title']}", key=f"reminder:{r['id']}", urgent=True, force_now=mine)
            # only a real delivery (or a dry-run rehearsal of one) marks it sent; otherwise it stays
            # pending and the next sweep tries again
            if res is not None and res.status in ("succeeded", "dry_run", "duplicate"):
                ctx.db.execute("UPDATE reminders SET status='sent' WHERE id=?", (r["id"],))
                sent += 1
        # urgent items that were held (quiet hours, daily cap, or registered by a service
        # without Telegram access) go out as soon as they are allowed to
        flushed = 0
        for n in nt.held(ctx.db, urgent_only=True, limit=10):
            if not nt.should_send_now(ctx.db, True):
                break
            res = ctx.deliver(n["id"], n["key"], n["text"], n["buttons"])
            flushed += int(res is not None and res.status in ("succeeded", "dry_run", "duplicate"))
        return {"due": len(due), "sent": sent, "flushed": flushed}
