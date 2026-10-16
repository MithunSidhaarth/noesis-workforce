"""Communications Agent.

Email: RECEIVED -> CLASSIFIED -> PRIORITIZED -> ACTION_IDENTIFIED -> DRAFTED -> POLICY_CHECKED -> SENT_OR_APPROVAL_REQUIRED -> TRACKED
Starts read + draft only: email.send and calendar writes need policy switches
and, until then, your approval. Availability comes only from the real calendar.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..core.db import dumps
from ..core.queue import Task
from ..core.settings import get_settings
from ..core.state import set_integration_status
from ..core.util import now
from .base import Agent, AgentContext, PermanentError, register, untrusted

RULES = [
    ("meeting_request", r"\b(meet|meeting|call|catch up|schedule|availability|available|calendar invite|zoom|google meet)\b", "high"),
    ("deadline", r"\b(deadline|due (?:by|on)|by (?:monday|tuesday|wednesday|thursday|friday|eod|tomorrow)|last date)\b", "high"),
    ("opportunity", r"\b(internship|fellowship|hackathon|scholarship|selected|shortlisted|interview|offer letter)\b", "high"),
    ("action_required", r"\b(please (?:confirm|reply|respond|submit|review)|action required|kindly|request you)\b", "medium"),
    ("receipt", r"\b(receipt|invoice|payment|order (?:confirmed|shipped))\b", "low"),
]


def classify_email(m: dict) -> tuple[str, str]:
    if m.get("list_unsubscribe"):
        return "newsletter", "low"
    text = f"{m.get('subject', '')} {m.get('snippet', '')}".lower()
    for cat, rx, prio in RULES:
        if re.search(rx, text):
            return cat, prio
    return "fyi", "low"


def suggest_slots(busy: list[tuple[float, float]], start: float, days: int, tz: str, duration_min: int = 30,
                  work_hours: tuple[int, int] = (10, 18), limit: int = 5) -> list[float]:
    """Free slots computed only from the calendar's busy intervals."""
    out = []
    z = ZoneInfo(tz)
    d0 = datetime.fromtimestamp(start, z)
    for i in range(days):
        day = (d0 + timedelta(days=i)).replace(minute=0, second=0, microsecond=0)
        if day.weekday() >= 5:
            continue
        for h in range(work_hours[0], work_hours[1]):
            for mm in (0, 30):
                s = day.replace(hour=h, minute=mm).timestamp()
                e = s + duration_min * 60
                if s <= start or e > day.replace(hour=work_hours[1], minute=0).timestamp():
                    continue
                if all(e <= b0 or s >= b1 for b0, b1 in busy):
                    out.append(s)
                    if len(out) >= limit:
                        return out
    return out


@register("communications")
class CommunicationsAgent(Agent):
    accepts = {"suggest_meeting_slots", "draft_reply"}

    def handlers(self):
        return {"check_inbox": self.check_inbox, "draft_reply": self.draft_reply,
                "suggest_meeting_slots": self.suggest_meeting_slots, "send_reply": self.send_reply}

    def _mail(self, ctx: AgentContext):
        return ctx.services.get("mailbox")

    def check_inbox(self, ctx: AgentContext, task: Task) -> dict:
        mb = self._mail(ctx)
        if mb is None or not mb.configured():
            set_integration_status(ctx.db, "gmail", "not_configured", "Gmail OAuth not set up")
            return {"skipped": "gmail not configured"}
        msgs = mb.list_recent()
        set_integration_status(ctx.db, "gmail", "ok", f"{len(msgs)} recent messages")
        important = []
        for m in msgs:
            if ctx.db.scalar("SELECT 1 FROM emails WHERE id=?", (m["id"],)):
                continue  # already tracked: no duplicate actions
            cat, prio = classify_email(m)
            actions = []
            if cat == "meeting_request":
                actions.append("suggest_slots")
            if cat in ("meeting_request", "action_required", "deadline", "opportunity"):
                actions.append("draft_reply")
            state = "ACTION_IDENTIFIED" if actions else "TRACKED"
            ctx.db.execute("INSERT INTO emails(id, thread_id, sender, subject, snippet, received_at, state, category, priority, actions_json, "
                           "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                           (m["id"], m.get("thread_id"), m["from"][:200], m["subject"][:300], m["snippet"][:500], m.get("received_at"),
                            state, cat, prio, dumps(actions), now(), now()))
            if "draft_reply" in actions:
                ctx.enqueue_self("draft_reply", {"email_id": m["id"]}, idempotency_key=f"draft:{m['id']}")
            if prio == "high":
                important.append(f"• [{cat}] {m['subject'][:80]} — {m['from'][:60]}")
        if important:
            ctx.notify_owner("📬 Important email:\n" + "\n".join(important[:10]), key=f"mail:{','.join(sorted(important))[:200]}", urgent=True)
        return {"fetched": len(msgs), "important": len(important)}

    def suggest_meeting_slots(self, ctx: AgentContext, task: Task) -> dict:
        cal = ctx.services.get("calendar")
        if cal is None or not cal.configured():
            return {"slots": [], "note": "calendar not connected; availability unknown (not guessed)"}
        start = now()
        days = int(task.payload.get("days", 5))
        busy = cal.busy(start, start + days * 86400)
        slots = suggest_slots(busy, start, days, get_settings().timezone, int(task.payload.get("duration_min", 30)))
        return {"slots": [datetime.fromtimestamp(s, ZoneInfo(get_settings().timezone)).isoformat() for s in slots]}

    def draft_reply(self, ctx: AgentContext, task: Task) -> dict:
        e = ctx.db.one("SELECT * FROM emails WHERE id=?", (task.payload["email_id"],))
        if not e:
            raise PermanentError("email not tracked")
        slots_txt = ""
        if e["category"] == "meeting_request":
            slots = self.suggest_meeting_slots(ctx, task).get("slots", [])
            slots_txt = ("Possible times (from my calendar): " + ", ".join(s[:16].replace("T", " ") for s in slots[:3])) if slots else ""
        if ctx.has_model("write"):
            prompt = ("Draft a short, polite reply on the owner's behalf. Do not commit to anything, do not invent facts or "
                      f"availability. {('Offer only these times: ' + slots_txt) if slots_txt else 'Do not propose times.'}\n"
                      + untrusted(f"From: {e['sender']}\nSubject: {e['subject']}\n{e['snippet']}", 4000))
            try:
                body = ctx.llm("write", prompt, max_tokens=400).text.strip()
            except Exception as ex:  # noqa: BLE001
                ctx.log(f"draft model unavailable: {ex}", level="warning")
                body = ""
        else:
            body = ""
        if not body:
            body = "Hi,\n\nThanks for your email. I'll get back to you shortly." + (f"\n\n{slots_txt}" if slots_txt else "") + "\n\nBest,"
        ctx.db.execute("UPDATE emails SET draft=?, state='DRAFTED', updated_at=? WHERE id=?", (body, now(), e["id"]))
        mb = self._mail(ctx)
        to = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", e["sender"] or "")
        if mb is not None and mb.configured() and to:
            res = ctx.act("email.draft", f"gdraft:{e['id']}", lambda r: mb.create_draft(to.group(0), "Re: " + e["subject"], body, e["thread_id"]),
                          target=to.group(0), summary=f"Draft reply to {e['subject'][:60]}", wait_on_approval=False)
            ctx.db.execute("UPDATE emails SET state='POLICY_CHECKED', updated_at=? WHERE id=?", (now(), e["id"]))
            return {"email_id": e["id"], "draft": res.status}
        return {"email_id": e["id"], "draft": "stored locally (gmail not configured)"}

    def send_reply(self, ctx: AgentContext, task: Task) -> dict:
        e = ctx.db.one("SELECT * FROM emails WHERE id=?", (task.payload["email_id"],))
        if not e or not e["draft"]:
            raise PermanentError("no draft to send")
        if task.payload.get("approval_denied"):
            ctx.db.execute("UPDATE emails SET state='TRACKED', updated_at=? WHERE id=?", (now(), e["id"]))
            return {"email_id": e["id"], "denied": True}
        mb = self._mail(ctx)
        to = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", e["sender"] or "")
        if not to:
            raise PermanentError("no recipient address")

        def executor(req):
            if mb is None or not mb.configured():
                from ..policies.engine import IntegrationRevoked
                ex = IntegrationRevoked("gmail not configured")
                ex.integration = "gmail"
                raise ex
            return mb.send(to.group(0), "Re: " + e["subject"], e["draft"], e["thread_id"])

        res = ctx.act("email.send", f"send:{e['id']}", executor, target=to.group(0), params={"subject": "Re: " + e["subject"]},
                      attributes={"category": e["category"]}, summary=f"Send reply: {e['subject'][:60]}")
        # honest states: only a real send is TRACKED; a dry run can be sent for real later
        state = {"succeeded": "TRACKED", "duplicate": "TRACKED", "dry_run": "DRY_RUN", "blocked": "BLOCKED"}.get(
            res.status, "APPROVAL_REQUIRED")
        ctx.db.execute("UPDATE emails SET state=?, follow_up_at=?, updated_at=? WHERE id=?",
                       (state, now() + 5 * 86400 if state == "TRACKED" else None, now(), e["id"]))
        return {"email_id": e["id"], "action": res.status, "evidence": res.evidence}
