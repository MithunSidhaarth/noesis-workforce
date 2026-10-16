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
            ctx.notify_owner(f"I couldn't map this request to a workflow I can run safely: “{text[:200]}”. "
                             "Try /help for what I can do.", key=f"unmapped:{task.id}", force_now=True)
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
    def morning_brief(self, ctx: AgentContext, task: Task) -> dict:
        """One message a day with everything that can wait: what needs you, what closes
        this week, what is new, what is at risk. Held notifications are folded in."""
        from .. import notifications as nt
        from ..owner_actions import closes_in, pending_approvals, plain_status, ref, remember_listing
        db, t = ctx.db, now()
        day = datetime.now(ZoneInfo(get_settings().timezone)).strftime("%Y-%m-%d")
        lines = [f"☀️ Morning brief · {datetime.now(ZoneInfo(get_settings().timezone)):%a %d %b}"]
        if SystemState(db).dry_run:
            lines.append("🧪 Dry-run is ON: nothing is sent, published or submitted for real. Turn it off in Settings when ready.")
        if SystemState(db).paused:
            lines.append("⏸ Emergency pause is ON: external actions are on hold. /resume to continue.")

        need = []
        appr = pending_approvals(db, 5)
        for i, a in enumerate(appr, 1):
            need.append(f"{i}. Approve? {a['summary'][:90]}  (/approve {i} · /deny {i})")
        waiting = db.all("SELECT a.id, a.missing_json, o.title FROM applications a JOIN opportunities o ON o.id=a.opportunity_id "
                         "WHERE a.state IN ('PREPARATION','REVIEW') AND a.missing_json!='[]' ORDER BY o.deadline LIMIT 5")
        for w in waiting:
            need.append(f"• “{w['title'][:60]}” needs: {', '.join(loads(w['missing_json'])[:3])}")
        ready = db.all("SELECT a.id, o.title, o.official_url FROM applications a JOIN opportunities o ON o.id=a.opportunity_id "
                       "WHERE a.state='READY_TO_SUBMIT' LIMIT 5")
        for r in ready:
            need.append(f"• Submit “{r['title'][:60]}” on the site, then /submitted {ref(r['id'])} <confirmation>")
        unknown = db.scalar("SELECT COUNT(*) FROM external_actions WHERE status='unknown'")
        if unknown:
            need.append(f"• {unknown} action(s) with an unknown outcome: check them in the dashboard (Logs)")
        lines += ["", "Needs you:"] + (need or ["Nothing. 🎉"])
        if waiting:
            lines.append("See /missing for what to add to your profile.")

        closing = db.all("SELECT * FROM opportunities WHERE deadline BETWEEN ? AND ? AND verification_status!='failed' "
                         "AND application_status NOT IN ('SUBMITTED','CLOSED') AND (confirmed_by IS NOT NULL OR "
                         "COALESCE(relevance_score,0)>=0.5) ORDER BY deadline LIMIT 8", (t, t + 7 * 86400))
        if closing:
            remember_listing(db, "listing:opportunities", [o["id"] for o in closing])
            lines += ["", "Closing this week:"]
            for i, o in enumerate(closing, 1):
                lines.append(f"{i}. {o['title'][:70]} · {closes_in(o['deadline'])} · {plain_status(o)}  (/opp {i})")

        held = [n for n in nt.held(db, limit=40) if not n["urgent"]]
        if held:
            lines += ["", "New since yesterday:"]
            for n in held[:10]:
                first = n["text"].splitlines()[0][:110]
                r = next((ln for ln in n["text"].splitlines() if ln.startswith("Ref ")), "")
                lines.append(f"• {first}" + (f"  (/opp {r[4:]})" if r and n["key"].startswith("opp:") else ""))
            if len(held) > 10:
                lines.append(f"…and {len(held) - 10} more in the dashboard.")

        risky = db.all("SELECT a.id, o.title, o.deadline, a.state FROM applications a JOIN opportunities o ON o.id=a.opportunity_id "
                       "WHERE a.state IN ('REVIEW','READY_TO_SUBMIT') AND o.deadline BETWEEN ? AND ?", (t, t + 48 * 3600))
        if risky:
            lines += ["", "⚠️ At risk (under 48h, not submitted):"]
            lines += [f"• {r['title'][:70]} · {closes_in(r['deadline'])}" for r in risky]

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
        out += [f"• {d['bot_id']}/{d['kind']} failed {d['n']}×: {(d['error'] or '')[:80]}" for d in dead]
        out += [f"• Source “{f['name']}” keeps failing" for f in db.all("SELECT name FROM sources WHERE reliability='failing'")]
        out += [f"• {i['name']} is {i['status']}: {(i['detail'] or '')[:80]}" for i in
                db.all("SELECT name, status, detail FROM integrations WHERE status IN ('revoked','error')")]
        out += [f"• {b['name']} is offline" for b in db.all("SELECT name FROM bots WHERE enabled=1 AND state='offline'")]
        return out

    def daily_summary(self, ctx: AgentContext, task: Task) -> dict:
        db, since = ctx.db, now() - 86400
        day = datetime.now(ZoneInfo(get_settings().timezone)).strftime("%Y-%m-%d")
        lines = ["🌙 Today"]
        if SystemState(db).dry_run:
            lines.append("🧪 Dry-run is ON: everything below was rehearsed, nothing went out for real.")
        appr = db.scalar("SELECT COUNT(*) FROM approvals WHERE status='pending'")
        missing = db.scalar("SELECT COUNT(*) FROM applications WHERE state IN ('PREPARATION','REVIEW') AND missing_json!='[]'")
        needs = ([f"• {appr} approval(s) waiting (/inbox)"] if appr else []) + \
                ([f"• {missing} application(s) waiting on your info (/missing)"] if missing else [])
        lines += ["Needs you:"] + (needs or ["Nothing."])
        found = db.scalar("SELECT COUNT(*) FROM opportunities WHERE discovered_at>=?", (since,))
        good = db.scalar("SELECT COUNT(*) FROM opportunities WHERE discovered_at>=? AND COALESCE(relevance_score,0)>=0.7 "
                         "AND verification_status!='failed'", (since,))
        prepared = db.scalar("SELECT COUNT(*) FROM applications WHERE created_at>=?", (since,))
        submitted = db.scalar("SELECT COUNT(*) FROM applications WHERE state IN ('SUBMITTED','FOLLOW_UP') AND updated_at>=?", (since,))
        published = db.scalar("SELECT COUNT(*) FROM content_items WHERE published_at>=?", (since,))
        drafts = db.scalar("SELECT COUNT(*) FROM emails WHERE draft IS NOT NULL AND updated_at>=?", (since,))
        lines += ["Done:", f"• {found} opportunities scanned, {good} good matches",
                  f"• {prepared} application(s) prepared, {submitted} submitted",
                  f"• {published} post(s) published, {drafts} email draft(s)"]
        health = self._health(ctx, since)
        if health:
            lines += ["Needs fixing:"] + health
        text = "\n".join(lines)
        ctx.notify_owner(text, key=f"daily:{day}", force_now=True)
        self._write_review(ctx, f"08 Reviews/Workforce {day}.md", f"workforce-daily:{day}", text)
        return {"summary": text}

    def weekly_summary(self, ctx: AgentContext, task: Task) -> dict:
        from ..owner_actions import missing_summary
        db, since = ctx.db, now() - 7 * 86400
        wk = datetime.now(ZoneInfo(get_settings().timezone)).strftime("%G-W%V")
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
        lines = [f"📈 Week {wk}"]
        if SystemState(db).dry_run:
            lines.append("🧪 Dry-run was ON this week.")
        lines += [f"Funnel: {disc} found → {matched} good matches → {confirmed} verified → {prepared} prepared → {submitted} submitted",
                  "Outcomes: " + (", ".join(f"{o['outcome']} {o['n']}" for o in outcomes) or "none recorded yet")]
        if missed:
            lines.append("Missed deadlines: " + "; ".join(m["title"][:50] for m in missed[:5]))
        if skips:
            lines.append("Why you skipped: " + "; ".join(f"{s['skip_reason'][:40]} ({s['n']})" for s in skips))
        lines.append(f"Model spend: ${spend:.2f}")
        tips = []
        for s_ in skips:
            if s_["n"] >= 3:
                tips.append(f"You skipped {s_['n']} for “{s_['skip_reason'][:40]}”. Add a word from that to exclude_keywords in your profile.")
        for r in src:
            if (r["found"] or 0) >= 10 and not r["ok"]:
                tips.append(f"“{r['name']}” found {r['found']} this week and none checked out. Consider disabling it.")
        top_missing = missing_summary(db)[:2]
        for item, n, how in top_missing:
            tips.append(f"{n} application(s) wait on “{item}”: {how}")
        if missed:
            tips.append("Tap ⏰ Remind me on matches you care about to get T-7/T-2/T-1 reminders.")
        if tips:
            lines += ["Suggestions:"] + [f"• {t_}" for t_ in tips[:5]]
        health = self._health(ctx, since)
        if health:
            lines += ["Needs fixing:"] + health
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
