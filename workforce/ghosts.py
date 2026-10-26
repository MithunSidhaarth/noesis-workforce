"""Ghost-listing detector: roles that are probably not really hiring.

Signals (each with a plain reason the owner sees):
  * posted long ago and still up (60+ days; 120+ days counts more)
  * the same company re-posted the same role under a new link
  * the company never answered: 3+ of your applications there, none got a reply
  * no deadline, no official link, and only seen on an aggregator
The score is a hint (0..1), never a filter: a flagged listing is still shown, ranked lower.
"""
from __future__ import annotations

from .core.db import Database, dumps
from .core.util import now


def assess_ghost_risk(db: Database, opp_id: str) -> tuple[float, list[str]]:
    o = db.one("SELECT * FROM opportunities WHERE id=?", (opp_id,))
    if not o:
        return 0.0, []
    risk, why = 0.0, []
    posted = o.get("published_at")
    if posted:
        age = (now() - posted) / 86400
        if age >= 120:
            risk += 0.5
            why.append(f"posted {int(age)} days ago and still up")
        elif age >= 60:
            risk += 0.3
            why.append(f"posted {int(age)} days ago")
    if o.get("reposts"):
        risk += min(0.5, 0.25 * o["reposts"])
        why.append(f"re-posted {o['reposts']} time{'s' if o['reposts'] != 1 else ''} under a new link")
    org = (o.get("organizer") or "").strip()
    if org:
        r = db.one("SELECT COUNT(DISTINCT o.id) applied, COUNT(DISTINCT CASE WHEN s2.stage IN "
                   "('online_assessment','interview_1','interview_2','hr','offer','rejected') THEN o.id END) replied "
                   "FROM opportunities o JOIN application_stages s ON s.opportunity_id=o.id AND s.stage='applied' "
                   "LEFT JOIN application_stages s2 ON s2.opportunity_id=o.id WHERE lower(o.organizer)=lower(?) AND o.id!=?",
                   (org, opp_id)) or {}
        if (r.get("applied") or 0) >= 3 and not r.get("replied"):
            risk += 0.4
            why.append(f"{org} never replied to your {r['applied']} earlier applications")
    if not o.get("deadline") and o.get("verification_status") == "needs_review" and not o.get("official_url"):
        risk += 0.15
        why.append("no deadline and no official link")
    risk = round(min(1.0, risk), 2)
    db.execute("UPDATE opportunities SET ghost_risk=?, ghost_reasons_json=? WHERE id=?", (risk, dumps(why), opp_id))
    return risk, why


def reassess_all(db: Database, limit: int = 500) -> int:
    """Daily: ages change, and a company's silence becomes visible over time."""
    rows = db.all("SELECT id FROM opportunities WHERE stage IN ('found','shortlisted') ORDER BY discovered_at DESC LIMIT ?", (limit,))
    for r in rows:
        assess_ghost_risk(db, r["id"])
    return len(rows)
