"""Referral finder: who to ask, and a note you send yourself.

NOESIS never opens, searches or scrapes LinkedIn (CLAUDE.md) and never messages anyone.
It builds search links that you open in your own browser (alumni from your college at
the company, engineers in Bengaluru, campus recruiters) and drafts a short connection
note from your verified facts. You pick the people, send the note, and log them here
so the follow-up stays on your list.
"""
from __future__ import annotations

from urllib.parse import quote_plus

from .core.db import Database
from .core.state import audit
from .core.util import new_id, now
from .owner_actions import OwnerActionError, clip, find_opportunity

STATUSES = ("to_contact", "messaged", "replied", "referred", "declined")
NOTE_LIMIT = 300   # LinkedIn's connection-note limit


def _fact(profile: dict, key: str) -> str:
    f = (profile.get("facts") or {}).get(key) or {}
    return str(f.get("value") or "").strip() if isinstance(f, dict) and f.get("verified") else ""


def search_links(company: str, college: str = "", city: str = "Bengaluru") -> list[dict]:
    li = lambda q: "https://www.linkedin.com/search/results/people/?keywords=" + quote_plus(q)  # noqa: E731
    g = lambda q: "https://www.google.com/search?q=" + quote_plus(q)  # noqa: E731
    out = []
    if college:
        out.append({"label": f"Alumni of {clip(college, 40)} at {company}", "url": li(f"{company} {college}")})
        out.append({"label": "Same, via Google", "url": g(f'site:linkedin.com/in "{company}" "{college}"')})
    out.append({"label": f"Engineers at {company} in {city}", "url": li(f"{company} software engineer {city}")})
    out.append({"label": f"Campus / university recruiters at {company}", "url": li(f"{company} university recruiter India")})
    return out


def draft_note(profile: dict, company: str, role: str | None = None) -> str:
    """A short, honest note from verified facts only. You edit and send it yourself."""
    college = _fact(profile, "college") or _fact(profile, "institution")
    year = _fact(profile, "graduation_year")
    skills = ", ".join(s.strip() for s in _fact(profile, "skills").split(",")[:3] if s.strip())
    who = "I'm a student" + (f" at {college}" if college else "") + (f" (graduating {year})" if year else "")
    role_txt = f"the {role} role" if role else f"internships at {company}"
    parts = [f"Hi! {who}.", f"I'm applying for {role_txt}" + (f" and work with {skills}" if skills else "") + ".",
             "Would you be open to a quick chat or a referral if it looks like a fit? Thank you!"]
    note = " ".join(parts)
    if len(note) > NOTE_LIMIT:
        note = " ".join([f"Hi! {who}.", f"Applying for {role_txt}.", "Open to a quick chat or a referral? Thanks!"])
    return note[:NOTE_LIMIT]


def plan(db: Database, token: str) -> dict:
    """token: an opportunity ref, or a company name."""
    from .sources.registry import load_profile
    token = (token or "").strip()
    if not token:
        raise OwnerActionError("Use /referrals <ref> or /referrals <company>, e.g. /referrals Razorpay")
    opp = None
    try:
        opp = find_opportunity(db, token)
    except OwnerActionError:
        pass
    company = (opp or {}).get("organizer") or token
    profile = load_profile()
    college = _fact(profile, "college") or _fact(profile, "institution")
    existing = db.all("SELECT * FROM referrals WHERE lower(company)=lower(?) ORDER BY created_at", (company,))
    return {"company": company, "opportunity": opp, "links": search_links(company, college),
            "note": draft_note(profile, company, (opp or {}).get("title")), "existing": existing,
            "college_known": bool(college)}


def add_referral(db: Database, company: str, name: str, profile_url: str | None = None, relation: str = "employee",
                 opportunity_id: str | None = None, note: str | None = None, by: str = "owner") -> dict:
    if not (company or "").strip() or not (name or "").strip():
        raise OwnerActionError("A referral needs the company and the person's name.")
    rid = new_id("ref")
    db.execute("INSERT INTO referrals(id, opportunity_id, company, name, profile_url, relation, status, note_draft, created_at, "
               "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (rid, opportunity_id, company.strip()[:80], name.strip()[:80],
                                                             (profile_url or "")[:300] or None, relation[:20], "to_contact",
                                                             note, now(), now()))
    audit(db, by, "referral.added", None, {"referral_id": rid, "company": company})
    return db.one("SELECT * FROM referrals WHERE id=?", (rid,))


def set_status(db: Database, referral_id: str, status: str, by: str = "owner") -> dict:
    if status not in STATUSES:
        raise OwnerActionError(f"Status must be one of: {', '.join(STATUSES)}")
    n = db.execute("UPDATE referrals SET status=?, updated_at=? WHERE id=? OR id LIKE ?",
                   (status, now(), referral_id, f"%{referral_id}")).rowcount
    if not n:
        raise OwnerActionError("No referral with that id.")
    audit(db, by, "referral.status", None, {"referral_id": referral_id, "status": status})
    return db.one("SELECT * FROM referrals WHERE id=? OR id LIKE ?", (referral_id, f"%{referral_id}"))


def render(p: dict) -> str:
    lines = [f"🤝 Referrals at {p['company']}", "", "Open these yourself (I never open LinkedIn):"]
    lines += [f"• {link['label']}: {link['url']}" for link in p["links"]]
    if not p["college_known"]:
        lines.append("Tip: /profile set college <your college> adds an alumni search.")
    lines += ["", "A note you can send (edit it first):", p["note"]]
    if p["existing"]:
        lines += ["", "Already on your list:"] + [f"• {r['name']} ({r['status'].replace('_', ' ')})" for r in p["existing"]]
    lines += ["", "Log someone with /referral add <company> | <name> | <profile link>"]
    return "\n".join(lines)
