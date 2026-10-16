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
from workforce.core.db import loads
from workforce.core.state import SystemState, audit, configure_logging, exec_log, set_integration_status
from workforce.core.util import now
from workforce.owner_actions import (OwnerActionError, closes_in, confirm_opportunity, create_reminder, decide,
                                     find_opportunity, local, missing_summary, opportunity_card, pending_approvals,
                                     plain_status, record_outcome, record_submission, remind_opportunity, set_profile_fact,
                                     shortlist, skip_opportunity)
from workforce.policies.engine import IntegrationError, IntegrationRevoked

HELP = """NOESIS Workforce

Opportunities
/opportunities: your shortlist (open, by deadline) · /opportunities all
/opp <n|ref>: full card with buttons
/confirm <n|ref> [official link]: it's real, prepare an application
/skip <n|ref> [reason]: not for me
/submitted <ref> <confirmation>: you submitted it yourself
/outcome <ref> accepted|rejected|no_response
/missing: what applications are waiting on
/profile · /profile set <key> <value>

Everyday
/inbox: approvals (numbered) · /approve <n> · /deny <n>
/remind tomorrow 09:00 call the bank (also: in 2h …, fri 18:00 …, 2026-11-03 10:00 …)
/status · /bots · /tasks · /content
/pause: stop all external actions · /resume

Anything else goes to the Chief of Staff."""

class CommandHandler:
    def __init__(self, db: Database | None = None, owner_id: int | None = None):
        self.db = db or get_db()
        self.owner_id = int(owner_id if owner_id is not None else get_settings().telegram_owner_id or 0)
        self.state = SystemState(self.db)
        self.queue = TaskQueue(self.db)

    def _authorized(self, sender, chat: dict, text: str) -> bool:
        if not self.owner_id or sender != self.owner_id or chat.get("type") != "private":
            audit(self.db, "telegram", "telegram.unauthorized", None,
                  {"from_id": sender, "chat_type": chat.get("type"), "text": (text or "")[:80]})
            return False
        return True

    def handle(self, update: dict):
        """Returns a reply: str, {"text", "buttons"}, a callback result dict, or None."""
        if "callback_query" in update:
            return self.handle_callback(update["callback_query"])
        msg = update.get("message") or {}
        sender = (msg.get("from") or {}).get("id")
        chat = msg.get("chat") or {}
        if not self._authorized(sender, chat, msg.get("text") or ""):
            return None
        text = (msg.get("text") or "").strip()
        if not text:
            return None
        if text.startswith("/"):
            cmd, _, arg = text.partition(" ")
            fn = getattr(self, "cmd_" + cmd[1:].split("@")[0].lower(), None)
            audit(self.db, "telegram:owner", "telegram.command", None, {"cmd": cmd})
            if not fn:
                return "Unknown command. /help"
            try:
                return fn(arg.strip())
            except OwnerActionError as e:
                return str(e)
        tid, _ = self.queue.enqueue(TaskSpec(bot_id="orchestrator", kind="objective", payload={"text": text[:4000]},
                                             requested_by="telegram", idempotency_key=f"tg:{update.get('update_id')}"))
        return "Got it. I'll message you when it's done."

    def handle_callback(self, cq: dict) -> dict | None:
        """Inline buttons. Same owner + private-chat rule as typed commands."""
        sender = (cq.get("from") or {}).get("id")
        message = cq.get("message") or {}
        chat = message.get("chat") or {}
        data = str(cq.get("data") or "")
        if not self._authorized(sender, chat, data):
            return {"callback_id": cq.get("id"), "answer": "Not allowed."}
        audit(self.db, "telegram:owner", "telegram.button", None, {"data": data[:64]})
        kind, _, rest = data.partition(":")
        oid, _, verb = rest.rpartition(":")
        try:
            if kind == "a" and verb in ("y", "n"):
                out = decide(self.db, oid, verb == "y", by="telegram:owner")
                note = "✅ Approved. It will run now." if verb == "y" else "✖ Denied. Nothing will be sent."
                return self._cb(cq, note, f"{message.get('text', '')}\n\n{note}" if out else None)
            if kind == "o" and verb == "p":
                r = confirm_opportunity(self.db, oid, by="telegram:owner")
                note = "✅ Preparing the application. I'll tell you what's missing." if r["queued"] else "Confirmed (Applications bot is disabled)."
                return self._cb(cq, note, f"{message.get('text', '')}\n\n{note}")
            if kind == "o" and verb == "r":
                opp, n = remind_opportunity(self.db, oid, by="telegram:owner")
                note = f"⏰ I'll remind you {n} time(s) before it closes." if n else "⏰ Reminders were already set."
                return self._cb(cq, note, f"{message.get('text', '')}\n\n{note}")
            if kind == "o" and verb == "s":
                skip_opportunity(self.db, oid, by="telegram:owner", reason="not for me (button)")
                note = "✖ Skipped. Tell me why with /skip <ref> <reason> and I'll learn from it."
                return self._cb(cq, note, f"{message.get('text', '')}\n\n{note}")
        except OwnerActionError as e:
            return self._cb(cq, str(e)[:190], None)
        return self._cb(cq, "That button has expired.", None)

    @staticmethod
    def _cb(cq: dict, answer: str, edit: str | None) -> dict:
        m = cq.get("message") or {}
        return {"callback_id": cq.get("id"), "answer": answer, "edit": edit,
                "chat_id": (m.get("chat") or {}).get("id"), "message_id": m.get("message_id")}

    def cmd_help(self, _): return HELP
    def cmd_start(self, _): return HELP

    def _banner(self) -> list[str]:
        out = []
        if self.state.dry_run:
            out.append("🧪 Dry-run is ON: nothing is sent, published or submitted for real.")
        if self.state.paused:
            out.append("⏸ Emergency pause is ON: external actions are on hold.")
        return out

    def cmd_status(self, _):
        depth = self.queue.depth()
        bots = self.db.all("SELECT state, COUNT(*) n FROM bots WHERE enabled=1 GROUP BY state")
        appr = self.db.scalar("SELECT COUNT(*) FROM approvals WHERE status='pending'")
        dead = self.db.scalar("SELECT COUNT(*) FROM tasks WHERE status='dead' AND finished_at>?", (now() - 86400,))
        ints = self.db.all("SELECT name, status FROM integrations ORDER BY name")
        return "\n".join(self._banner() + [
            f"{'⏸ paused' if self.state.paused else '▶️ running'} · {'dry-run' if self.state.dry_run else 'live actions'}",
            "Bots: " + ", ".join(f"{b['state']} {b['n']}" for b in bots),
            "Queue: " + ", ".join(f"{k} {v}" for k, v in sorted(depth.items())),
            f"Waiting for you: {appr} approval(s) · failed tasks (24h): {dead}",
            "Integrations: " + ", ".join(f"{i['name']}={i['status']}" for i in ints)])

    def cmd_bots(self, _):
        rows = self.db.all("SELECT id, name, enabled, state, last_heartbeat, tasks_succeeded, tasks_failed FROM bots ORDER BY id")
        return "\n".join(f"{'🟢' if r['enabled'] and r['state'] != 'offline' else '⚪'} {r['name']}: {r['state']}"
                         f" (✓{r['tasks_succeeded']} ✗{r['tasks_failed']})" for r in rows)

    def cmd_tasks(self, _):
        rows = self.db.all("SELECT bot_id, kind, status, attempts FROM tasks WHERE status IN ('running','queued','failed','deferred',"
                           "'waiting_approval','pending') ORDER BY priority, created_at LIMIT 20")
        return "\n".join(f"• {r['bot_id']}/{r['kind']}: {r['status']} (try {r['attempts']})" for r in rows) or "No active tasks."

    def cmd_inbox(self, _):
        rows = pending_approvals(self.db, 10)
        if not rows:
            return "Nothing waiting for your approval."
        out = []
        for i, r in enumerate(rows, 1):
            reasons = "; ".join(loads(r["reasons_json"], [])[:3])
            out.append(f"{i}. {r['summary'][:120]}\n   why: {reasons}")
        return "\n\n".join(out) + "\n\n/approve <n> or /deny <n>"

    def cmd_approve(self, arg):
        a = decide(self.db, arg, True, by="telegram:owner")
        return f"✅ Approved: {a['summary'][:100]}"

    def cmd_deny(self, arg):
        a = decide(self.db, arg, False, by="telegram:owner")
        return f"✖ Denied: {a['summary'][:100]}"

    def cmd_opportunities(self, arg):
        rows = shortlist(self.db, include_all=arg.strip().lower() == "all")
        if not rows:
            return "Nothing open right now." + ("" if arg else " /opportunities all shows weaker matches too.")
        out = [f"{i}. {r['title'][:70]}\n   {closes_in(r['deadline'])} · {plain_status(r)}" for i, r in enumerate(rows, 1)]
        return "\n".join(out) + "\n\n/opp <n> for details · /confirm <n> · /skip <n>"

    def cmd_opp(self, arg):
        text, buttons = opportunity_card(find_opportunity(self.db, arg))
        return {"text": text, "buttons": buttons}

    def cmd_confirm(self, arg):
        token, _, url = arg.partition(" ")
        r = confirm_opportunity(self.db, token, by="telegram:owner", url=url or None)
        return (f"✅ Confirmed “{r['opportunity']['title'][:70]}”. Preparing the application now; "
                "I'll tell you what's missing.") if r["queued"] else "Confirmed, but the Applications bot is disabled."

    cmd_apply = cmd_confirm

    def cmd_skip(self, arg):
        token, _, reason = arg.partition(" ")
        o = skip_opportunity(self.db, token, by="telegram:owner", reason=reason)
        return f"✖ Skipped “{o['title'][:70]}” ({o['skip_reason']}). Reminders cancelled."

    def cmd_outcome(self, arg):
        token, _, outcome = arg.partition(" ")
        a = record_outcome(self.db, token, outcome or "", by="telegram:owner")
        return f"Recorded: {a['outcome'].replace('_', ' ')}. " + ("🎉 Congratulations!" if a["outcome"] == "accepted" else "Thanks, noted for the weekly review.")

    def cmd_submitted(self, arg):
        token, _, conf = arg.partition(" ")
        record_submission(self.db, token, conf, by="telegram:owner")
        return "📨 Recorded as submitted. I'll ask how it went in two weeks."

    def cmd_remind(self, arg):
        r = create_reminder(self.db, arg, by="telegram:owner")
        return f"⏰ OK. {local(r['due_at'])}: {r['title']}"

    def cmd_missing(self, _):
        rows = missing_summary(self.db)
        if not rows:
            return "No application is waiting on information from you."
        return "Applications are waiting on:\n" + "\n".join(f"• {item} ({n}): {how}" for item, n, how in rows[:12])

    def cmd_profile(self, arg):
        from workforce.sources.registry import load_profile, profile_problems
        parts = arg.split(" ", 2)
        if parts and parts[0].lower() == "set":
            if len(parts) < 3:
                return "Usage: /profile set <key> <value>, e.g. /profile set graduation_year 2027"
            r = set_profile_fact(self.db, parts[1], parts[2], by="telegram:owner")
            extra = f" Re-preparing {r['refreshed']} application(s) with it." if r["refreshed"] else ""
            return f"Saved {r['key']} ({r['section']}{', sensitive' if r['sensitive'] else ''}).{extra}"
        p = load_profile()
        lines = ["Verified facts:"]
        for sec in ("facts", "answers"):
            for k, v in (p.get(sec) or {}).items():
                if isinstance(v, dict) and v.get("verified") and v.get("value") not in (None, ""):
                    val = "•••" if v.get("sensitive") else str(v["value"])[:60]
                    lines.append(f"• {k}: {val}")
        if len(lines) == 1:
            lines.append("none yet")
        probs = profile_problems(p)
        if probs:
            lines += ["", "Gaps:"] + [f"• {x}" for x in probs]
        return "\n".join(lines + ["", "Add or change: /profile set <key> <value>"])

    def cmd_content(self, _):
        rows = self.db.all("SELECT title, platform, state FROM content_items ORDER BY updated_at DESC LIMIT 10")
        return "\n".join(f"• [{r['platform']}] {r['title'][:60]}: {r['state']}" for r in rows) or "Content queue is empty."

    def cmd_pause(self, _):
        self.state.set(SystemState.PAUSED, True, by="telegram:owner")
        return "⏸ Paused. Sends, posts and submissions wait until /resume; you'll still get alerts."

    def cmd_resume(self, _):
        self.state.set(SystemState.PAUSED, False, by="telegram:owner")
        return "▶️ Resumed. Held work continues within a few minutes" + (" (dry-run is ON)." if self.state.dry_run else ".")


def _heartbeat(db: Database) -> None:
    db.execute("INSERT INTO system_state(key, value, updated_at, updated_by) VALUES ('telegram_heartbeat', ?, ?, 'telegram') "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at", (str(time.time()), time.time()))


def deliver_reply(tg, update: dict, reply) -> None:
    if reply is None:
        return
    if isinstance(reply, dict) and "callback_id" in reply:
        if reply.get("callback_id"):
            tg.answer_callback(reply["callback_id"], reply.get("answer", ""))
        if reply.get("edit") and reply.get("chat_id") and reply.get("message_id"):
            tg.edit_message(reply["chat_id"], reply["message_id"], reply["edit"])
        return
    chat_id = update["message"]["chat"]["id"]
    if isinstance(reply, dict):
        tg.send(chat_id, reply["text"], reply.get("buttons"))
    else:
        tg.send(chat_id, reply)


def main() -> None:  # pragma: no cover - network loop
    from integrations.telegram.client import TelegramClient
    from workforce.factory import bootstrap
    configure_logging()
    db = bootstrap()
    s = get_settings()
    try:
        tg = TelegramClient(s.telegram_token, s.telegram_owner_id)
    except (IntegrationRevoked, ValueError) as e:
        msg = "TELEGRAM_OWNER_ID must be your numeric Telegram user id" if isinstance(e, ValueError) else str(e)
        set_integration_status(db, "telegram", "not_configured" if isinstance(e, IntegrationRevoked) else "error", msg)
        print(f"telegram disabled: {e}")
        while True:   # stay up (and healthy) so the dashboard can show why
            _heartbeat(db)
            time.sleep(60)
    handler = CommandHandler(db)
    offset = SystemState(db).get("telegram_offset")
    backoff = 5
    while True:
        try:
            _heartbeat(db)
            for u in tg.get_updates(offset):
                # handle first, then advance the offset: a crash mid-handle redelivers the
                # update, and the objective enqueue is idempotent on tg:<update_id>
                deliver_reply(tg, u, handler.handle(u))
                offset = u["update_id"] + 1
                # bookkeeping, not a policy change: write directly so the audit log stays meaningful
                db.execute("INSERT INTO system_state(key, value, updated_at, updated_by) VALUES ('telegram_offset', ?, ?, 'telegram') "
                           "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at", (str(offset), time.time()))
            set_integration_status(db, "telegram", "ok", "polling")
            backoff = 5
        except IntegrationRevoked as e:
            set_integration_status(db, "telegram", "revoked", str(e))
            time.sleep(300)
        except IntegrationError as e:
            set_integration_status(db, "telegram", "degraded", str(e))
            time.sleep(10)
        except Exception as e:  # noqa: BLE001  (database locked, a bug in one command: keep the bot alive)
            exec_log(db, "error", f"telegram loop error: {type(e).__name__}: {e}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)


if __name__ == "__main__":
    main()
