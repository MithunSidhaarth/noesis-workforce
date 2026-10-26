"""Weekly "what's working": which sources, kinds of role, companies and resume
versions get replies, and what to change. Plain sentences from the stage history;
nothing is shown until there are enough applications to say something (3+)."""
from __future__ import annotations

import re

from .core.db import Database
from .pipeline import response_rates

REPLIED = "('online_assessment','interview_1','interview_2','hr','offer','rejected')"
ROLE_KINDS = [("ML / AI", r"\b(ml|machine learning|ai|data scien|nlp|llm|research)\b"),
              ("backend / SDE", r"\b(backend|back-end|sde|software|platform|developer)\b"),
              ("frontend", r"\b(frontend|front-end|react|web|ui)\b"),
              ("data / analytics", r"\b(data|analyst|analytics)\b"),
              ("product", r"\b(product|pm)\b")]
MIN = 3


def _applied_rows(db: Database) -> list[dict]:
    return db.all(
        "SELECT o.id, o.title, o.organizer, o.discovery_source, a.resume_version, MIN(s.at) applied_at, "
        f"MAX(CASE WHEN s2.stage IN {REPLIED} THEN 1 ELSE 0 END) replied, "
        f"MIN(CASE WHEN s2.stage IN {REPLIED} THEN s2.at END) replied_at "
        "FROM opportunities o JOIN application_stages s ON s.opportunity_id=o.id AND s.stage='applied' "
        "LEFT JOIN application_stages s2 ON s2.opportunity_id=o.id LEFT JOIN applications a ON a.opportunity_id=o.id "
        "GROUP BY o.id")


def _rate(rows: list[dict]) -> float:
    return sum(r["replied"] for r in rows) / len(rows) if rows else 0.0


def weekly_insights(db: Database) -> list[str]:
    rows = _applied_rows(db)
    if len(rows) < MIN:
        return [f"Apply to {MIN - len(rows)} more to start seeing what works."] if rows else []
    out = []
    overall = _rate(rows)
    out.append(f"{int(overall * 100)}% of your {len(rows)} applications got a reply.")
    srcs = [r for r in response_rates(db, "source") if r["applied"] >= MIN]
    if len(srcs) >= 2:
        best, worst = max(srcs, key=lambda r: r["rate"]), min(srcs, key=lambda r: r["rate"])
        if best["rate"] > worst["rate"]:
            out.append(f"{best['name']} works best ({int(best['rate'] * 100)}% replied) and {worst['name']} worst "
                       f"({int(worst['rate'] * 100)}%): spend more time on the first.")
    kinds = []
    for label, rx in ROLE_KINDS:
        sub = [r for r in rows if re.search(rx, r["title"] or "", re.I)]
        if len(sub) >= MIN:
            kinds.append((label, _rate(sub), len(sub)))
    if len(kinds) >= 2:
        kinds.sort(key=lambda k: -k[1])
        out.append(f"{kinds[0][0]} roles reply most ({int(kinds[0][1] * 100)}% of {kinds[0][2]}); "
                   f"{kinds[-1][0]} least ({int(kinds[-1][1] * 100)}% of {kinds[-1][2]}).")
    versions = {}
    for r in rows:
        if r["resume_version"]:
            versions.setdefault(r["resume_version"], []).append(r)
    vs = [(v, _rate(rs), len(rs)) for v, rs in versions.items() if len(rs) >= MIN]
    if len(vs) >= 2:
        vs.sort(key=lambda x: -x[1])
        out.append(f"Resume {vs[0][0]} gets more replies ({int(vs[0][1] * 100)}%) than {vs[-1][0]} ({int(vs[-1][1] * 100)}%).")
    waits = [(r["replied_at"] - r["applied_at"]) / 86400 for r in rows if r["replied_at"] and r["applied_at"]]
    if waits:
        out.append(f"Replies come after {sum(waits) / len(waits):.0f} days on average; follow up after two weeks.")
    silent = [r for r in response_rates(db, "company") if r["applied"] >= MIN and not r["replied"]]
    if silent:
        out.append("No reply yet from " + ", ".join(s["name"] for s in silent[:3]) + ": try a referral there instead (/referrals).")
    if overall < 0.1 and len(rows) >= 10:
        out.append("Under 10% replies: tailor the resume per listing (/tailor) and ask for referrals on your top picks.")
    return out
