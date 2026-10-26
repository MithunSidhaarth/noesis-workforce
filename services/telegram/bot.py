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
from workforce.owner_actions import (OwnerActionError, clip, closes_in, confirm_opportunity, create_reminder, decide,
                                     find_opportunity, log_work, missing_label, missing_summary, opportunity_card, pending_approvals,
                                     plain_status, plural, record_outcome, record_submission, ref, remind_opportunity,
                                     set_profile_fact, request_ideas, shortlist, skip_opportunity, when_label)
from workforce.policies.engine import IntegrationError, IntegrationRevoked

HELP = """Here is what I can do. Where a command takes <n>, use the number from the last list or the 6-character ref.

Today
/inbox – requests waiting for your OK
/approve <n> · /deny <n>
/remind tomorrow 09:00 call the bank (also: in 2h …, fri 18:00 …, 2026-11-03 10:00 …)

Internships
/today – today's best matches to apply to, and your streak
/opportunities – open matches, soonest deadline first (/opportunities all for weaker ones)
/opp <n> – full details with buttons
/add <link> [description] – a posting you found (Internshala, Unstop, LinkedIn: paste the text)
/confirm <n> [official link] – it's real, prepare an application
/skip <n> [reason] – not for me
/tailor <n> – resume keyword score and tailored bullets for it
/drafts <ref> – copy-ready cover letter, bullets and answers

Pipeline
/pipeline – where every application stands
/stage <ref> <stage> [YYYY-MM-DD HH:MM] – e.g. /stage 3f9a1c oa 2026-11-04 15:00

Applications
/missing – what your applications need from you
/profile – your verified facts · /profile set graduation_year 2027
/submitted <ref> <confirmation> – you submitted it yourself
/outcome <ref> accepted | rejected | no_response

Content
/work <what you did> – turn it into posts (/work <brand> … for one brand)
/content – the latest posts and where they stand
/ideas – trending project ideas this week

System
/status – health, test mode and the queue
/bots · /tasks – what each bot is doing
/pause – stop all external actions · /resume

Anything else you type goes to the Chief of Staff as a request."""


ACTIVE = ("running", "queued", "deferred", "waiting_approval", "failed")


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
                return f"I don't know the command {cmd[:40]}. Send /help to see the ones I do."
            try:
                return fn(arg.strip())
            except OwnerActionError as e:
                return str(e)
        tid, _ = self.queue.enqueue(TaskSpec(bot_id="orchestrator", kind="objective", payload={"text": text[:4000]},
                                             requested_by="telegram", idempotency_key=f"tg:{update.get('update_id')}"))
        return "Got it. I'm on it and will message you when it's done."

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
                note = (f"Approved by you. It goes ahead now{self._test_note()}." if verb == "y"
                        else "Denied by you. Nothing was done.")
                return self._cb(cq, note, f"{message.get('text', '')}\n\n{note}" if out else None)
            if kind == "o" and verb == "p":
                r = confirm_opportunity(self.db, oid, by="telegram:owner")
                note = ("Confirmed. I'm preparing the application and will tell you what it still needs." if r["queued"]
                        else "Confirmed, but the Applications bot is turned off, so nothing will be prepared until you enable it.")
                return self._cb(cq, note, f"{message.get('text', '')}\n\n{note}")
            if kind == "o" and verb == "r":
                opp, n = remind_opportunity(self.db, oid, by="telegram:owner")
                note = (f"Reminders set: I'll message you {n} time{'s' if n != 1 else ''} before it closes." if n
                        else "Reminders were already set for this one.")
                return self._cb(cq, note, f"{message.get('text', '')}\n\n{note}")
            if kind == "g" and verb in ("y", "n"):
                from workforce.pipeline import LABELS, StageError, decide_proposal
                try:
                    p = decide_proposal(self.db, oid, verb == "y", by="telegram:owner")
                except StageError as e:
                    return self._cb(cq, str(e)[:190], None)
                note = f"Moved to {LABELS[p['stage']]}." if verb == "y" else "OK, left as it was."
                return self._cb(cq, note, f"{message.get('text', '')}\n\n{note}")
            if kind == "st" and verb in ("ghosted", "wait"):
                from workforce.pipeline import set_stage
                if verb == "ghosted":
                    set_stage(self.db, oid, "ghosted", by="telegram:owner", note="no reply (button)")
                    note = "Marked as no reply. It counts in your weekly insights."
                else:
                    self.db.execute("UPDATE opportunities SET stage_updated_at=? WHERE id=? AND stage='applied'", (now(), oid))
                    note = "OK, I'll ask again in two weeks."
                return self._cb(cq, note, f"{message.get('text', '')}\n\n{note}")
            if kind == "o" and verb == "s":
                skip_opportunity(self.db, oid, by="telegram:owner", reason="not for me (button)")
                note = f"Skipped, and its reminders are cancelled. To tell me why, send /skip {ref(oid)} <reason>."
                return self._cb(cq, note, f"{message.get('text', '')}\n\n{note}")
        except OwnerActionError as e:
            return self._cb(cq, str(e)[:190], None)
        return self._cb(cq, "This button no longer works. Send /inbox or /opportunities for a fresh list.", None)

    @staticmethod
    def _cb(cq: dict, answer: str, edit: str | None) -> dict:
        m = cq.get("message") or {}
        return {"callback_id": cq.get("id"), "answer": answer, "edit": edit,
                "chat_id": (m.get("chat") or {}).get("id"), "message_id": m.get("message_id")}

    def cmd_help(self, _): return HELP

    def cmd_start(self, _):
        return "\n".join(["Hi. NOESIS is running and listening to you here."] + self._banner() + ["", HELP])

    def _banner(self) -> list[str]:
        out = []
        if self.state.dry_run:
            out.append("Test mode is on: nothing is sent, posted or submitted for real.")
        if self.state.paused:
            out.append("Paused: external actions are on hold until you send /resume.")
        return out

    def _test_note(self) -> str:
        return " (test mode: it is only rehearsed, nothing goes out)" if self.state.dry_run else ""

    def cmd_status(self, _):
        depth = self.queue.depth()
        bots = self.db.all("SELECT state, COUNT(*) n FROM bots WHERE enabled=1 GROUP BY state")
        appr = self.db.scalar("SELECT COUNT(*) FROM approvals WHERE status='pending'")
        dead = self.db.scalar("SELECT COUNT(*) FROM tasks WHERE status='dead' AND finished_at>?", (now() - 86400,))
        ints = self.db.all("SELECT name, status FROM integrations ORDER BY name")
        working = [i["name"] for i in ints if i["status"] == "ok"]
        unset = [i["name"] for i in ints if i["status"] == "not_configured"]
        broken = [f"{i['name']} ({i['status'].replace('_', ' ')})" for i in ints if i["status"] not in ("ok", "not_configured")]
        mode = "Paused" if self.state.paused else "Running"
        mode += ", in test mode" if self.state.dry_run else ", live: actions are real"
        lines = [mode + ".",
                 "Bots: " + (", ".join(f"{b['n']} {b['state']}" for b in bots) or "none enabled"),
                 "Queue: " + (", ".join(f"{depth[k]} {k.replace('_', ' ')}" for k in ACTIVE if depth.get(k)) or "empty"),
                 f"Waiting for you: {plural(appr, 'approval')}" + (" (/inbox)" if appr else ""),
                 f"Failed in the last 24 hours: {plural(dead, 'task')}"]
        if working:
            lines.append("Working: " + ", ".join(working))
        if broken:
            lines.append("Needs attention: " + ", ".join(broken))
        if unset:
            lines.append("Not set up: " + ", ".join(unset))
        return "\n".join(self._banner() + [""] * bool(self._banner()) + lines)

    def cmd_bots(self, _):
        rows = self.db.all("SELECT id, name, enabled, state, last_heartbeat, tasks_succeeded, tasks_failed FROM bots ORDER BY id")
        return "\n".join(f"{'🟢' if r['enabled'] and r['state'] != 'offline' else '⚪'} {r['name']}: "
                         f"{r['state'] if r['enabled'] else 'turned off'}, {r['tasks_succeeded']} done, {r['tasks_failed']} failed"
                         for r in rows) or "No bots are registered yet."

    def cmd_tasks(self, _):
        rows = self.db.all("SELECT bot_id, kind, status, attempts FROM tasks WHERE status IN ('running','queued','failed','deferred',"
                           "'waiting_approval','pending') ORDER BY priority, created_at LIMIT 20")
        return "\n".join(f"• {r['bot_id']}: {r['kind'].replace('_', ' ')}, {r['status'].replace('_', ' ')}"
                         + (f" (attempt {r['attempts']})" if (r["attempts"] or 0) > 1 else "") for r in rows) \
            or "Nothing is running or queued right now."

    def cmd_inbox(self, _):
        rows = pending_approvals(self.db, 10)
        if not rows:
            return "Nothing is waiting for your approval."
        out = []
        for i, r in enumerate(rows, 1):
            reasons = "; ".join(loads(r["reasons_json"], [])[:3])
            out.append(f"{i}. {clip(r['summary'], 120)}" + (f"\n   Why: {reasons}" if reasons else ""))
        return "\n\n".join(out) + "\n\nReply /approve <n> or /deny <n>, e.g. /approve 1."

    def cmd_approve(self, arg):
        a = decide(self.db, arg, True, by="telegram:owner")
        return f"Approved: {clip(a['summary'], 100)}\nIt goes ahead now{self._test_note()}."

    def cmd_deny(self, arg):
        a = decide(self.db, arg, False, by="telegram:owner")
        return f"Denied: {clip(a['summary'], 100)}\nNothing was done."

    def cmd_opportunities(self, arg):
        show_all = arg.strip().lower() == "all"
        rows = shortlist(self.db, include_all=show_all)
        if not rows:
            return "No open opportunities right now." + ("" if show_all else " Send /opportunities all to include weaker matches.")
        out = [f"{i}. {clip(r['title'], 70)}\n   {closes_in(r['deadline'])}. {plain_status(r)}." for i, r in enumerate(rows, 1)]
        return "\n".join(out) + "\n\nSend /opp <n> for details, /confirm <n> to prepare it, or /skip <n>."

    def cmd_opp(self, arg):
        text, buttons = opportunity_card(find_opportunity(self.db, arg))
        return {"text": text, "buttons": buttons}

    def cmd_confirm(self, arg):
        token, _, url = arg.partition(" ")
        r = confirm_opportunity(self.db, token, by="telegram:owner", url=url or None)
        title = clip(r["opportunity"]["title"], 70)
        return (f"Confirmed “{title}”. I'm preparing the application and will tell you what it still needs."
                if r["queued"] else
                f"Confirmed “{title}”, but the Applications bot is turned off, so nothing will be prepared until you enable it.")

    cmd_apply = cmd_confirm

    def cmd_skip(self, arg):
        token, _, reason = arg.partition(" ")
        o = skip_opportunity(self.db, token, by="telegram:owner", reason=reason)
        return f"Skipped “{clip(o['title'], 70)}” ({o['skip_reason']}). Its reminders are cancelled."

    def cmd_outcome(self, arg):
        token, _, outcome = arg.partition(" ")
        a = record_outcome(self.db, token, outcome or "", by="telegram:owner")
        if a["outcome"] == "accepted":
            return "Recorded: accepted. Congratulations, well earned."
        return f"Recorded: {a['outcome'].replace('_', ' ')}. It will show up in the weekly review."

    def cmd_submitted(self, arg):
        token, _, conf = arg.partition(" ")
        record_submission(self.db, token, conf, by="telegram:owner")
        return "Recorded as submitted, and its deadline reminders are cancelled. I'll ask how it went in two weeks."

    def cmd_add(self, arg):
        from workforce.owner_actions import add_link
        r = add_link(self.db, arg, by="telegram:owner")
        return ("Got it. I'm reading the posting now and will add it to your shortlist." if r["queued"]
                else "I'm already on that link.")

    def cmd_stage(self, arg):
        from workforce.owner_actions import change_stage
        r = change_stage(self.db, arg, by="telegram:owner")
        when = f" on {when_label(r['event_at'])}. I've set reminders and a prep plan" if r["event_at"] else ""
        return f"“{clip(r['opportunity']['title'], 70)}” is now at {r['label']}{when}."

    def cmd_pipeline(self, _):
        from workforce.pipeline import ACTIVE, LABELS, board
        cols = board(self.db)
        lines = []
        for st in ACTIVE + ["offer"]:
            if cols[st]:
                lines.append(f"{LABELS[st]} ({len(cols[st])})")
                lines += [f"  • {clip(o['title'], 50)}{' · ' + o['organizer'] if o['organizer'] else ''} ({ref(o['id'])})"
                          for o in cols[st][:5]]
        return "\n".join(lines) or "Nothing in the pipeline yet. /today shows what to apply to."

    def cmd_today(self, _):
        from workforce.habits import streak, todays_shortlist
        st = streak(self.db)
        rows = todays_shortlist(self.db, st["quota"])
        head = (f"Today: {st['today']}/{st['quota']} applied. Streak {plural(st['streak'], 'day')} (best {st['best']}).")
        if not rows:
            return head + "\nNo strong open matches right now. /opportunities all shows weaker ones."
        return head + "\n\n" + "\n".join(
            f"{i}. {clip(r['title'], 60)}{' at ' + r['organizer'] if r['organizer'] else ''}\n   "
            f"{int((r['relevance_score'] or 0) * 100)}% fit. {closes_in(r['deadline'])}." for i, r in enumerate(rows, 1)) \
            + "\n\n/opp <n> for details, /tailor <n> to tailor your resume, /confirm <n> to prepare it."

    def cmd_tailor(self, arg):
        o = find_opportunity(self.db, arg)
        self.queue.enqueue(TaskSpec(bot_id="applications", kind="tailor_resume", payload={"opportunity_id": o["id"]},
                                    requested_by="telegram", idempotency_key=f"tailor:{o['id']}:{int(now() // 60)}"))
        return f"Tailoring your resume for “{clip(o['title'], 70)}”. It takes a minute."

    def cmd_drafts(self, arg):
        from workforce.owner_actions import find_application
        a = find_application(self.db, arg)
        d = loads(a.get("drafts_json"), {}) or {}
        out = []
        if d.get("cover_letter"):
            out += ["Cover letter (draft, check it):", d["cover_letter"], ""]
        if d.get("bullets"):
            out += ["Resume bullets:"] + [f"• {b['text']}" for b in d["bullets"]] + [""]
        for k, v in (d.get("answers") or {}).items():
            out += [f"{k.replace('_', ' ').capitalize()}:", v, ""]
        if d.get("ats") and d["ats"].get("score") is not None:
            out.append(f"Keyword match {d['ats']['score']}%. Missing: {', '.join(d['ats']['missing'][:8]) or 'none'}")
        return "\n".join(out) or "No drafts yet. Upload your resume and set profile facts, then /confirm it again."

    def cmd_remind(self, arg):
        r = create_reminder(self.db, arg, by="telegram:owner")
        return f"Reminder set for {when_label(r['due_at'])}: {r['title']}"

    def cmd_missing(self, _):
        rows = missing_summary(self.db)
        if not rows:
            return "No application is waiting on anything from you."
        return "Your applications are waiting on:\n" + "\n".join(
            f"• {missing_label(item).capitalize()} ({plural(n, 'application')}): {how}" for item, n, how in rows[:12])

    def cmd_profile(self, arg):
        from workforce.sources.registry import load_profile, profile_problems
        parts = arg.split(" ", 2)
        if parts and parts[0].lower() == "set":
            if len(parts) < 3:
                return "Add a key and a value, e.g. /profile set graduation_year 2027"
            r = set_profile_fact(self.db, parts[1], parts[2], by="telegram:owner")
            extra = f" Re-preparing {plural(r['refreshed'], 'application')} with it." if r["refreshed"] else ""
            return f"Saved {r['key']} ({r['section']}{', kept private' if r['sensitive'] else ''}).{extra}"
        p = load_profile()
        lines = ["Verified facts:"]
        for sec in ("facts", "answers"):
            for k, v in (p.get(sec) or {}).items():
                if isinstance(v, dict) and v.get("verified") and v.get("value") not in (None, ""):
                    val = "•••" if v.get("sensitive") else clip(v["value"], 60)
                    lines.append(f"• {k}: {val}")
        if len(lines) == 1:
            lines.append("None yet.")
        probs = profile_problems(p)
        if probs:
            lines += ["", "Still missing:"] + [f"• {x}" for x in probs]
        return "\n".join(lines + ["", "To add or change one: /profile set <key> <value>"])

    def cmd_content(self, _):
        rows = self.db.all("SELECT title, platform, brand, state FROM content_items ORDER BY updated_at DESC LIMIT 10")
        return "\n".join(f"• {clip(r['title'], 60)} ({r['brand'] + ', ' if r['brand'] else ''}{r['platform']}): "
                         f"{r['state'].replace('_', ' ').lower()}" for r in rows) \
            or "No posts yet. Send /work <what you did> and I'll draft some."

    def cmd_work(self, arg):
        r = log_work(self.db, arg, by="telegram:owner")
        who = r["brand"] or "your accounts"
        return (f"Logged. I'm drafting posts for {who}. Each one goes through quality checks and your publishing rules "
                "before anything is posted; follow along with /content.")

    def cmd_ideas(self, _):
        r = request_ideas(self.db, by="telegram:owner")
        return ("Looking for trending project ideas now. The note will land in your vault under 02 Knowledge/Project Ideas."
                if r["queued"] else "Already on it. The note will land in your vault shortly.")

    def cmd_pause(self, _):
        self.state.set(SystemState.PAUSED, True, by="telegram:owner")
        return "Paused. Emails, posts and submissions wait until you send /resume. You'll still get alerts."

    def cmd_resume(self, _):
        self.state.set(SystemState.PAUSED, False, by="telegram:owner")
        return "Resumed. Held work continues within a few minutes." + (
            " Test mode is still on, so nothing goes out for real." if self.state.dry_run else "")


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
