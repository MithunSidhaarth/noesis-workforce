"""OA and interview prep linked to the company.

When a listing moves to online_assessment or an interview stage, the owner gets a
short plan: practice sessions spaced before the date (each a reminder), topics
picked from the role, the questions the owner logged after earlier rounds at the
same company (/debrief), and, when a model is available, what is generally known
about that company's rounds, labelled as general knowledge that may be outdated.
"""
from __future__ import annotations

import re

from .core.util import new_id, now

BASE_TOPICS = {
    "online_assessment": ["arrays and hashing", "two pointers and sliding window", "binary search", "trees and graphs (BFS/DFS)",
                          "dynamic programming", "greedy", "stacks and heaps"],
    "interview": ["explain your best project end to end", "DSA: medium problems out loud", "OOP and low-level design",
                  "DBMS, OS and networks basics", "behavioural: STAR stories"],
    "hr": ["why this company", "tell me about yourself (90 seconds)", "strengths, weaknesses, conflict story",
           "availability, stipend expectations, relocation"],
}
ROLE_TOPICS = [
    (re.compile(r"\b(ml|machine learning|ai|data scien|nlp|vision|llm)\b", re.I),
     ["ML fundamentals: bias/variance, metrics, overfitting", "probability and statistics", "pandas / numpy coding"]),
    (re.compile(r"\b(data|analyst|analytics|sql)\b", re.I), ["SQL: joins, group by, window functions", "case questions"]),
    (re.compile(r"\b(frontend|front-end|react|web|ui)\b", re.I), ["JavaScript closures, promises, event loop", "React state and rendering"]),
    (re.compile(r"\b(backend|back-end|sde|software|platform)\b", re.I), ["REST API design", "SQL and indexing basics"]),
]
LEETCODE_TAGS = {"arrays and hashing": "hash-table", "two pointers and sliding window": "sliding-window",
                 "binary search": "binary-search", "trees and graphs (BFS/DFS)": "breadth-first-search",
                 "dynamic programming": "dynamic-programming", "greedy": "greedy", "stacks and heaps": "heap-priority-queue"}


def topics_for(opp: dict) -> list[str]:
    stage = opp.get("stage") or "online_assessment"
    key = "online_assessment" if stage == "online_assessment" else "hr" if stage == "hr" else "interview"
    out = list(BASE_TOPICS[key])
    text = f"{opp.get('title', '')} {opp.get('summary') or ''}"
    for rx, extra in ROLE_TOPICS:
        if rx.search(text):
            out = extra + out
    return list(dict.fromkeys(out))


def company_questions(db, company: str | None, limit: int = 10) -> list[dict]:
    if not company:
        return []
    return db.all("SELECT question, topic, stage, went_well FROM interview_questions WHERE lower(company)=lower(?) "
                  "ORDER BY asked_at DESC LIMIT ?", (company, limit))


def prep_plan(ctx, opp: dict) -> dict:
    """Builds the plan, schedules practice reminders, tells the owner. Returns the plan."""
    from .agents.base import untrusted
    from .owner_actions import clip, day_label, plural, when_label
    db = ctx.db
    topics = topics_for(opp)
    bank = company_questions(db, opp.get("organizer"))
    general = []
    if ctx.has_model("write") and opp.get("organizer"):
        prompt = ("A student has an upcoming " + (opp.get("stage") or "assessment").replace("_", " ") + " for the role below. "
                  "From general public knowledge, list up to 4 short points on what this company's rounds usually focus "
                  "on for interns (format, topics, difficulty). If you don't know this company, return an empty list. "
                  'Reply JSON: {"points": [..]}.\n' + untrusted(f"{opp['title']} at {opp['organizer']}", 400))
        try:
            data = ctx.llm("write", prompt, max_tokens=300).json()
            general = [str(p)[:160] for p in (data.get("points") or [])][:4] if isinstance(data, dict) else []
        except Exception as e:  # noqa: BLE001
            ctx.log(f"company prep notes unavailable: {e}", level="debug")
    event = opp.get("next_event_at")
    start = now() + 3 * 3600
    end = event - 6 * 3600 if event and event - 6 * 3600 > start else start + 4 * 86400
    n = max(1, min(5, int((end - start) // 86400) + 1))
    step = (end - start) / max(1, n - 1) if n > 1 else 0
    sessions = []
    for i in range(n):
        topic = topics[i % len(topics)]
        due = start + i * step
        tag = LEETCODE_TAGS.get(topic)
        link = f" https://leetcode.com/tag/{tag}/" if tag else ""
        title = f"Prep for {clip(opp.get('organizer') or opp['title'], 40)}: {topic} (45 min).{link}"
        cur = db.execute("INSERT OR IGNORE INTO reminders(id, title, due_at, source, ref_id, dedupe_key, created_at) VALUES (?,?,?,?,?,?,?)",
                         (new_id("rem"), title, due, "applications", opp["id"], f"prep:{opp['id']}:{opp.get('stage')}:{i}", now()))
        if cur.rowcount:
            sessions.append({"due": due, "topic": topic})
    lines = [f"🧠 Prep plan: {clip(opp['title'], 70)}" + (f" at {opp['organizer']}" if opp.get("organizer") else ""), ""]
    if event:
        lines.append(f"{(opp.get('stage') or '').replace('_', ' ').capitalize()} on {when_label(event)}.")
    lines.append(f"{plural(len(sessions), 'practice session')} scheduled, starting {day_label(start)}:")
    lines += [f"• {s['topic']}" for s in sessions]
    if bank:
        lines += ["", "From your own debriefs at this company:"] + [f"• {clip(q['question'], 100)}" for q in bank[:5]]
    if general:
        lines += ["", "What's generally known (may be outdated):"] + [f"• {p}" for p in general]
    lines += ["", "Practise out loud with your Interview Coach bot. After the round, send /debrief to log the questions."]
    ctx.notify_owner("\n".join(lines), key=f"prep:{opp['id']}:{opp.get('stage')}", force_now=True)
    plan = {"topics": topics, "sessions": sessions, "bank": bank, "general": general}
    ctx.memory_set(f"prep:{opp['id']}", {**plan, "stage": opp.get("stage")})
    ctx.log(f"prep plan for {opp['id']}", sessions=len(sessions))
    return plan
