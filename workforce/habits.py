"""Daily apply quota and streaks.

The quota (profile `daily_apply_quota`, default 3) is how many quality applications
the owner wants to send a day. The morning brief carries a shortlist of the best
open matches not yet applied to, so there is never a need to hunt. A day counts
toward the streak when at least `quota` listings reached 'applied' that day
(local time); today counts once it is met, and an unfinished today doesn't break it.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .core.db import Database
from .core.settings import get_settings
from .core.util import now
from .owner_actions import remember_listing


def quota() -> int:
    from .sources.registry import load_profile
    try:
        return max(1, int(load_profile().get("daily_apply_quota") or 3))
    except (TypeError, ValueError):
        return 3


def todays_shortlist(db: Database, n: int | None = None) -> list[dict]:
    """Best open matches you haven't acted on: verified or confirmed first, then score, ghost risk last."""
    n = n or quota()
    rows = db.all(
        "SELECT * FROM opportunities WHERE stage IN ('found','shortlisted','preparing') AND verification_status!='failed' "
        "AND (deadline IS NULL OR deadline>?) AND COALESCE(relevance_score,0)>=0.5 AND type IN ('internship','job','unknown') "
        "ORDER BY CASE WHEN stage='preparing' THEN 0 WHEN stage='shortlisted' THEN 1 ELSE 2 END, "
        "CASE WHEN verification_status='verified' THEN 0 ELSE 1 END, "
        "COALESCE(relevance_score,0) * (1 - 0.5 * COALESCE(ghost_risk,0)) DESC, deadline LIMIT ?", (now(), n))
    remember_listing(db, "listing:opportunities", [r["id"] for r in rows])
    return rows


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, ZoneInfo(get_settings().timezone)).strftime("%Y-%m-%d")


def applied_per_day(db: Database, days: int = 60) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in db.all("SELECT DISTINCT opportunity_id, at FROM application_stages WHERE stage='applied' AND at>=?",
                    (now() - days * 86400,)):
        d = _day(r["at"])
        out[d] = out.get(d, 0) + 1
    return out


def streak(db: Database, q: int | None = None) -> dict:
    q = q or quota()
    per = applied_per_day(db)
    today = datetime.now(ZoneInfo(get_settings().timezone)).date()
    n, d = 0, today
    if per.get(d.isoformat(), 0) < q:   # today isn't over yet: start counting from yesterday
        d -= timedelta(days=1)
    while per.get(d.isoformat(), 0) >= q:
        n += 1
        d -= timedelta(days=1)
    best, run = 0, 0
    for i in range(60, -1, -1):
        if per.get((today - timedelta(days=i)).isoformat(), 0) >= q:
            run += 1
            best = max(best, run)
        else:
            run = 0
    return {"quota": q, "today": per.get(today.isoformat(), 0), "streak": n, "best": best}
