"""Chief of Staff / Orchestrator.

Deterministic routing: objectives are matched to workflow templates (task DAGs)
and dispatched to bots by registered capability. A model is only consulted to
classify a free-text objective into one of the known templates, never to
create new tools or permissions.
"""
from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from ..contracts import TaskSpec
from ..core.db import dumps, loads
from ..core.queue import Task
from ..core.settings import get_settings
from ..core.state import SystemState
from ..core.util import now
from .base import Agent, AgentContext, PermanentError, register, untrusted

# template -> list of steps; each step: (key, bot, kind, payload_template, depends_on_keys)
TEMPLATES: dict[str, list[tuple[str, str, str, dict, list[str]]]] = {
    "find_opportunities": [
        ("sweep", "opportunity_scout", "find_opportunities", {}, []),
    ],
    "capture_knowledge": [
        ("ingest", "knowledge", "ingest_url", {"url": "{url}"}, []),
    ],
    "content_from_topic": [
        ("context", "knowledge", "find_context", {"query": "{topic}"}, []),
        ("brief", "content_strategist", "create_brief", {"topic": "{topic}", "platform": "{platform}"}, ["context"]),
    ],
    "outreach_research": [
        ("draft", "outreach", "draft_outreach", {"contact_id": "{contact_id}"}, []),
    ],
    "reminder": [
        ("remind", "personal_ops", "create_reminder", {"title": "{title}", "due_at": "{due_at}"}, []),
    ],
}

KEYWORDS = [
    ("capture_knowledge", re.compile(r"https?://\S+")),
    ("find_opportunities", re.compile(r"\b(opportunit|internship|hackathon|fellowship|scholarship|grant)", re.I)),
    ("content_from_topic", re.compile(r"\b(post|content|reel|linkedin|instagram|caption)\b", re.I)),
]


def fill(template: dict, params: dict) -> dict:
    out = {}
    for k, v in template.items():
        if isinstance(v, str) and v.startswith("{") and v.endswith("}"):
            out[k] = params.get(v[1:-1])
        else:
            out[k] = v
    return out


@register("orchestrator")
class Orchestrator(Agent):
    accepts = {"objective"}

    def handlers(self):
        return {"objective": self.objective, "run_workflow": self.run_workflow,
                "daily_summary": self.daily_summary, "weekly_summary": self.weekly_summary,
                "morning_brief": self.morning_brief}

    def classify(self, ctx: AgentContext, text: str) -> tuple[str | None, dict]:
        for name, rx in KEYWORDS:
            m = rx.search(text)
            if m:
                params = {"url": m.group(0)} if name == "capture_knowledge" else {"topic": text[:200], "platform": "linkedin"}
                return name, params
        if ctx.has_model("classify"):
            prompt = (f"Choose one workflow for the request from {sorted(TEMPLATES)} or 'none'. "
                      "Reply with the name only.\n" + untrusted(text, 2000))
            try:
                name = ctx.llm("classify", prompt, max_tokens=20).text.strip().lower()
                if name in TEMPLATES:
                    return name, {"topic": text[:200], "platform": "linkedin"}
            except Exception as e:  # noqa: BLE001
                ctx.log(f"classifier unavailable: {e}", level="debug")
        return None, {}

    def objective(self, ctx: AgentContext, task: Task) -> dict:
        text = str(task.payload.get("text", ""))[:4000]
        name = task.payload.get("workflow")
        params = task.payload.get("params") or {}
        if not name:
            name, params = self.classify(ctx, text)
        if not name:
            ctx.notify_owner(f"🤔 I couldn't act on that request\n\n“{text[:200]}”\n\nIt doesn't match anything I can run "
                             "safely. Try rephrasing it, e.g. \"find hackathons\", or send /help to see the commands.",
                             key=f"unmapped:{task.id}", force_now=True)
            return {"workflow": None, "reason": "no matching workflow"}
        return self.start_workflow(ctx, name, params, task)

    def run_workflow(self, ctx: AgentContext, task: Task) -> dict:
        return self.start_workflow(ctx, task.payload["workflow"], task.payload.get("params", {}), task)

    def start_workflow(self, ctx: AgentContext, name: str, params: dict, task: Task) -> dict:
        steps = TEMPLATES.get(name)
        if steps is None:
            raise PermanentError(f"unknown workflow {name}")
        # derived from the task, so a retried objective reuses the same workflow and task keys
        wid = task.workflow_id or f"wf_{task.id.split('_', 1)[-1]}"
        ctx.db.execute("INSERT OR IGNORE INTO workflows(id, name, status, requested_by, spec_json, created_at, updated_at) "
                       "VALUES (?,?,?,?,?,?,?)", (wid, name, "running", task.requested_by, dumps({"params": params}), now(), now()))
        ids: dict[str, str] = {}
        for key, bot, kind, tmpl, deps in steps:
            tid, _ = ctx.queue.enqueue(TaskSpec(
                bot_id=bot, kind=kind, payload=fill(tmpl, params), workflow_id=wid, parent_task_id=task.id,
                correlation_id=task.correlation_id, requested_by=ctx.bot.id, delegation_depth=task.delegation_depth + 1,
                depends_on=[ids[d] for d in deps], idempotency_key=f"wf:{wid}:{key}"))
            ids[key] = tid
        return {"workflow_id": wid, "workflow": name, "tasks": ids}

    def on_research_finding(self, ctx: AgentContext, msg: dict) -> None:
        counts = ctx.memory_get("findings_today", {})
        day = datetime.now(ZoneInfo(get_settings().timezone)).strftime("%Y-%m-%d")
        counts[day] = counts.get(day, 0) + 1
        ctx.memory_set("findings_today", {day: counts[day]})

    # ---- owner-facing reports -------------------------------------------
    # Telegram messages are sent as plain text (no parse_mode), so no Markdown here.
    # Layout: a one-line headline with one emoji, a blank line, then short sections
    # (a plain header line plus bullets), only when they have content.
    @staticmethod
    def _today():
        return datetime.now(ZoneInfo(get_settings().timezone))

    @staticmethod
    def _first_name() -> str:
        try:
            from ..sources.registry import load_profile
            return str(load_profile().get("name") or "").split(" ")[0].strip()
        except Exception:  # noqa: BLE001  (a greeting is never worth failing the brief)
            return ""

    @staticmethod
    def _mode_lines(db) -> list[str]:
        st, out = SystemState(db), []
        if st.dry_run:
            out.append("Test mode is on: nothing is sent, posted or submitted for real. Turn it off in the dashboard Settings when ready.")
        if st.paused:
            out.append("External actions are paused. Send /resume to restart them.")
        return out

    @staticmethod
    def _section(lines: list[str], title: str, items: list[str]) -> None:
        if items:
            lines += ["", title] + items

    def morning_brief(self, ctx: AgentContext, task: Task) -> dict:
        """One message a day with everything that can wait: what needs you, what closes
        this week, what is new, what is at risk. Held notifications are folded in."""
        from .. import notifications as nt
        from ..owner_actions import (clip, closes_in, day_label, lower_first, missing_label, pending_approvals, plain_status,
                                     plural, ref, remember_listing)
        db, t = ctx.db, now()
        today = self._today()
        day = today.strftime("%Y-%m-%d")
        name = self._first_name()
        lines = [f"☀️ Good morning{', ' + name if name else ''}. {day_label(t)}"]
        mode = self._mode_lines(db)
        if mode:
            lines += [""] + mode

        need = []
        appr = pending_approvals(db, 5)
        for i, a in enumerate(appr, 1):
            need.append(f"• Approve or deny: {clip(a['summary'], 90)} (/approve {i} or /deny {i})")
        waiting = db.all("SELECT a.id, a.missing_json, o.title FROM applications a JOIN opportunities o ON o.id=a.opportunity_id "
                         "WHERE a.state IN ('PREPARATION','REVIEW') AND a.missing_json!='[]' ORDER BY o.deadline LIMIT 5")
        for w in waiting:
            need.append(f"• “{clip(w['title'], 60)}” needs {', '.join(missing_label(m) for m in loads(w['missing_json'])[:3])} (/missing)")
        ready = db.all("SELECT a.id, o.title, o.official_url FROM applications a JOIN opportunities o ON o.id=a.opportunity_id "
                       "WHERE a.state='READY_TO_SUBMIT' LIMIT 5")
        for r in ready:
            need.append(f"• Submit “{clip(r['title'], 60)}” on its website, then send /submitted {ref(r['id'])} <confirmation>")
        unknown = db.scalar("SELECT COUNT(*) FROM external_actions WHERE status='unknown'")
        if unknown:
            need.append(f"• {plural(unknown, 'action')} may or may not have gone through. Check the dashboard Logs page before anything is retried.")
        self._section(lines, "Needs you", need)

        closing = db.all("SELECT * FROM opportunities WHERE deadline BETWEEN ? AND ? AND verification_status!='failed' "
                         "AND application_status NOT IN ('SUBMITTED','CLOSED') AND (confirmed_by IS NOT NULL OR "
                         "COALESCE(relevance_score,0)>=0.5) ORDER BY deadline LIMIT 8", (t, t + 7 * 86400))
        if closing:
            remember_listing(db, "listing:opportunities", [o["id"] for o in closing])
        self._section(lines, "Closing this week", [
            f"{i}. {clip(o['title'], 70)}: {lower_first(closes_in(o['deadline']))}. {plain_status(o)}. (/opp {i})"
            for i, o in enumerate(closing, 1)])

        held = [n for n in nt.held(db, limit=40) if not n["urgent"]]
        listed = {o["id"] for o in closing}
        new = []
        for n in held:
            if n["key"].startswith("opp:"):
                o = db.one("SELECT * FROM opportunities WHERE id=?", (n["key"][4:],))
                if o and o["id"] in listed:
                    continue          # already under "Closing this week"
                if o:
                    new.append(f"• New {o['type'] if o['type'] not in ('unknown', 'other', None) else 'opportunity'}: "
                               f"{clip(o['title'], 70)}, {lower_first(closes_in(o['deadline']))}. (/opp {ref(o['id'])})")
                    continue
            first = n["text"].splitlines()[0] if n["text"] else ""
            head = first.split(" ", 1)[1] if first and not first[0].isalnum() and " " in first else first
            new.append(f"• {clip(head, 110)}")
        if len(new) > 10:
            new = new[:10] + [f"…and {len(new) - 10} more in the dashboard."]
        self._section(lines, "New since yesterday", new)

        risky = db.all("SELECT a.id, o.title, o.deadline, a.state FROM applications a JOIN opportunities o ON o.id=a.opportunity_id "
                       "WHERE a.state IN ('REVIEW','READY_TO_SUBMIT') AND o.deadline BETWEEN ? AND ?", (t, t + 48 * 3600))
        self._section(lines, "At risk: closes within 48 hours and not submitted",
                      [f"• {clip(r['title'], 70)}: {lower_first(closes_in(r['deadline']))}" for r in risky])

        todo = []
        if appr:
            todo.append(f"answer {plural(len(appr), 'approval')}")
        if waiting:
            todo.append(f"fill in details for {plural(len(waiting), 'application')}")
        if ready:
            todo.append(f"submit {plural(len(ready), 'application')}")
        if unknown:
            todo.append(f"check {plural(unknown, 'unclear action')}")
        if todo:
            joined = todo[0] if len(todo) == 1 else ", ".join(todo[:-1]) + " and " + todo[-1]
            lines += ["", f"Today: {joined}."]
        elif risky:
            lines += ["", "Today: keep an eye on the deadlines above."]
        else:
            lines += ["", "Nothing needs you today."]

        text = "\n".join(lines)
        res = ctx.notify_owner(text, key=f"brief:{day}", force_now=True)
        if res is not None and res.status in ("succeeded", "dry_run", "duplicate"):
            for n in held:
                nt.mark(db, n["id"], "digested")
        self._write_review(ctx, f"08 Reviews/Brief {day}.md", f"workforce-brief:{day}", text)
        return {"needs_you": len(need), "closing": len(closing), "new": len(held), "at_risk": len(risky),
                "delivered": bool(res and res.status in ("succeeded", "dry_run", "duplicate"))}

    def _health(self, ctx: AgentContext, since: float) -> list[str]:
        """Only what is broken; silence means healthy."""
        db, out = ctx.db, []
        dead = db.all("SELECT bot_id, kind, COUNT(*) n, MAX(error) error FROM tasks WHERE status='dead' AND finished_at>=? "
                      "GROUP BY bot_id, kind ORDER BY n DESC LIMIT 5", (since,))
        out += [f"• {d['bot_id']}: {d['kind'].replace('_', ' ')} failed {d['n']} time{'s' if d['n'] != 1 else ''}. "
                f"Last error: {(d['error'] or 'none recorded')[:80]}" for d in dead]
        out += [f"• The source “{f['name']}” keeps failing. Check or disable it in the dashboard."
                for f in db.all("SELECT name FROM sources WHERE reliability='failing'")]
        for i in db.all("SELECT name, status, detail FROM integrations WHERE status IN ('revoked','error')"):
            detail = f" ({(i['detail'] or '')[:80]})" if i["detail"] else ""
            out.append(f"• {i['name']} was disconnected{detail}. Reconnect it, then check the dashboard Integrations page."
                       if i["status"] == "revoked" else f"• {i['name']} is returning errors{detail}. See the dashboard Integrations page.")
        out += [f"• {b['name']} is offline." for b in db.all("SELECT name FROM bots WHERE enabled=1 AND state='offline'")]
        return out

    def daily_summary(self, ctx: AgentContext, task: Task) -> dict:
        from ..owner_actions import day_label, plural
        db, since = ctx.db, now() - 86400
        day = self._today().strftime("%Y-%m-%d")
        lines = [f"🌙 Your day in review, {day_label(now())}"]
        if SystemState(db).dry_run:
            lines += ["", "Test mode was on: everything below was rehearsed, nothing went out for real."]
        appr = db.scalar("SELECT COUNT(*) FROM approvals WHERE status='pending'")
        missing = db.scalar("SELECT COUNT(*) FROM applications WHERE state IN ('PREPARATION','REVIEW') AND missing_json!='[]'")
        self._section(lines, "Needs you",
                      ([f"• {plural(appr, 'approval')} waiting (/inbox)"] if appr else [])
                      + ([f"• {plural(missing, 'application')} waiting on details from you (/missing)"] if missing else []))
        found = db.scalar("SELECT COUNT(*) FROM opportunities WHERE discovered_at>=?", (since,))
        good = db.scalar("SELECT COUNT(*) FROM opportunities WHERE discovered_at>=? AND COALESCE(relevance_score,0)>=0.7 "
                         "AND verification_status!='failed'", (since,))
        prepared = db.scalar("SELECT COUNT(*) FROM applications WHERE created_at>=?", (since,))
        submitted = db.scalar("SELECT COUNT(*) FROM applications WHERE state IN ('SUBMITTED','FOLLOW_UP') AND updated_at>=?", (since,))
        published = db.scalar("SELECT COUNT(*) FROM content_items WHERE published_at>=?", (since,))
        drafts = db.scalar("SELECT COUNT(*) FROM emails WHERE draft IS NOT NULL AND updated_at>=?", (since,))
        done = []
        if found:
            done.append(f"• Found {plural(found, 'new listing')}, {plural(good, 'good match', 'good matches')}")
        if prepared:
            done.append(f"• Prepared {plural(prepared, 'application')}")
        if submitted:
            done.append(f"• Submitted {plural(submitted, 'application')}")
        if published:
            done.append(f"• Published {plural(published, 'post')}")
        if drafts:
            done.append(f"• Drafted {plural(drafts, 'email reply', 'email replies')}")
        self._section(lines, "Done today", done)
        health = self._health(ctx, since)
        self._section(lines, "Needs fixing", health)
        if len(lines) == 1:
            lines += ["", "A quiet day: nothing new and nothing waiting for you."]
        text = "\n".join(lines)
        ctx.notify_owner(text, key=f"daily:{day}", force_now=True)
        self._write_review(ctx, f"08 Reviews/Workforce {day}.md", f"workforce-daily:{day}", text)
        return {"summary": text}

    def weekly_summary(self, ctx: AgentContext, task: Task) -> dict:
        from ..owner_actions import clip, day_label, missing_label, missing_summary, plural
        db, since = ctx.db, now() - 7 * 86400
        today = self._today()
        wk = today.strftime("%G-W%V")
        disc = db.scalar("SELECT COUNT(*) FROM opportunities WHERE discovered_at>=?", (since,))
        matched = db.scalar("SELECT COUNT(*) FROM opportunities WHERE discovered_at>=? AND COALESCE(relevance_score,0)>=0.7", (since,))
        confirmed = db.scalar("SELECT COUNT(*) FROM opportunities WHERE discovered_at>=? AND verification_status='verified'", (since,))
        prepared = db.scalar("SELECT COUNT(*) FROM applications WHERE created_at>=?", (since,))
        submitted = db.scalar("SELECT COUNT(*) FROM applications WHERE evidence IS NOT NULL AND updated_at>=?", (since,))
        outcomes = db.all("SELECT outcome, COUNT(*) n FROM applications WHERE outcome IS NOT NULL AND updated_at>=? GROUP BY outcome", (since,))
        missed = db.all("SELECT title FROM opportunities WHERE deadline BETWEEN ? AND ? AND application_status NOT IN ('SUBMITTED','CLOSED') "
                        "AND (confirmed_by IS NOT NULL OR COALESCE(relevance_score,0)>=0.7)", (since, now()))
        skips = db.all("SELECT skip_reason, COUNT(*) n FROM opportunities WHERE skip_reason IS NOT NULL GROUP BY skip_reason "
                       "ORDER BY n DESC LIMIT 3")
        spend = db.scalar("SELECT COALESCE(SUM(cost_usd),0) FROM model_usage WHERE ts>=?", (since,))
        src = db.all("SELECT s.name, COUNT(o.id) found, SUM(CASE WHEN o.verification_status='verified' THEN 1 ELSE 0 END) ok "
                     "FROM sources s LEFT JOIN opportunities o ON o.discovery_source=s.id AND o.discovered_at>=? "
                     "WHERE s.enabled=1 GROUP BY s.id", (since,))
        lines = [f"📈 Your week in review, {day_label(since)} to {day_label(now())}"]
        if SystemState(db).dry_run:
            lines += ["", "Test mode was on this week, so nothing went out for real."]
        opp = [f"• {disc} found → {plural(matched, 'good match', 'good matches')} → {confirmed} verified → "
               f"{prepared} prepared → {submitted} submitted",
               "• Outcomes: " + (", ".join(f"{o['outcome'].replace('_', ' ')} {o['n']}" for o in outcomes) or "none recorded yet")]
        if missed:
            opp.append("• Missed deadlines: " + "; ".join(clip(m["title"], 50) for m in missed[:5]))
        if skips:
            opp.append("• Why you skipped: " + "; ".join(f"{clip(s['skip_reason'], 40)} ({s['n']})" for s in skips))
        self._section(lines, "Opportunities", opp)
        self._section(lines, "Costs", [f"• Model spend: ${spend:.2f}"])
        tips = []
        for s_ in skips:
            if s_["n"] >= 3:
                tips.append(f"You skipped {s_['n']} for “{clip(s_['skip_reason'], 40)}”. "
                            "Add a word from that to exclude_keywords in your profile and I'll stop showing them.")
        for r in src:
            if (r["found"] or 0) >= 10 and not r["ok"]:
                tips.append(f"“{r['name']}” found {r['found']} this week and none checked out. Consider disabling it.")
        for item, n, how in missing_summary(db)[:2]:
            tips.append(f"{plural(n, 'application')} wait{'s' if n == 1 else ''} on {missing_label(item)}: {how}")
        if missed:
            tips.append("Tap “Remind me” on matches you care about to get reminders 7, 2 and 1 days before they close.")
        self._section(lines, "Suggestions", [f"• {t_}" for t_ in tips[:5]])
        self._section(lines, "Needs fixing", self._health(ctx, since))
        text = "\n".join(lines)
        ctx.notify_owner(text, key=f"weekly:{wk}", force_now=True)
        self._write_review(ctx, f"08 Reviews/Workforce {wk}.md", f"workforce-weekly:{wk}", text)
        return {"summary": text, "funnel": [disc, matched, confirmed, prepared, submitted]}

    def _write_review(self, ctx: AgentContext, rel: str, key: str, text: str) -> None:
        vault = ctx.services.get("vault")
        if vault is None or "vault.write" not in ctx.bot.tools:
            return
        ctx.act("vault.write", f"vault:{key}", lambda r: {**vault.upsert_note(key, rel, {"title": rel.split('/')[-1][:-3], "type": "review"},
                                                                               text, ctx.bot.id), "evidence": rel},
                target=rel, wait_on_approval=False)
