"""Offer comparison: stipend, duration, location and mode, PPO chance and your own
learning rating, side by side with a transparent score (weights below; change them
in your profile under offer_weights)."""
from __future__ import annotations

import re

from .core.db import Database
from .core.state import audit
from .core.util import new_id, now
from .owner_actions import OwnerActionError, clip

DEFAULT_WEIGHTS = {"stipend": 0.35, "ppo": 0.25, "learning": 0.25, "mode": 0.1, "location": 0.05}
PPO = {"yes": 1.0, "likely": 0.7, "unknown": 0.3, "no": 0.0}
MODE = {"onsite": 0.8, "hybrid": 1.0, "remote": 0.7}


def _int(s: str) -> int | None:
    s = (s or "").lower().replace(",", "").replace("₹", "").replace("rs.", "").replace("inr", "").strip()
    m = re.match(r"^(\d+(?:\.\d+)?)\s*(k|l|lakh|lpa)?", s)
    if not m:
        return None
    v = float(m.group(1)) * {"k": 1000, "l": 100000, "lakh": 100000, "lpa": 100000 / 12}.get(m.group(2) or "", 1)
    return int(round(v))


def parse_offer(text: str) -> dict:
    """'Company | stipend/month | months | location | mode | ppo | learning 1-5 [| notes]'"""
    parts = [p.strip() for p in (text or "").split("|")]
    if len(parts) < 2 or not parts[0]:
        raise OwnerActionError("Use /offer <company> | <stipend per month> | <months> | <location> | <onsite/hybrid/remote> | "
                               "<ppo yes/likely/unknown/no> | <learning 1-5>, e.g. /offer Razorpay | 50000 | 6 | Bengaluru | "
                               "hybrid | likely | 4")
    get = lambda i: parts[i] if len(parts) > i else ""  # noqa: E731
    mode = get(4).lower() or None
    if mode and mode not in MODE:
        raise OwnerActionError("Mode must be onsite, hybrid or remote.")
    ppo = (get(5).lower().replace("ppo", "").strip() or "unknown")
    if ppo not in PPO:
        raise OwnerActionError("PPO must be yes, likely, unknown or no.")
    learning = _int(get(6))
    if learning is not None and not 1 <= learning <= 5:
        raise OwnerActionError("Rate learning from 1 to 5.")
    months = get(2)
    return {"company": parts[0][:80], "stipend_month_inr": _int(get(1)),
            "duration_months": float(months) if re.match(r"^\d+(\.\d+)?$", months) else None,
            "location": get(3)[:60] or None, "mode": mode, "ppo": ppo, "learning": learning, "notes": get(7)[:300] or None}


def add_offer(db: Database, fields: dict, opportunity_id: str | None = None, by: str = "owner") -> dict:
    oid = new_id("off")
    db.execute("INSERT INTO offers(id, opportunity_id, company, role, stipend_month_inr, duration_months, location, mode, ppo, "
               "learning, deadline_to_accept, notes, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (oid, opportunity_id, fields["company"], fields.get("role"), fields.get("stipend_month_inr"),
                fields.get("duration_months"), fields.get("location"), fields.get("mode"), fields.get("ppo") or "unknown",
                fields.get("learning"), fields.get("deadline_to_accept"), fields.get("notes"), now()))
    audit(db, by, "offer.added", None, {"offer_id": oid, "company": fields["company"]})
    return db.one("SELECT * FROM offers WHERE id=?", (oid,))


def delete_offer(db: Database, offer_id: str) -> int:
    return db.execute("DELETE FROM offers WHERE id=? OR id LIKE ?", (offer_id, f"%{offer_id}")).rowcount


def weights() -> dict:
    from .sources.registry import load_profile
    w = {**DEFAULT_WEIGHTS, **{k: float(v) for k, v in (load_profile().get("offer_weights") or {}).items() if k in DEFAULT_WEIGHTS}}
    total = sum(w.values()) or 1
    return {k: v / total for k, v in w.items()}


def compare(db: Database) -> list[dict]:
    from .sources.registry import load_profile
    rows = db.all("SELECT * FROM offers ORDER BY created_at")
    if not rows:
        return []
    w = weights()
    top = max((r["stipend_month_inr"] or 0) for r in rows) or 1
    prefs = [p.lower() for p in load_profile().get("preferred_locations", [])]
    for r in rows:
        parts = {
            "stipend": (r["stipend_month_inr"] or 0) / top,
            "ppo": PPO.get(r["ppo"] or "unknown", 0.3),
            "learning": ((r["learning"] or 3) - 1) / 4,
            "mode": MODE.get(r["mode"] or "", 0.7),
            "location": 1.0 if any(p and p in (r["location"] or "").lower() for p in prefs) or r["mode"] == "remote" else 0.5,
        }
        r["parts"] = {k: round(v, 2) for k, v in parts.items()}
        r["score"] = round(100 * sum(w[k] * parts[k] for k in w))
    return sorted(rows, key=lambda r: -r["score"])


def render(rows: list[dict]) -> str:
    if not rows:
        return "No offers logged yet. Add one with /offer <company> | <stipend> | <months> | <location> | <mode> | <ppo> | <learning 1-5>"
    lines = ["🏁 Your offers, best first", ""]
    for i, r in enumerate(rows, 1):
        stipend = f"₹{r['stipend_month_inr']:,}/mo" if r["stipend_month_inr"] else "stipend ?"
        bits = [stipend, f"{r['duration_months']:g} mo" if r["duration_months"] else None, r["location"], r["mode"],
                f"PPO {r['ppo']}", f"learning {r['learning']}/5" if r["learning"] else None]
        lines.append(f"{i}. {clip(r['company'], 40)}: {r['score']}/100")
        lines.append("   " + " · ".join(b for b in bits if b))
    w = weights()
    lines += ["", "Weights: " + ", ".join(f"{k} {int(v * 100)}%" for k, v in w.items()) + " (profile offer_weights to change)."]
    return "\n".join(lines)
