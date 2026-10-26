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
                "send_due_reminders": self.send_due, "calendar_sync": self.calendar_sync,
                "test_call": self.test_call}

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
            head = "🔔 Reminder" if mine or not r.get("ref_id") else "⏰ Deadline coming up"
            res = ctx.notify_owner(f"{head}\n\n{r['title']}", key=f"reminder:{r['id']}", urgent=True, force_now=mine)
            # only a real delivery (or a dry-run rehearsal of one) marks it sent; otherwise it stays
            # pending and the next sweep tries again
            if res is not None and res.status in ("succeeded", "dry_run", "duplicate"):
                ctx.db.execute("UPDATE reminders SET status='sent' WHERE id=?", (r["id"],))
                sent += 1
                self._maybe_call(ctx, r)
        # urgent items that were held (quiet hours, daily cap, or registered by a service
        # without Telegram access) go out as soon as they are allowed to
        flushed = 0
        for n in nt.held(ctx.db, urgent_only=True, limit=10):
            if not nt.should_send_now(ctx.db, True):
                break
            res = ctx.deliver(n["id"], n["key"], n["text"], n["buttons"], urgent=bool(n.get("urgent")))
            flushed += int(res is not None and res.status in ("succeeded", "dry_run", "duplicate"))
        return {"due": len(due), "sent": sent, "flushed": flushed}

    def calendar_sync(self, ctx: AgentContext, task: Task) -> dict:
        from ..calendar_sync import sync
        return sync(ctx)

    def test_call(self, ctx: AgentContext, task: Task) -> dict:
        from ..phone import call_owner
        out = call_owner(ctx, "This is a test call from NOESIS. If you can hear this, phone alerts work.", "test",
                         key=f"test:{task.id}")
        text = {"placed": "📞 Test call placed. Your phone should ring now.",
                "dry_run": "📞 Test mode: the call was rehearsed, not placed."}.get(
            out["status"], f"📞 No test call: {out['detail']}. See docs/PHONE.md.")
        ctx.notify_owner(text, key=f"calltest:{task.id}", force_now=True)
        return out

    @staticmethod
    def _maybe_call(ctx: AgentContext, r: dict) -> None:
        """An interview in an hour, or the last day for an application you haven't sent yet,
        also rings your phone (policies/phone.yaml decides; off by default)."""
        from ..phone import call_owner, reminder_call_reason
        reason = reminder_call_reason(r)
        if not reason or ctx.services.get("phone") is None:
            return
        if reason == "deadline_last_day":
            stage = ctx.db.scalar("SELECT stage FROM opportunities WHERE id=?", (r.get("ref_id"),))
            if stage not in ("shortlisted", "preparing"):
                return
        call_owner(ctx, "NOESIS reminder. " + r["title"].split(" http")[0], reason, key=f"rem:{r['id']}")
