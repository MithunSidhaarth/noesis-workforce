"""Outreach Agent: prospects, personalized drafts, bounded follow-ups, opt-outs.

Personalization uses only facts recorded on the contact (with a source URL).
No fabricated familiarity, referrals or deceptive subjects; the sender identity
from policies/outreach.yaml is always used. Sending is draft-only by default.
"""
from __future__ import annotations

import re

from ..core.queue import Task
from ..core.util import new_id, now
from ..sources.registry import load_profile
from .base import Agent, AgentContext, PermanentError, register, untrusted

DECEPTIVE = [r"^re:", r"^fwd?:", r"\burgent\b", r"\bas promised\b", r"\bfollowing up on our (?:call|chat|conversation)\b",
             r"\b(?:\w+) (?:referred|recommended) me\b", r"\bwe met\b"]


def relevance(contact: dict, profile: dict) -> tuple[float, str]:
    text = f"{contact.get('role') or ''} {contact.get('organization') or ''} {contact.get('notes') or ''}".lower()
    hits = [i for i in profile.get("interests", []) if i.lower() in text]
    score = min(1.0, 0.25 * len(hits) + (0.25 if contact.get("source_url") else 0))
    return round(score, 2), ("matches " + ", ".join(hits)) if hits else "no interest overlap found"


def check_honesty(subject: str, body: str) -> list[str]:
    problems = [f"deceptive pattern: {p}" for p in DECEPTIVE if re.search(p, subject, re.I) or re.search(p, body, re.I)]
    return problems


@register("outreach")
class OutreachAgent(Agent):
    accepts = {"draft_outreach"}

    def handlers(self):
        return {"draft_outreach": self.draft, "send_outreach": self.send, "process_follow_ups": self.follow_ups,
                "weekly_report": self.weekly_report}

    def draft(self, ctx: AgentContext, task: Task) -> dict:
        c = ctx.db.one("SELECT * FROM contacts WHERE id=?", (task.payload["contact_id"],))
        if not c:
            raise PermanentError("contact not found")
        if c["status"] in ("opted_out", "declined", "bounced") or ctx.db.scalar("SELECT 1 FROM suppression_list WHERE email=?", (c["email"],)):
            return {"contact_id": c["id"], "skipped": "suppressed"}
        profile = load_profile()
        score, why = relevance(c, profile)
        ctx.db.execute("UPDATE contacts SET relevance=?, relevance_rationale=?, updated_at=? WHERE id=?", (score, why, now(), c["id"]))
        sender = ctx.services.get("policies_outreach", {}).get("sender_identity") or profile.get("name") or "the sender"
        kind = task.payload.get("kind", "initial")
        if ctx.has_model("write"):
            prompt = (f"Write a short cold email from {sender} to {c['name'] or 'the recipient'} ({c['role'] or ''} at {c['organization'] or ''}). "
                      "Use ONLY the facts below for personalization; no claims of prior contact, referrals or shared history; "
                      "a plain, accurate subject. Include a one-line opt-out sentence. Reply JSON {\"subject\": str, \"body\": str}.\n"
                      f"Objective: {task.payload.get('objective', 'explore collaboration or opportunities')}\n"
                      + untrusted(f"Contact notes: {c['notes']}\nSource: {c['source_url']}", 3000))
            try:
                d = ctx.llm("write", prompt, max_tokens=500).json()
                subject, body = str(d["subject"])[:120], str(d["body"])
            except Exception as e:  # noqa: BLE001
                ctx.log(f"model draft failed: {e}", level="warning")
                subject = body = ""
        else:
            subject = body = ""
        if not body:
            subject = f"Question about {c['organization'] or 'your work'}"
            note = f"I came across {c['source_url']} and " if c["source_url"] else ""
            body = (f"Hi {c['name'] or 'there'},\n\nI'm {sender}. {note}I'd like to ask whether you'd be open to a short "
                    f"conversation about {task.payload.get('objective', 'opportunities in your team')}.\n\n"
                    "If this isn't relevant, just reply 'no thanks' and I won't follow up.\n\nBest,\n" + sender)
        problems = check_honesty(subject, body)
        if problems:
            raise PermanentError("draft failed honesty checks: " + "; ".join(problems))
        mid = new_id("om")
        ctx.db.execute("INSERT INTO outreach_messages(id, contact_id, kind, subject, body, status, created_at) VALUES (?,?,?,?,?,?,?)",
                       (mid, c["id"], kind, subject, body, "draft", now()))
        ctx.db.execute("UPDATE contacts SET status=CASE WHEN status='prospect' THEN 'drafted' ELSE status END, updated_at=? WHERE id=?",
                       (now(), c["id"]))
        return {"contact_id": c["id"], "message_id": mid, "relevance": score}

    def send(self, ctx: AgentContext, task: Task) -> dict:
        m = ctx.db.one("SELECT * FROM outreach_messages WHERE id=?", (task.payload["message_id"],))
        c = ctx.db.one("SELECT * FROM contacts WHERE id=?", (m["contact_id"],))
        if task.payload.get("approval_denied"):
            ctx.db.execute("UPDATE outreach_messages SET status='blocked' WHERE id=?", (m["id"],))
            return {"message_id": m["id"], "denied": True}
        mb = ctx.services.get("mailbox")

        def executor(req):
            if mb is None or not mb.configured():
                from ..policies.engine import IntegrationRevoked
                e = IntegrationRevoked("gmail not configured for outreach")
                e.integration = "gmail"
                raise e
            return mb.send(c["email"], m["subject"], m["body"])

        res = ctx.act("outreach.send", f"outreach:{m['id']}", executor, target=c["email"],
                      attributes={"kind": m["kind"], "follow_ups_sent": c["follow_ups_sent"], "relevance": c["relevance"]},
                      summary=f"Send {m['kind']} to {c['name'] or c['email']}: {m['subject']}")
        if res.status == "succeeded":
            pol = ctx.services.get("policies_outreach", {})
            ctx.db.execute("UPDATE outreach_messages SET status='sent', sent_at=? WHERE id=?", (now(), m["id"]))
            ctx.db.execute("UPDATE contacts SET status='contacted', last_contacted=?, follow_ups_sent=follow_ups_sent+?, next_follow_up=?, "
                           "updated_at=? WHERE id=?", (now(), 1 if m["kind"] == "follow_up" else 0,
                                                       now() + pol.get("follow_up_after_days", 5) * 86400, now(), c["id"]))
        elif res.status == "blocked":
            ctx.db.execute("UPDATE outreach_messages SET status='blocked' WHERE id=?", (m["id"],))
        return {"message_id": m["id"], "action": res.status, "reasons": res.reasons}

    def follow_ups(self, ctx: AgentContext, task: Task) -> dict:
        pol = ctx.services.get("policies_outreach", {"max_follow_ups": 2})
        due = ctx.db.all("SELECT * FROM contacts WHERE status='contacted' AND next_follow_up<=? AND follow_ups_sent<?",
                         (now(), pol.get("max_follow_ups", 2)))
        for c in due:
            ctx.enqueue_self("draft_outreach", {"contact_id": c["id"], "kind": "follow_up"},
                             idempotency_key=f"fu:{c['id']}:{c['follow_ups_sent']}")
        return {"follow_ups_drafted": len(due)}

    def weekly_report(self, ctx: AgentContext, task: Task) -> dict:
        week = now() - 7 * 86400
        stats = {r["status"]: r["n"] for r in ctx.db.all("SELECT status, COUNT(*) n FROM contacts GROUP BY status")}
        sent = ctx.db.scalar("SELECT COUNT(*) FROM outreach_messages WHERE status='sent' AND sent_at>=?", (week,))
        drafts = ctx.db.scalar("SELECT COUNT(*) FROM outreach_messages WHERE status='draft'")
        text = (f"📈 Outreach this week\n\n{sent} message{'s' if sent != 1 else ''} sent, "
                f"{drafts} draft{'s' if drafts != 1 else ''} waiting for review.\n"
                "Contacts: " + (", ".join(f"{v} {k.replace('_', ' ')}" for k, v in stats.items()) or "none yet") + ".")
        ctx.notify_owner(text, key=f"outreach-week:{int(now() // (7 * 86400))}")
        return {"sent": sent, "drafts": drafts, "pipeline": stats}


def mark_opted_out(db, email: str, reason: str, by: str) -> None:
    from ..core.state import audit
    email = email.lower().strip()
    db.execute("INSERT OR IGNORE INTO suppression_list(email, reason, created_at) VALUES (?,?,?)", (email, reason, now()))
    db.execute("UPDATE contacts SET status='opted_out', next_follow_up=NULL, updated_at=? WHERE email=?", (now(), email))
    audit(db, by, "outreach.opt_out", None, {"email": email, "reason": reason})
