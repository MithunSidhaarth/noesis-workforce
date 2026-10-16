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
from ..core.db import dumps
from ..core.queue import Task
from ..core.settings import get_settings
from ..core.util import new_id, now
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
                "daily_summary": self.daily_summary, "weekly_summary": self.weekly_summary}

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
                             "Try /help for what I can do.", key=f"unmapped:{task.id}")
            return {"workflow": None, "reason": "no matching workflow"}
        return self.start_workflow(ctx, name, params, task)

    def run_workflow(self, ctx: AgentContext, task: Task) -> dict:
        return self.start_workflow(ctx, task.payload["workflow"], task.payload.get("params", {}), task)

    def start_workflow(self, ctx: AgentContext, name: str, params: dict, task: Task) -> dict:
        steps = TEMPLATES.get(name)
        if steps is None:
            raise PermanentError(f"unknown workflow {name}")
        wid = task.workflow_id or new_id("wf")
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

    def _summary(self, ctx: AgentContext, since: float, label: str) -> str:
        db = ctx.db
        done = db.scalar("SELECT COUNT(*) FROM tasks WHERE status='succeeded' AND finished_at>=?", (since,))
        dead = db.all("SELECT bot_id, kind, error FROM tasks WHERE status='dead' AND finished_at>=? LIMIT 5", (since,))
        opps = db.all("SELECT title, relevance_score, verification_status FROM opportunities WHERE discovered_at>=? "
                      "ORDER BY relevance_score DESC LIMIT 5", (since,))
        n_opps = db.scalar("SELECT COUNT(*) FROM opportunities WHERE discovered_at>=?", (since,))
        approvals = db.scalar("SELECT COUNT(*) FROM approvals WHERE status='pending'")
        actions = db.all("SELECT status, COUNT(*) n FROM external_actions WHERE created_at>=? GROUP BY status", (since,))
        cost = db.scalar("SELECT COALESCE(SUM(cost_usd),0) FROM model_usage WHERE ts>=?", (since,))
        failing = db.all("SELECT name FROM sources WHERE reliability='failing'")
        lines = [f"📊 {label}", f"Tasks completed: {done}", f"Opportunities found: {n_opps}"]
        lines += [f"  • {o['title'][:70]} ({o['relevance_score']}, {o['verification_status']})" for o in opps]
        lines.append(f"External actions: {', '.join(f'{a['status']} {a['n']}' for a in actions) or 'none'}")
        lines.append(f"Pending approvals: {approvals}")
        lines.append(f"Model spend: ${cost:.4f}")
        if dead:
            lines.append("Failures needing attention:")
            lines += [f"  • {d['bot_id']}/{d['kind']}: {(d['error'] or '')[:80]}" for d in dead]
        if failing:
            lines.append("Failing sources: " + ", ".join(f["name"] for f in failing))
        return "\n".join(lines)

    def daily_summary(self, ctx: AgentContext, task: Task) -> dict:
        text = self._summary(ctx, now() - 86400, "Daily summary")
        day = datetime.now(ZoneInfo(get_settings().timezone)).strftime("%Y-%m-%d")
        ctx.notify_owner(text, key=f"daily:{day}")
        self._write_review(ctx, f"08 Reviews/Workforce {day}.md", f"workforce-daily:{day}", text)
        return {"summary": text}

    def weekly_summary(self, ctx: AgentContext, task: Task) -> dict:
        text = self._summary(ctx, now() - 7 * 86400, "Weekly summary")
        wk = datetime.now(ZoneInfo(get_settings().timezone)).strftime("%G-W%V")
        ctx.notify_owner(text, key=f"weekly:{wk}")
        return {"summary": text}

    def _write_review(self, ctx: AgentContext, rel: str, key: str, text: str) -> None:
        vault = ctx.services.get("vault")
        if vault is None or "vault.write" not in ctx.bot.tools:
            return
        ctx.act("vault.write", f"vault:{key}", lambda r: {**vault.upsert_note(key, rel, {"title": rel.split('/')[-1][:-3], "type": "review"},
                                                                               text, ctx.bot.id), "evidence": rel},
                target=rel, wait_on_approval=False)
