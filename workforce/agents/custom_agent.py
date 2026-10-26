"""The engine behind every custom agent and the built-in Assistant.

One registered bot (`custom_agents`) runs them all; each task names the agent. The
agent's row (task, tools, model tier) is read fresh for every task, so dashboard edits
apply to the next message. The model answers with {"reply", "actions"}; each action
must be a tool from the fixed allow-list (workforce/custom_agents.py TOOLS) that the
owner ticked for this agent, and runs through the same owner_actions / ctx.act paths
as everything else. Nothing here can message anyone but the owner.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..core.queue import Task
from ..core.settings import get_settings
from ..core.state import audit
from ..core.util import now, sha
from ..model_router.router import BudgetExceeded, ModelUnavailable
from .base import Agent, AgentContext, PermanentError, register, untrusted

MAX_ROUNDS = 3              # model calls per message: answer, (tool results -> answer) x2
MAX_ACTIONS = 4             # tool calls per round
READ_TOOLS = {"search_opportunities", "today_shortlist", "company_dossier", "question_bank", "find_referrals"}


@register("custom_agent")
class CustomAgent(Agent):
    accepts = {"chat"}

    def handlers(self):
        return {"chat": self.chat, "run_task": self.run_task}

    # ---- entry points ----------------------------------------------------------
    def chat(self, ctx: AgentContext, task: Task) -> dict:
        from .. import custom_agents as ca
        agent = self._agent(ctx, task)
        if agent is None:
            return {"skipped": "agent disabled or deleted"}
        text = str(task.payload.get("text") or "").strip()
        voice = task.payload.get("voice_path")
        if voice:
            heard = self._transcribe(ctx, voice)
            if heard is None:
                self.reply(ctx, agent, task, "🎙️ I couldn't transcribe that voice note (voice notes need GROQ_API_KEY in .env). "
                           "Type your answer instead.", n=0)
                return {"voice": "failed"}
            text = f"(voice note) {heard}" + (f"\n{text}" if text else "")
        if not text:
            return {"skipped": "empty message"}
        ca.remember(ctx.db, agent["id"], "owner", text)
        answer, used = self.converse(ctx, agent, task, text)
        ca.remember(ctx.db, agent["id"], "agent", answer)
        self.reply(ctx, agent, task, answer)
        ctx.db.execute("UPDATE custom_agents SET last_active=?, last_error=NULL WHERE id=?", (now(), agent["id"]))
        return {"agent_id": agent["id"], "tools_used": used}

    def run_task(self, ctx: AgentContext, task: Task) -> dict:
        """A scheduled run: the agent does its job unprompted (morning scout, evening question...)."""
        from .. import custom_agents as ca
        agent = self._agent(ctx, task)
        if agent is None:
            return {"skipped": "agent disabled or deleted"}
        cue = (f"[Scheduled run, {self._now_label()}] Do your scheduled task now, as described in your job. "
               "If there is nothing worth telling the owner, reply exactly NOTHING.")
        answer, used = self.converse(ctx, agent, task, cue)
        if answer.strip().upper().rstrip(".") == "NOTHING":
            return {"agent_id": agent["id"], "sent": False}
        ca.remember(ctx.db, agent["id"], "agent", answer)
        self.reply(ctx, agent, task, answer)
        ctx.db.execute("UPDATE custom_agents SET last_active=? WHERE id=?", (now(), agent["id"]))
        return {"agent_id": agent["id"], "sent": True, "tools_used": used}

    # ---- the conversation --------------------------------------------------------
    @staticmethod
    def _agent(ctx: AgentContext, task: Task) -> dict | None:
        from ..core.db import loads
        aid = task.payload.get("agent_id")
        row = ctx.db.one("SELECT * FROM custom_agents WHERE id=?", (aid,))
        if not row or not row["enabled"]:
            return None
        row["tools"] = loads(row["tools_json"], [])
        return row

    @staticmethod
    def _now_label() -> str:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(get_settings().timezone)).strftime("%a %d %b %Y, %H:%M")

    def system_for(self, ctx: AgentContext, agent: dict) -> str:
        from .. import custom_agents as ca
        from ..habits import quota
        from ..resume import active_resume
        from ..sources.registry import load_profile
        from .opportunity_scout import profile_summary
        tools = [f"- {t}({ca.TOOLS[t]['args']}): {ca.TOOLS[t]['label']}" for t in agent["tools"] if t in ca.TOOLS]
        res = active_resume(ctx.db)
        parts = [f"Your name: {agent['name']} {agent['emoji']}", f"Your job, in the owner's words:\n{agent['task']}",
                 "Your tools:\n" + ("\n".join(tools) if tools else "(none: just talk)"),
                 f"Now: {self._now_label()} ({get_settings().timezone}). Daily application quota: {quota()}.",
                 "The owner's verified profile (the only facts you may state about them):\n"
                 + json.dumps(profile_summary(load_profile()), ensure_ascii=False)[:2500]]
        if res and ({"tailor_resume", "log_debrief", "question_bank"} & set(agent["tools"]) or agent.get("template") == "coach"):
            parts.append("Resume lines (verified; quote, don't embellish):\n" + untrusted(res["text"], 3500))
        return "\n\n".join(parts)

    def converse(self, ctx: AgentContext, agent: dict, task: Task, text: str) -> tuple[str, list[str]]:
        from .. import custom_agents as ca
        if not ctx.has_model("write"):
            return ("I can't think right now: no language model is available (set up Claude Code or a free Gemini / Groq key, "
                    "see /status on the main bot)."), []
        tier = agent.get("model_tier") if agent.get("model_tier") in ("cheap", "standard") else "standard"
        orig = ctx.bot
        ctx.bot = orig.model_copy(update={"model": orig.model.model_copy(update={"prefer_tier": tier})})
        try:
            history = ca.recent_turns(ctx.db, agent["id"])[:-1] if not text.startswith("[Scheduled") else \
                ca.recent_turns(ctx.db, agent["id"])
            convo = "\n".join(f"{'OWNER' if t['role'] == 'owner' else 'YOU'}: {t['text'][:1500]}" for t in history)
            prompt = (("Conversation so far:\n" + convo + "\n\n") if convo else "") + f"OWNER: {text[:6000]}"
            used: list[str] = []
            reply = ""
            for rnd in range(MAX_ROUNDS):
                data = self._ask(ctx, agent, prompt)
                reply = data["reply"] or reply
                actions = data["actions"][:MAX_ACTIONS]
                if not actions or rnd == MAX_ROUNDS - 1:
                    break
                results = []
                for a in actions:
                    out = self.run_tool(ctx, agent, task, a.get("tool"), a.get("args") or {}, n=len(used))
                    used.append(str(a.get("tool")))
                    results.append({"tool": a.get("tool"), "result": out})
                needs_answer = any(r["tool"] in READ_TOOLS for r in results)
                if not needs_answer:
                    done = "\n".join(f"• {r['tool']}: {r['result'][:200]}" for r in results)
                    reply = (reply + ("\n\n" if reply else "") + done).strip()
                    break
                prompt += (f"\nYOU: {json.dumps({'reply': reply, 'actions': actions}, ensure_ascii=False)[:2000]}\n"
                           "TOOL RESULTS:\n" + untrusted(json.dumps(results, ensure_ascii=False), 9000)
                           + "\nNow write your answer to the owner (JSON as before; actions only if still needed).")
            return (reply or "(no reply)")[:3800], used
        except (ModelUnavailable, BudgetExceeded) as e:
            return f"My model is unavailable right now ({str(e)[:120]}). Try again in a bit.", []
        finally:
            ctx.bot = orig

    def _ask(self, ctx: AgentContext, agent: dict, prompt: str) -> dict:
        c = ctx.llm("write", prompt, system_extra=self.system_for(ctx, agent), max_tokens=1200)
        try:
            data = c.json()
        except (ValueError, json.JSONDecodeError):
            return {"reply": c.text.strip()[:3800], "actions": []}
        if not isinstance(data, dict):
            return {"reply": c.text.strip()[:3800], "actions": []}
        acts = data.get("actions") if isinstance(data.get("actions"), list) else []
        return {"reply": str(data.get("reply") or "").strip(),
                "actions": [a for a in acts if isinstance(a, dict) and isinstance(a.get("tool"), str)]}

    # ---- tools ---------------------------------------------------------------------
    def run_tool(self, ctx: AgentContext, agent: dict, task: Task, name: str | None, args: dict, n: int = 0) -> str:
        """Returns a short result string for the model and the owner. Never raises for a bad call."""
        from .. import custom_agents as ca
        from ..owner_actions import OwnerActionError
        if name not in ca.TOOLS or name not in agent["tools"]:
            audit(ctx.db, f"agent:{agent['id']}", "custom_agent.tool_refused", task.id, {"tool": str(name)[:40]})
            return f"refused: '{name}' is not one of your tools"
        args = {k: v for k, v in args.items() if isinstance(k, str)} if isinstance(args, dict) else {}
        try:
            out = getattr(self, f"tool_{name}")(ctx, agent, task, args, n)
        except OwnerActionError as e:
            out = f"could not do it: {e}"
        except PermanentError as e:
            out = f"could not do it: {e}"
        except Exception as e:  # noqa: BLE001  (a broken tool must not kill the conversation)
            from ..core.util import redact
            ctx.log(f"tool {name} failed: {e}", level="warning")
            out = f"failed: {redact(str(e))[:160]}"
        audit(ctx.db, f"agent:{agent['id']}", "custom_agent.tool", task.id, {"tool": name, "result": str(out)[:120]})
        return str(out)[:4000]

    @staticmethod
    def _by(agent: dict) -> str:
        return f"agent:{agent['id']}"

    def tool_create_reminder(self, ctx, agent, task, a, n):
        from ..owner_actions import create_reminder, when_label
        r = create_reminder(ctx.db, f"{a.get('when', '')} {a.get('what', '')}".strip(), by=self._by(agent))
        return f"reminder set for {when_label(r['due_at'])}: {r['title']}"

    def tool_search_opportunities(self, ctx, agent, task, a, n):
        from ..owner_actions import closes_in, ref
        q = str(a.get("query") or "").strip().lower()[:80]
        days = int(a.get("days") or 0) if str(a.get("days") or "").isdigit() else 0
        limit = max(1, min(10, int(a.get("limit") or 6) if str(a.get("limit") or "").isdigit() else 6))
        sql = ("SELECT id, title, organizer, deadline, stage, relevance_score, ghost_risk, official_url FROM opportunities "
               "WHERE verification_status!='failed' AND stage NOT IN ('skipped','rejected','withdrawn') AND (deadline IS NULL OR deadline>?)")
        args: list = [now()]
        for w in q.split()[:5]:
            sql += " AND lower(title || ' ' || COALESCE(organizer,'') || ' ' || COALESCE(summary,'')) LIKE ?"
            args.append(f"%{w}%")
        if days:
            sql += " AND discovered_at>=?"
            args.append(now() - days * 86400)
        rows = ctx.db.all(sql + " ORDER BY COALESCE(relevance_score,0) DESC, deadline LIMIT ?", (*args, limit))
        if not rows:
            return "no matching listings"
        return "\n".join(f"[{ref(r['id'])}] {r['title'][:80]} at {r['organizer'] or '?'}; {closes_in(r['deadline'])}; "
                         f"score {int((r['relevance_score'] or 0) * 100)}%; stage {r['stage']}"
                         + ("; possible ghost listing" if (r['ghost_risk'] or 0) >= 0.5 else "")
                         + (f"; {r['official_url']}" if r['official_url'] else "") for r in rows)

    def tool_today_shortlist(self, ctx, agent, task, a, n):
        from ..habits import quota, streak, todays_shortlist
        from ..owner_actions import closes_in, ref
        rows = todays_shortlist(ctx.db)
        s = streak(ctx.db)
        head = f"quota {quota()}/day; applied today {s.get('today', 0)}; streak {s.get('streak', 0)} days"
        return head + "\n" + ("\n".join(f"[{ref(r['id'])}] {r['title'][:80]} at {r['organizer'] or '?'}; {closes_in(r['deadline'])}"
                                         for r in rows) or "nothing open right now")

    def tool_add_opportunity_url(self, ctx, agent, task, a, n):
        from ..owner_actions import add_link
        out = add_link(ctx.db, f"{a.get('url', '')} {a.get('text', '')}".strip(), by=self._by(agent))
        return f"added {out['url']}; I'll check it and it shows up in /opportunities"

    def tool_draft_email(self, ctx, agent, task, a, n):
        import re
        mb = ctx.services.get("mailbox")
        if mb is None:
            return "Gmail isn't connected, so I can't create drafts. Here is the text instead."
        to = str(a.get("to") or "").strip()
        if not re.match(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", to, re.I):
            return "need a valid 'to' address for the draft"
        subject, body = str(a.get("subject") or "")[:200], str(a.get("body") or "")[:8000]
        res = ctx.act("email.draft", f"cadraft:{agent['id']}:{sha(to, subject, body)[:20]}",
                      lambda r: mb.create_draft(r.params["to"], r.params["subject"], r.params["body"]),
                      target=to, params={"to": to, "subject": subject, "body": body},
                      summary=f"{agent['name']}: draft to {to}: {subject[:60]}", wait_on_approval=False)
        return {"succeeded": "draft saved in Gmail (not sent; review and send it yourself)", "duplicate": "that draft already exists",
                "dry_run": "test mode: draft not created"}.get(res.status, f"draft {res.status}: {'; '.join(res.reasons)}")

    def tool_save_note(self, ctx, agent, task, a, n):
        vault = ctx.services.get("vault")
        if vault is None:
            return "no vault connected (NOESIS_VAULT_PATH)"
        from integrations.obsidian.vault import slugify
        title, body = str(a.get("title") or "Note")[:120], str(a.get("body") or "")[:20000]
        from datetime import datetime
        day = datetime.now().strftime("%Y-%m-%d")
        rel = f"00 Inbox/Agents/{slugify(agent['name'], 30)}/{day}-{slugify(title, 50)}.md"
        key = f"agentnote:{agent['id']}:{sha(title, body)[:16]}"
        res = ctx.act("vault.write", f"vault:{key}",
                      lambda r: {**vault.upsert_note(key, rel, {"title": title, "type": "agent-note", "agent": agent["name"]},
                                                     body, f"agent:{agent['id']}"), "evidence": rel},
                      target=rel, params={"title": title}, summary=f"note: {title}", wait_on_approval=False)
        return f"saved to {rel}" if res.status in ("succeeded", "duplicate", "dry_run") else f"note {res.status}"

    def tool_set_stage(self, ctx, agent, task, a, n):
        """Agents propose; the owner confirms with a tap (the same flow as email-detected stages)."""
        from ..owner_actions import clip, find_opportunity, ref
        from ..pipeline import LABELS, normalize_stage, propose_stage
        opp = find_opportunity(ctx.db, str(a.get("ref") or ""))
        stage = normalize_stage(str(a.get("stage") or ""))
        prop, _ = propose_stage(ctx.db, opp["id"], f"agent:{agent['id']}:{task.id}", stage, f"{agent['name']} suggested it")
        ctx.notify_owner(f"{agent['emoji']} {agent['name']} suggests moving “{clip(opp['title'], 80)}” to {LABELS[stage]}.\n"
                         f"ref {ref(prop['id'])}", key=f"stageprop:{prop['id']}", force_now=True,
                         buttons=[[{"text": f"✅ Move to {LABELS[stage]}"[:40], "data": f"g:{prop['id']}:y"},
                                   {"text": "✖ No", "data": f"g:{prop['id']}:n"}]])
        return f"asked you to confirm moving it to {LABELS[stage]} (tap the button in the main NOESIS chat)"

    def tool_company_dossier(self, ctx, agent, task, a, n):
        from ..dossier import build_dossier
        company = str(a.get("company") or "").strip()
        if not company:
            return "which company?"
        return build_dossier(ctx, company)["text"]

    def tool_tailor_resume(self, ctx, agent, task, a, n):
        from ..contracts import TaskSpec
        from ..owner_actions import find_opportunity
        opp = find_opportunity(ctx.db, str(a.get("ref") or ""))
        ctx.queue.enqueue(TaskSpec(bot_id="applications", kind="tailor_resume", payload={"opportunity_id": opp["id"]},
                                   requested_by=ctx.bot.id, idempotency_key=f"tailor:{opp['id']}:{int(now() // 600)}"))
        return "tailoring started: the ATS score and drafts arrive in the main NOESIS chat (/drafts)"

    def tool_find_referrals(self, ctx, agent, task, a, n):
        from ..referrals import plan, render
        return render(plan(ctx.db, str(a.get("ref") or a.get("company") or "")))

    def tool_log_debrief(self, ctx, agent, task, a, n):
        from ..debrief import log_debrief, split_questions
        qs = a.get("questions")
        qs = [str(q)[:400] for q in qs if str(q).strip()] if isinstance(qs, list) else split_questions(str(qs or ""))
        k = log_debrief(ctx.db, str(a.get("company") or ""), str(a.get("stage") or "") or None, qs, by=self._by(agent))
        return f"logged {k} question(s) to your bank"

    def tool_question_bank(self, ctx, agent, task, a, n):
        from ..debrief import bank
        rows = bank(ctx.db, str(a.get("company") or "") or None, str(a.get("topic") or "") or None, limit=15)
        return "\n".join(f"{r['company']} ({r['stage'] or '?'}, {r['topic']}): {r['question']}" for r in rows) or "the bank is empty"

    def tool_call_me(self, ctx, agent, task, a, n):
        from ..phone import call_owner
        msg = f"Message from your {agent['name']} agent. " + str(a.get("message") or "")[:500]
        out = call_owner(ctx, msg, "agent_request", key=f"agent:{agent['id']}:{task.id}:{n}")
        return {"placed": "calling you now", "dry_run": "test mode: no call placed"}.get(
            out["status"], f"no call ({out['detail']}); this message is the alert instead")

    def tool_ask_main_agent(self, ctx, agent, task, a, n):
        from ..contracts import TaskSpec
        text = str(a.get("text") or "").strip()[:2000]
        if not text:
            return "nothing to hand over"
        ctx.queue.enqueue(TaskSpec(bot_id="orchestrator", kind="objective", payload={"text": text, "from_agent": agent["id"]},
                                   requested_by=ctx.bot.id, idempotency_key=f"ask:{task.id}:{n}"))
        return "handed to the Chief of Staff; it reports back in the main NOESIS chat"

    # ---- voice + replies ---------------------------------------------------------------
    @staticmethod
    def _transcribe(ctx: AgentContext, path: str) -> str | None:
        from .. import stt
        p = Path(path)
        base = (Path(get_settings().data_dir) / "voice").resolve()
        if base not in p.resolve().parents or not p.exists():
            return None
        try:
            return stt.transcribe(p) or None
        except stt.SttUnavailable as e:
            ctx.log(f"voice note not transcribed: {e}", level="warning")
            return None
        finally:
            try:
                p.unlink()   # the audio isn't kept once it is text
            except OSError:
                pass

    def reply(self, ctx: AgentContext, agent: dict, task: Task, text: str, n: int = 1) -> None:
        if agent["builtin"]:
            tg = ctx.services.get("telegram")
            sender = (lambda r: tg.send_owner(r.params["text"])) if tg is not None else None
        else:
            bots = ctx.services.get("custom_bots")
            sender = (lambda r: bots.send(r.params["agent_id"], r.params["text"])) if bots is not None else None
        if sender is None:
            ctx.log("no Telegram sender for this agent; reply kept in its history only", level="warning")
            return
        res = ctx.act("telegram.custom_agent_reply", f"ca:{agent['id']}:{task.id}:{n}", sender, target="owner",
                      params={"agent_id": agent["id"], "text": f"{agent['emoji']} {text}" if agent["builtin"] else text},
                      summary=f"{agent['name']}: {text[:100]}", wait_on_approval=False)
        if res.status not in ("succeeded", "duplicate", "dry_run"):
            ctx.db.execute("UPDATE custom_agents SET last_error=? WHERE id=?", ("; ".join(res.reasons)[:300] or res.status,
                                                                                 agent["id"]))
