"""Post-interview debrief and your personal question bank.

After an OA or interview you send what was asked (/debrief <ref> q1; q2; q3). It is
stored by company, stage and topic, feeds the prep plan the next time that company
moves you to a round, and the Interview Coach agent drills you on it.
"""
from __future__ import annotations

import re

from .core.db import Database
from .core.state import audit
from .core.util import new_id, now
from .owner_actions import OwnerActionError, find_opportunity

TOPICS = [
    ("system design", re.compile(r"\b(design|scal|architecture|load balanc|cache|caching|sharding|microservice)", re.I)),
    ("dsa", re.compile(r"\b(array|string|tree|graph|linked list|dp|dynamic programming|binary search|heap|stack|queue|"
                       r"sort|hash|leetcode|complexity|recursion|bfs|dfs|two pointer|sliding window)", re.I)),
    ("ml", re.compile(r"\b(model|overfit|regulari|gradient|loss|neural|transformer|precision|recall|ml|llm|embedding|"
                      r"bias|variance|classification|regression)", re.I)),
    ("sql / dbms", re.compile(r"\b(sql|join|index|normali|transaction|acid|database|dbms|query)", re.I)),
    ("cs fundamentals", re.compile(r"\b(os|process|thread|deadlock|memory|paging|tcp|http|network|oop|polymorphism|"
                                   r"inheritance)\b", re.I)),
    ("behavioural", re.compile(r"\b(tell me about|yourself|conflict|weakness|strength|why (do you|this|our)|challenge|team|"
                               r"failure|proud)", re.I)),
    ("projects", re.compile(r"\b(project|internship|resume|walk me through)", re.I)),
]


def topic_of(question: str) -> str:
    for name, rx in TOPICS:
        if rx.search(question):
            return name
    return "other"


def split_questions(text: str) -> list[str]:
    parts = re.split(r"\s*(?:;|\n|\|)\s*", text or "")
    return [p.strip(" -•*\t")[:400] for p in parts if len(p.strip(" -•*\t")) >= 6][:30]


def log_debrief(db: Database, company: str, stage: str | None, questions: list[str], opportunity_id: str | None = None,
                notes: str | None = None, went_well: bool | None = None, by: str = "owner") -> int:
    company = (company or "").strip()[:80]
    if not company or not questions:
        raise OwnerActionError("Send the company (or ref) and at least one question, e.g. "
                               "/debrief 3f9a1c reverse a linked list; design a URL shortener")
    n = 0
    for q in questions:
        db.execute("INSERT INTO interview_questions(id, opportunity_id, company, stage, question, topic, my_answer_notes, "
                   "went_well, asked_at) VALUES (?,?,?,?,?,?,?,?,?)",
                   (new_id("iq"), opportunity_id, company, stage, q[:400], topic_of(q), notes,
                    None if went_well is None else int(bool(went_well)), now()))
        n += 1
    audit(db, by, "debrief.logged", None, {"company": company, "questions": n})
    return n


def debrief_from_text(db: Database, text: str, by: str = "owner") -> dict:
    """'/debrief <ref|company> q1; q2; q3'. With a ref, the company and stage come from the listing."""
    head, _, rest = (text or "").strip().partition(" ")
    if not head:
        raise OwnerActionError("Use /debrief <ref or company> question 1; question 2; ...")
    opp = None
    try:
        opp = find_opportunity(db, head)
    except OwnerActionError:
        pass
    company = (opp or {}).get("organizer") or head.replace("_", " ")
    stage = (opp or {}).get("stage")
    qs = split_questions(rest)
    n = log_debrief(db, company, stage, qs, opportunity_id=(opp or {}).get("id"), by=by)
    return {"company": company, "stage": stage, "logged": n, "topics": sorted({topic_of(q) for q in qs})}


def bank(db: Database, company: str | None = None, topic: str | None = None, limit: int = 20) -> list[dict]:
    where, args = [], []
    if company:
        where.append("lower(company)=lower(?)")
        args.append(company.strip())
    if topic:
        where.append("topic=?")
        args.append(topic.strip().lower())
    sql = "SELECT id, company, stage, question, topic, went_well, asked_at FROM interview_questions"
    if where:
        sql += " WHERE " + " AND ".join(where)
    return db.all(sql + " ORDER BY asked_at DESC LIMIT ?", (*args, limit))


def bank_stats(db: Database) -> list[dict]:
    return db.all("SELECT company, COUNT(*) AS n, MAX(asked_at) AS last FROM interview_questions GROUP BY lower(company) "
                  "ORDER BY n DESC LIMIT 20")
