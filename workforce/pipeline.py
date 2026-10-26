"""Application pipeline: every listing has one stage, from "found" to "selected".

found → shortlisted → preparing → applied → online_assessment → interview_1 →
interview_2 → hr → offer, or one of the closed ends: rejected, ghosted, withdrawn,
skipped. Each change is a row in application_stages (who, when, why, and the
OA / interview time when there is one). Stage changes come from the owner
(Telegram /stage, a drag on the dashboard board, the CLI), from the owner's own
actions (confirm, submit, outcome), or as a *proposal* from recruiter email that
the owner confirms with one tap. Bots never move a stage on their own.
"""
from __future__ import annotations

import re

from .core.db import Database
from .core.state import audit
from .core.util import new_id, now

STAGES = ["found", "shortlisted", "preparing", "applied", "online_assessment", "interview_1", "interview_2", "hr",
          "offer", "rejected", "ghosted", "withdrawn", "skipped"]
ACTIVE = ["shortlisted", "preparing", "applied", "online_assessment", "interview_1", "interview_2", "hr"]
CLOSED = ["offer", "rejected", "ghosted", "withdrawn", "skipped"]
BOARD = ACTIVE + ["offer", "rejected", "ghosted", "withdrawn"]   # the Kanban columns
LABELS = {"found": "Found", "shortlisted": "Shortlisted", "preparing": "Preparing", "applied": "Applied",
          "online_assessment": "Online assessment", "interview_1": "Interview 1", "interview_2": "Interview 2",
          "hr": "HR round", "offer": "Offer 🎉", "rejected": "Rejected", "ghosted": "No reply", "withdrawn": "Withdrawn",
          "skipped": "Skipped"}
ALIASES = {"oa": "online_assessment", "assessment": "online_assessment", "test": "online_assessment",
           "interview": "interview_1", "interview1": "interview_1", "i1": "interview_1", "r1": "interview_1",
           "interview2": "interview_2", "i2": "interview_2", "r2": "interview_2", "hr_round": "hr",
           "selected": "offer", "accepted": "offer", "reject": "rejected", "no_response": "ghosted", "noreply": "ghosted",
           "submitted": "applied", "apply": "applied", "shortlist": "shortlisted", "prepare": "preparing"}
FOLLOW_UP_DAYS = 14


class StageError(ValueError):
    """Bad input from the owner; the message is safe to show them."""


def normalize_stage(word: str) -> str:
    w = re.sub(r"[\s-]+", "_", (word or "").strip().lower())
    w = ALIASES.get(w, w)
    if w not in STAGES:
        raise StageError(f"I don't know the stage '{word[:30]}'. Use one of: " + ", ".join(s for s in STAGES if s != "found") + ".")
    return w


def set_stage(db: Database, opp_id: str, stage: str, by: str, note: str = "", event_at: float | None = None) -> dict:
    """Move a listing to `stage` (any stage may follow any other: real hiring is messy,
    and the owner is the authority). Same stage again only updates the event time."""
    stage = normalize_stage(stage)
    opp = db.one("SELECT id, stage, title FROM opportunities WHERE id=?", (opp_id,))
    if not opp:
        raise StageError("That listing doesn't exist any more.")
    t = now()
    if opp["stage"] == stage and event_at is None:
        return opp
    db.execute("INSERT INTO application_stages(id, opportunity_id, from_stage, stage, at, event_at, note, by) VALUES (?,?,?,?,?,?,?,?)",
               (new_id("stg"), opp_id, opp["stage"], stage, t, event_at, (note or "")[:500], by))
    db.execute("UPDATE opportunities SET stage=?, stage_updated_at=?, next_event_at=? WHERE id=?",
               (stage, t, event_at if stage in ("online_assessment", "interview_1", "interview_2", "hr") else None, opp_id))
    audit(db, by, "stage.changed", None, {"opportunity_id": opp_id, "from": opp["stage"], "to": stage})
    if stage in CLOSED:
        from .owner_actions import cancel_opportunity_reminders
        cancel_opportunity_reminders(db, opp_id)
    return db.one("SELECT * FROM opportunities WHERE id=?", (opp_id,))


def history(db: Database, opp_id: str) -> list[dict]:
    return db.all("SELECT from_stage, stage, at, event_at, note, by FROM application_stages WHERE opportunity_id=? ORDER BY at",
                  (opp_id,))


def board(db: Database) -> dict[str, list[dict]]:
    cols: dict[str, list[dict]] = {s: [] for s in BOARD}
    for r in db.all("SELECT id, title, organizer, stage, stage_updated_at, next_event_at, deadline, official_url, relevance_score "
                    f"FROM opportunities WHERE stage IN ({','.join('?' * len(BOARD))}) ORDER BY stage_updated_at DESC",
                    tuple(BOARD)):
        cols[r["stage"]].append(r)
    return cols


def funnel(db: Database, since: float | None = None) -> dict:
    """How many listings ever reached each step (from the stage history, so a rejection
    after an interview still counts as an interview)."""
    since = since or 0
    reached = lambda stages: db.scalar(  # noqa: E731
        f"SELECT COUNT(DISTINCT opportunity_id) FROM application_stages WHERE stage IN ({','.join('?' * len(stages))}) AND at>=?",
        (*stages, since)) or 0
    return {
        "found": db.scalar("SELECT COUNT(*) FROM opportunities WHERE discovered_at>=?", (since,)) or 0,
        "applied": reached(["applied"]),
        "online_assessment": reached(["online_assessment"]),
        "interview": reached(["interview_1", "interview_2", "hr"]),
        "offer": reached(["offer"]),
        "rejected": reached(["rejected"]),
    }


def response_rates(db: Database, group: str = "source") -> list[dict]:
    """Per source or per company: applied, got any reply (OA/interview/offer/rejection), rate."""
    col = {"source": "o.discovery_source", "company": "COALESCE(o.organizer, '(unknown)')"}[group]
    rows = db.all(
        f"SELECT {col} name, COUNT(DISTINCT o.id) applied, "
        "COUNT(DISTINCT CASE WHEN s2.stage IN ('online_assessment','interview_1','interview_2','hr','offer','rejected') "
        "THEN o.id END) replied FROM opportunities o JOIN application_stages s ON s.opportunity_id=o.id AND s.stage='applied' "
        "LEFT JOIN application_stages s2 ON s2.opportunity_id=o.id GROUP BY name ORDER BY applied DESC")
    for r in rows:
        r["rate"] = round(r["replied"] / r["applied"], 2) if r["applied"] else 0.0
    return rows


def stale_applied(db: Database, days: int = FOLLOW_UP_DAYS) -> list[dict]:
    """Applied this long ago, still at 'applied': time to follow up (or call it ghosted)."""
    return db.all("SELECT id, title, organizer, stage_updated_at FROM opportunities WHERE stage='applied' AND stage_updated_at<=?",
                  (now() - days * 86400,))


# ---- recruiter email -> proposed stage ---------------------------------------
EMAIL_RULES = [
    ("offer", re.compile(r"\b(offer letter|pleased to offer|offer of internship|we are delighted to offer|congratulations[^.]{0,40}selected)\b", re.I)),
    ("rejected", re.compile(r"\b(regret to inform|unfortunately[^.]{0,60}(not|unable)|not (been )?selected|decided not to (move|proceed)|"
                            r"will not be moving forward|other candidates)\b", re.I)),
    ("hr", re.compile(r"\b(hr (round|discussion|interview)|final round|culture fit)\b", re.I)),
    ("interview_1", re.compile(r"\b(interview|technical round|schedule (a|your) (call|chat)|next round)\b", re.I)),
    ("online_assessment", re.compile(r"\b(online assessment|coding (test|challenge|assessment)|hackerrank|hackerearth|codility|"
                                     r"codesignal|mettl|assessment link|take-home)\b", re.I)),
]


def detect_stage(subject: str, snippet: str) -> tuple[str, str] | None:
    """(stage, matched phrase) for a recruiter email, or None. Order matters: an offer or a
    rejection mentions 'interview' too."""
    text = f"{subject or ''} {snippet or ''}"
    for stage, rx in EMAIL_RULES:
        m = rx.search(text)
        if m:
            return stage, m.group(0)
    return None


def match_opportunity(db: Database, sender: str, subject: str, snippet: str) -> dict | None:
    """The listing an email is about: an applied-or-later listing whose company name or
    application domain appears in the sender or subject. None when unsure (never guess)."""
    text = f"{sender} {subject} {snippet}".lower()
    cands = []
    for o in db.all("SELECT id, title, organizer, official_url, stage FROM opportunities WHERE stage IN "
                    "('applied','online_assessment','interview_1','interview_2','hr','preparing')"):
        org = (o["organizer"] or "").strip().lower()
        dom = ""
        if o["official_url"]:
            from .sources.parsing import domain
            dom = domain(o["official_url"]).split(".")[-2] if "." in domain(o["official_url"]) else ""
        hit = (len(org) >= 3 and re.search(rf"(?<![a-z0-9]){re.escape(org)}(?![a-z0-9])", text)) or \
              (len(dom) >= 4 and dom not in ("greenhouse", "lever", "ashbyhq", "google", "linkedin") and dom in text)
        if hit:
            cands.append(o)
    return cands[0] if len(cands) == 1 else None


def propose_stage(db: Database, opp_id: str, email_id: str | None, stage: str, evidence: str) -> tuple[dict, bool]:
    cur = db.execute("INSERT OR IGNORE INTO stage_proposals(id, opportunity_id, email_id, stage, evidence, created_at) "
                     "VALUES (?,?,?,?,?,?)", (new_id("prop"), opp_id, email_id, stage, (evidence or "")[:300], now()))
    row = db.one("SELECT * FROM stage_proposals WHERE opportunity_id=? AND email_id IS ? AND stage=?", (opp_id, email_id, stage))
    return row, bool(cur.rowcount)


def decide_proposal(db: Database, prop_id: str, accept: bool, by: str) -> dict:
    p = db.one("SELECT * FROM stage_proposals WHERE id=? OR id LIKE ?", (prop_id, f"%{prop_id}"))
    if not p:
        raise StageError("That suggestion no longer exists.")
    if p["status"] != "pending":
        raise StageError(f"That suggestion was already {p['status']}.")
    db.execute("UPDATE stage_proposals SET status=?, decided_at=? WHERE id=?", ("confirmed" if accept else "dismissed", now(), p["id"]))
    if accept:
        set_stage(db, p["opportunity_id"], p["stage"], by=by, note=f"from email: {p['evidence']}")
    return db.one("SELECT * FROM stage_proposals WHERE id=?", (p["id"],))


def pending_proposals(db: Database) -> list[dict]:
    return db.all("SELECT p.*, o.title, o.organizer FROM stage_proposals p JOIN opportunities o ON o.id=p.opportunity_id "
                  "WHERE p.status='pending' ORDER BY p.created_at")

