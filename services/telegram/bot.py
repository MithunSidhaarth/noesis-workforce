"""Telegram command centre (long polling; no public webhook needed).

Only messages whose numeric sender id equals TELEGRAM_OWNER_ID, in a private
chat, are processed. Usernames are never trusted. Free text is turned into an
orchestrator `objective` task and goes through the same queue, policies and
approvals as everything else.
"""
from __future__ import annotations

import time

from workforce.contracts import TaskSpec
from workforce.core.db import Database, get_db
from workforce.core.queue import TaskQueue
from workforce.core.settings import get_settings
from workforce.core.state import SystemState, audit, configure_logging, set_integration_status
from workforce.core.util import iso, now
from workforce.policies.engine import IntegrationError, IntegrationRevoked, decide_approval

HELP = """NOESIS Workforce commands
/status — overall system status
/bots — bots and their state
/tasks — active and queued tasks
/inbox — approval requests
/approve <id> · /deny <id> — decide an approval
/opportunities — recent discoveries
/content — content queue and publication status
/pause — stop new external actions (emergency pause)
/resume — resume external actions
/help — this list
Anything else is treated as a request for the Chief of Staff."""


class CommandHandler:
    def __init__(self, db: Database | None = None, owner_id: int | None = None):
        self.db = db or get_db()
        self.owner_id = int(owner_id if owner_id is not None else get_settings().telegram_owner_id or 0)
        self.state = SystemState(self.db)
        self.queue = TaskQueue(self.db)

    def handle(self, update: dict) -> str | None:
        msg = update.get("message") or {}
        sender = (msg.get("from") or {}).get("id")
        chat = msg.get("chat") or {}
        if not self.owner_id or sender != self.owner_id or chat.get("type") != "private":
            audit(self.db, "telegram", "telegram.unauthorized", None,
                  {"from_id": sender, "chat_type": chat.get("type"), "text": (msg.get("text") or "")[:80]})
            return None
        text = (msg.get("text") or "").strip()
        if not text:
            return None
        if text.startswith("/"):
            cmd, _, arg = text.partition(" ")
            fn = getattr(self, "cmd_" + cmd[1:].split("@")[0].lower(), None)
            audit(self.db, "telegram:owner", "telegram.command", None, {"cmd": cmd})
            return fn(arg.strip()) if fn else "Unknown command. /help"
        tid, _ = self.queue.enqueue(TaskSpec(bot_id="orchestrator", kind="objective", payload={"text": text[:4000]},
                                             requested_by="telegram", idempotency_key=f"tg:{update.get('update_id')}"))
        return "Got it — passed to the Chief of Staff."

    def cmd_help(self, _): return HELP
    def cmd_start(self, _): return HELP

    def cmd_status(self, _):
        depth = self.queue.depth()
        bots = self.db.all("SELECT state, COUNT(*) n FROM bots WHERE enabled=1 GROUP BY state")
        appr = self.db.scalar("SELECT COUNT(*) FROM approvals WHERE status='pending'")
        dead = self.db.scalar("SELECT COUNT(*) FROM tasks WHERE status='dead' AND finished_at>?", (now() - 86400,))
        ints = self.db.all("SELECT name, status FROM integrations ORDER BY name")
        return "\n".join([
            f"{'⏸ PAUSED' if self.state.paused else '▶️ running'} · {'dry-run ON' if self.state.dry_run else 'live actions'}",
            "Bots: " + ", ".join(f"{b['state']} {b['n']}" for b in bots),
            "Queue: " + ", ".join(f"{k} {v}" for k, v in sorted(depth.items())),
            f"Pending approvals: {appr} · dead tasks (24h): {dead}",
            "Integrations: " + ", ".join(f"{i['name']}={i['status']}" for i in ints)])

    def cmd_bots(self, _):
        rows = self.db.all("SELECT id, name, enabled, state, last_heartbeat, tasks_succeeded, tasks_failed FROM bots ORDER BY id")
        return "\n".join(f"{'🟢' if r['enabled'] and r['state'] != 'offline' else '⚪'} {r['name']} — {r['state']}"
                         f" (✓{r['tasks_succeeded']} ✗{r['tasks_failed']})" for r in rows)

    def cmd_tasks(self, _):
        rows = self.db.all("SELECT bot_id, kind, status, attempts FROM tasks WHERE status IN ('running','queued','failed','deferred',"
                           "'waiting_approval','pending') ORDER BY priority, created_at LIMIT 20")
        return "\n".join(f"• {r['bot_id']}/{r['kind']} — {r['status']} (try {r['attempts']})" for r in rows) or "No active tasks."

    def cmd_inbox(self, _):
        rows = self.db.all("SELECT id, bot_id, summary, reasons_json FROM approvals WHERE status='pending' ORDER BY created_at LIMIT 10")
        return "\n\n".join(f"{r['id']}\n{r['bot_id']}: {r['summary']}\nwhy: {r['reasons_json']}" for r in rows) or "No pending approvals."

    def cmd_approve(self, arg):
        r = decide_approval(self.db, arg, True, by="telegram:owner")
        return f"Approved {arg}." if r else "No pending approval with that id."

    def cmd_deny(self, arg):
        r = decide_approval(self.db, arg, False, by="telegram:owner")
        return f"Denied {arg}." if r else "No pending approval with that id."

    def cmd_opportunities(self, _):
        tz = get_settings().timezone
        rows = self.db.all("SELECT title, type, deadline, relevance_score, verification_status, official_url FROM opportunities "
                           "ORDER BY discovered_at DESC LIMIT 8")
        return "\n\n".join(f"{r['title'][:80]}\n{r['type']} · {(iso(r['deadline'], tz) or 'deadline unknown')[:10]} · "
                           f"{r['relevance_score']} · {r['verification_status']}\n{r['official_url'] or ''}" for r in rows) or "None yet."

    def cmd_content(self, _):
        rows = self.db.all("SELECT title, platform, state FROM content_items ORDER BY updated_at DESC LIMIT 10")
        return "\n".join(f"• [{r['platform']}] {r['title'][:60]} — {r['state']}" for r in rows) or "Content queue is empty."

    def cmd_pause(self, _):
        self.state.set(SystemState.PAUSED, True, by="telegram:owner")
        return "⏸ Paused. No new external actions will run; queued work is preserved."

    def cmd_resume(self, _):
        self.state.set(SystemState.PAUSED, False, by="telegram:owner")
        return "▶️ Resumed. Actions still pass policy checks" + (" (dry-run is ON)." if self.state.dry_run else ".")


def main() -> None:  # pragma: no cover - network loop
    from integrations.telegram.client import TelegramClient
    from workforce.factory import bootstrap
    configure_logging()
    db = bootstrap()
    s = get_settings()
    try:
        tg = TelegramClient(s.telegram_token, s.telegram_owner_id)
    except IntegrationRevoked as e:
        set_integration_status(db, "telegram", "not_configured", str(e))
        print(f"telegram disabled: {e}")
        while True:
            time.sleep(3600)
    handler = CommandHandler(db)
    offset = SystemState(db).get("telegram_offset")
    while True:
        try:
            for u in tg.get_updates(offset):
                offset = u["update_id"] + 1
                SystemState(db).set("telegram_offset", offset, by="telegram")
                reply = handler.handle(u)
                if reply:
                    tg.send(u["message"]["chat"]["id"], reply)
            set_integration_status(db, "telegram", "ok", "polling")
        except IntegrationRevoked as e:
            set_integration_status(db, "telegram", "revoked", str(e))
            time.sleep(300)
        except IntegrationError as e:
            set_integration_status(db, "telegram", "degraded", str(e))
            time.sleep(10)


if __name__ == "__main__":
    main()
