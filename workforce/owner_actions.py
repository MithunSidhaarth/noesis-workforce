"""Things only the owner can do, shared by Telegram, the dashboard API and noesisctl.

Each function records who did it in the audit log. Nothing here is reachable by
a bot: bots only ever see the results (an opportunity marked verified by the
owner, a fact the owner typed, a decided approval).
"""
from __future__ import annotations

import re
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from .contracts import TaskSpec
from .core.db import Database, dumps, loads
from .core.queue import BotDisabled, TaskQueue
from .core.settings import get_settings
from .core.state import audit
from .core.util import new_id, now, sha

OPEN_APP_STATES = ("PREPARATION", "REVIEW", "READY_TO_SUBMIT")
OUTCOMES = ("accepted", "rejected", "no_response", "withdrawn")
SENSITIVE_KEYS = {"date_of_birth", "id_number", "aadhaar", "passport_number", "address", "phone", "income", "caste", "religion"}
ANSWER_KEYS_PREFIX = ("short_bio", "why_", "about_", "motivation", "achievements", "projects")


class OwnerActionError(ValueError):
    """Bad input from the owner; the message is safe to show them."""


def tz() -> ZoneInfo:
    return ZoneInfo(get_settings().timezone)


def ref(id_: str) -> str:
    """Short, typeable handle for an id (last 6 chars)."""
    return id_[-6:]


def local(ts: float | None, fmt: str = "%a %d %b, %H:%M") -> str:
    return datetime.fromtimestamp(ts, tz()).strftime(fmt) if ts else "unknown"


def closes_in(deadline: float | None) -> str:
    if not deadline:
        return "Deadline not stated"
    secs = deadline - now()
    if secs < 0:
        return f"Closed {local(deadline, '%d %b')}"
    days = int(secs // 86400)
    when = local(deadline, "%d %b %H:%M")
    zone = datetime.now(tz()).tzname()
    if days == 0:
        return f"Closes today ({when} {zone}, {int(secs // 3600)}h left)"
    return f"Closes in {days} day{'s' if days != 1 else ''} ({when} {zone})"


# ---- lookups -------------------------------------------------------------------
def _resolve(db: Database, table: str, token: str, listing_key: str | None = None) -> dict:
    token = (token or "").strip()
    if not token:
        raise OwnerActionError("Which one? Give the number or the 6-character ref.")
    if listing_key and token.isdigit():
        ids = loads(db.scalar("SELECT value FROM system_state WHERE key=?", (listing_key,)), [])
        n = int(token)
        if 1 <= n <= len(ids):
            r = db.one(f"SELECT * FROM {table} WHERE id=?", (ids[n - 1],))
            if r:
                return r
        raise OwnerActionError(f"No item #{n} in the last list. Send the list command again.")
    rows = db.all(f"SELECT * FROM {table} WHERE id=? OR id LIKE ? LIMIT 2", (token, f"%{token}"))
    if len(rows) == 1:
        return rows[0]
    if not rows:
        raise OwnerActionError(f"Nothing found for '{token}'.")
    raise OwnerActionError(f"'{token}' matches more than one item; use more characters.")


def remember_listing(db: Database, key: str, ids: list[str]) -> None:
    db.execute("INSERT INTO system_state(key, value, updated_at, updated_by) VALUES (?,?,?,'owner_actions') "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at", (key, dumps(ids), now()))


def find_opportunity(db: Database, token: str) -> dict:
    return _resolve(db, "opportunities", token, "listing:opportunities")


def find_approval(db: Database, token: str) -> dict:
    return _resolve(db, "approvals", token, "listing:approvals")


def find_application(db: Database, token: str) -> dict:
    """Accepts an application ref or the ref of its opportunity."""
    try:
        return _resolve(db, "applications", token)
    except OwnerActionError:
        opp = find_opportunity(db, token)
        app = db.one("SELECT * FROM applications WHERE opportunity_id=?", (opp["id"],))
        if not app:
            raise OwnerActionError(f"'{opp['title'][:60]}' has no application yet. Use /confirm {ref(opp['id'])} first.")
        return app


# ---- opportunities -------------------------------------------------------------
def plain_status(opp: dict) -> str:
    notes = loads(opp.get("verification_notes_json"), [])
    if opp.get("confirmed_by"):
        return "Confirmed by you"
    s = opp.get("verification_status")
    if s == "verified":
        return "Looks official"
    if s == "failed":
        return "Problem: " + (next((n for n in notes if "passed" in n or "reachab" in n), None) or "failed checks")
    if s == "needs_review":
        why = []
        for n in notes:
            if "aggregator" in n:
                why.append("link is from a listing site, not the organiser")
            elif "deadline not stated" in n:
                why.append("deadline not stated")
            elif "no application" in n:
                why.append("no link to apply")
            elif "duplicate" in n:
                why.append("may be a duplicate")
        return "Needs a quick check: " + ("; ".join(dict.fromkeys(why)) or "see details")
    return "Not checked yet"


def candidate_links(opp: dict) -> list[str]:
    return [u for u in loads(opp.get("raw_json"), {}).get("candidate_links", []) if isinstance(u, str)]


def opportunity_card(opp: dict, short: bool = False) -> tuple[str, list]:
    elig = loads(opp.get("eligibility_json"), [])
    who = "; ".join(e[:90] for e in elig[:2]) if elig else "not stated"
    lines = [f"🎯 {opp['title'][:120]}",
             f"{opp['type'].capitalize()} · {closes_in(opp.get('deadline'))}",
             f"For: {who}",
             f"Effort: {opp.get('estimated_effort') or 'unknown'} · Match: {int((opp.get('relevance_score') or 0) * 100)}%",
             f"Status: {plain_status(opp)}",
             opp.get("official_url") or "No link in the listing"]
    if not short:
        if opp.get("relevance_rationale"):
            lines.append(f"Why: {opp['relevance_rationale']}")
        cands = candidate_links(opp)
        if cands and opp.get("verification_status") != "verified":
            lines.append(f"Possible official page (unconfirmed): {cands[0]}")
        if opp.get("skip_reason"):
            lines.append(f"Skipped: {opp['skip_reason']}")
    lines.append(f"Ref {ref(opp['id'])}")
    buttons = []
    if opp.get("application_status") not in ("SUBMITTED", "CLOSED") and opp.get("verification_status") != "failed":
        buttons = [[{"text": "✅ Prepare", "data": f"o:{opp['id']}:p"},
                    {"text": "⏰ Remind me", "data": f"o:{opp['id']}:r"},
                    {"text": "✖ Not for me", "data": f"o:{opp['id']}:s"}]]
    return "\n".join(lines), buttons


def shortlist(db: Database, include_all: bool = False, limit: int = 10) -> list[dict]:
    min_score = 0.0 if include_all else 0.5
    rows = db.all(
        "SELECT * FROM opportunities WHERE verification_status!='failed' AND application_status NOT IN ('SUBMITTED','CLOSED') "
        "AND (deadline IS NULL OR deadline>?) AND COALESCE(relevance_score,0)>=? "
        "ORDER BY CASE WHEN deadline IS NULL THEN 1 ELSE 0 END, deadline, relevance_score DESC LIMIT ?",
        (now(), min_score, limit))
    remember_listing(db, "listing:opportunities", [r["id"] for r in rows])
    return rows


def schedule_deadline_reminders(db: Database, opp: dict, by: str, days_before=(7, 2, 1)) -> int:
    """Reminders at T-7, T-2 and T-1 days (only those still in the future). Idempotent."""
    if not opp.get("deadline"):
        return 0
    n = 0
    for d in days_before:
        due = opp["deadline"] - d * 86400
        if due <= now():
            continue
        title = (f"“{opp['title'][:80]}” closes in {d} day{'s' if d != 1 else ''} "
                 f"({local(opp['deadline'], '%d %b %H:%M')}). {opp.get('official_url') or ''}".strip())
        cur = db.execute("INSERT OR IGNORE INTO reminders(id, title, due_at, source, ref_id, dedupe_key, created_at) "
                         "VALUES (?,?,?,?,?,?,?)", (new_id("rem"), title, due, by, opp["id"], f"oppdl:{opp['id']}:{d}", now()))
        n += cur.rowcount
    return n


def cancel_opportunity_reminders(db: Database, opp_id: str) -> int:
    return db.execute("UPDATE reminders SET status='cancelled' WHERE ref_id=? AND status='pending'", (opp_id,)).rowcount


def _safe_url(url: str | None) -> str | None:
    if not url:
        return None
    url = url.strip()
    if not re.match(r"^https://[^\s/$.?#][^\s]*$", url) or len(url) > 1000:
        raise OwnerActionError("The link must be a full https:// address.")
    return url


def confirm_opportunity(db: Database, token: str, by: str, url: str | None = None) -> dict:
    """The owner vouches for this opportunity (optionally giving the official link).
    It becomes verified and goes straight to Applications for preparation."""
    opp = find_opportunity(db, token)
    if opp["application_status"] in ("SUBMITTED", "CLOSED"):
        raise OwnerActionError(f"That one is already {opp['application_status'].lower()}.")
    url = _safe_url(url)
    notes = loads(opp["verification_notes_json"], []) + [f"confirmed by owner ({by})" + (f"; official link {url}" if url else "")]
    db.execute("UPDATE opportunities SET verification_status='verified', confirmed_by=?, official_url=COALESCE(?, official_url), "
               "verification_notes_json=?, last_verified=?, application_status=CASE WHEN application_status='DISCOVERED' "
               "THEN 'VERIFIED' ELSE application_status END WHERE id=?", (by, url, dumps(notes), now(), opp["id"]))
    audit(db, by, "opportunity.confirmed", None, {"opportunity_id": opp["id"], "url": url})
    opp = db.one("SELECT * FROM opportunities WHERE id=?", (opp["id"],))
    schedule_deadline_reminders(db, opp, by)
    try:
        TaskQueue(db).enqueue(TaskSpec(bot_id="applications", kind="evaluate_opportunity",
                                       payload={"opportunity_id": opp["id"], "refresh": True}, requested_by=by,
                                       idempotency_key=f"confirm:{opp['id']}:{int(now())}"))
        queued = True
    except BotDisabled:
        queued = False
    return {"opportunity": opp, "queued": queued}


def skip_opportunity(db: Database, token: str, by: str, reason: str = "") -> dict:
    opp = find_opportunity(db, token)
    reason = (reason or "not for me").strip()[:200]
    db.execute("UPDATE opportunities SET application_status='CLOSED', skip_reason=? WHERE id=?", (reason, opp["id"]))
    db.execute("UPDATE applications SET state='CLOSED', outcome='skipped', updated_at=? WHERE opportunity_id=? AND state!='CLOSED'",
               (now(), opp["id"]))
    cancel_opportunity_reminders(db, opp["id"])
    audit(db, by, "opportunity.skipped", None, {"opportunity_id": opp["id"], "reason": reason})
    return db.one("SELECT * FROM opportunities WHERE id=?", (opp["id"],))


def remind_opportunity(db: Database, token: str, by: str) -> tuple[dict, int]:
    opp = find_opportunity(db, token)
    if not opp["deadline"]:
        raise OwnerActionError("No deadline is stated for that one, so there is nothing to time a reminder to. "
                               "Use /remind <when> <what> instead.")
    return opp, schedule_deadline_reminders(db, opp, by)


def record_outcome(db: Database, token: str, outcome: str, by: str) -> dict:
    outcome = outcome.strip().lower().replace(" ", "_")
    if outcome not in OUTCOMES:
        raise OwnerActionError(f"Outcome must be one of: {', '.join(OUTCOMES)}.")
    app = find_application(db, token)
    hist = loads(app["history_json"], []) + [{"ts": now(), "from": app["state"], "to": "CLOSED", "note": f"outcome {outcome} ({by})"}]
    db.execute("UPDATE applications SET state='CLOSED', outcome=?, history_json=?, updated_at=? WHERE id=?",
               (outcome, dumps(hist), now(), app["id"]))
    db.execute("UPDATE opportunities SET application_status='CLOSED' WHERE id=?", (app["opportunity_id"],))
    cancel_opportunity_reminders(db, app["opportunity_id"])
    audit(db, by, "application.outcome", None, {"application_id": app["id"], "outcome": outcome})
    return db.one("SELECT * FROM applications WHERE id=?", (app["id"],))


def record_submission(db: Database, token: str, confirmation: str, by: str) -> dict:
    from .agents.applications import record_manual_submission
    app = find_application(db, token)
    if not confirmation.strip():
        raise OwnerActionError("Add the confirmation number or a short note, e.g. /submitted abc123 email received 14 Oct.")
    try:
        out = record_manual_submission(db, app["id"], confirmation.strip()[:300], by)
    except ValueError as e:
        raise OwnerActionError(str(e)) from e
    cancel_opportunity_reminders(db, app["opportunity_id"])
    return out


# ---- approvals -----------------------------------------------------------------
def approval_message(db: Database, approval_id: str, bot_name: str) -> tuple[str, list]:
    a = db.one("SELECT * FROM approvals WHERE id=?", (approval_id,))
    if not a:
        return f"Approval needed ({bot_name}).", []
    reasons = loads(a["reasons_json"], [])
    text = "\n".join([f"🙋 {bot_name} needs your OK", a["summary"][:300], "", "Why it's asking:",
                      *[f"• {r}" for r in reasons[:6]], "", f"Ref {ref(a['id'])}"])
    return text, [[{"text": "✅ Approve", "data": f"a:{a['id']}:y"}, {"text": "✖ Deny", "data": f"a:{a['id']}:n"}]]


def pending_approvals(db: Database, limit: int = 10) -> list[dict]:
    rows = db.all("SELECT * FROM approvals WHERE status='pending' ORDER BY created_at LIMIT ?", (limit,))
    remember_listing(db, "listing:approvals", [r["id"] for r in rows])
    return rows


def decide(db: Database, token: str, grant: bool, by: str) -> dict:
    from .policies.engine import decide_approval
    a = find_approval(db, token)
    if a["status"] != "pending":
        raise OwnerActionError(f"That request was already {a['status']}.")
    out = decide_approval(db, a["id"], grant, by=by)
    if not out:
        raise OwnerActionError("That request is no longer pending.")
    return out


# ---- reminders -----------------------------------------------------------------
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def parse_when(text: str, ref_ts: float | None = None) -> tuple[float, str]:
    """Strict parser for '/remind <when> <what>'. Returns (due_ts, what).
    Accepted: 'YYYY-MM-DD HH:MM', 'today HH:MM', 'tomorrow HH:MM', 'in 30m|2h|3d',
    '<weekday> HH:MM'. Anything else raises OwnerActionError (we ask, never guess)."""
    z = tz()
    base = datetime.fromtimestamp(ref_ts or now(), z)
    s = text.strip()
    m = re.match(r"^(\d{4}-\d{2}-\d{2})\s+(\d{1,2}):(\d{2})\s+(.+)$", s)
    if m:
        try:
            d = datetime.strptime(m.group(1), "%Y-%m-%d").date()
            due = datetime.combine(d, dtime(int(m.group(2)), int(m.group(3))), z)
        except ValueError as e:
            raise OwnerActionError(f"That date/time isn't valid: {e}") from e
        return _future(due, base), m.group(4)
    m = re.match(r"^in\s+(\d{1,4})\s*(m|min|mins|minutes?|h|hrs?|hours?|d|days?)\s+(.+)$", s, re.I)
    if m:
        n, unit = int(m.group(1)), m.group(2)[0].lower()
        delta = {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n)}[unit]
        return (base + delta).timestamp(), m.group(3)
    m = re.match(r"^(today|tomorrow|" + "|".join(WEEKDAYS) + r"|mon|tue|wed|thu|fri|sat|sun)\s+(\d{1,2}):(\d{2})\s+(.+)$", s, re.I)
    if m:
        word, hh, mm = m.group(1).lower(), int(m.group(2)), int(m.group(3))
        if hh > 23 or mm > 59:
            raise OwnerActionError("Use a 24-hour time like 18:30.")
        if word == "today":
            day = base.date()
        elif word == "tomorrow":
            day = base.date() + timedelta(days=1)
        else:
            idx = next(i for i, w in enumerate(WEEKDAYS) if w.startswith(word[:3]))
            ahead = (idx - base.weekday()) % 7
            day = base.date() + timedelta(days=ahead)
            if ahead == 0 and datetime.combine(day, dtime(hh, mm), z) <= base:
                day += timedelta(days=7)
        return _future(datetime.combine(day, dtime(hh, mm), z), base), m.group(4)
    raise OwnerActionError("I couldn't read the time. Try one of:\n/remind tomorrow 09:00 call the bank\n"
                           "/remind in 2h submit the form\n/remind fri 18:00 review essay\n/remind 2026-11-03 10:00 renew passport")


def _future(due: datetime, base: datetime) -> float:
    if due <= base:
        raise OwnerActionError(f"{due:%d %b %H:%M} is already in the past.")
    return due.timestamp()


def create_reminder(db: Database, text: str, by: str) -> dict:
    due, what = parse_when(text)
    rid = new_id("rem")
    db.execute("INSERT INTO reminders(id, title, due_at, source, dedupe_key, created_at) VALUES (?,?,?,?,?,?)",
               (rid, what.strip()[:200], due, "owner", f"owner:{rid}", now()))
    audit(db, by, "reminder.created", None, {"reminder_id": rid})
    return db.one("SELECT * FROM reminders WHERE id=?", (rid,))


# ---- profile -------------------------------------------------------------------
def profile_section(key: str) -> str:
    return "answers" if key.startswith(ANSWER_KEYS_PREFIX) else "facts"


def set_profile_fact(db: Database, key: str, value: str, by: str) -> dict:
    key = key.strip().lower()
    if not re.match(r"^[a-z][a-z0-9_]{1,40}$", key):
        raise OwnerActionError("Keys are lowercase words joined by _, e.g. graduation_year.")
    value = value.strip()
    if not value or len(value) > 2000:
        raise OwnerActionError("Give a value (up to 2000 characters).")
    sec = profile_section(key)
    db.execute("INSERT INTO profile_facts(key, value, section, sensitive, updated_at, updated_by) VALUES (?,?,?,?,?,?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value, section=excluded.section, sensitive=excluded.sensitive, "
               "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
               (key, value, sec, int(key in SENSITIVE_KEYS), now(), by))
    audit(db, by, "profile.fact_set", None, {"key": key, "section": sec})   # value deliberately not logged
    refreshed = refresh_open_applications(db, by)
    return {"key": key, "section": sec, "sensitive": key in SENSITIVE_KEYS, "refreshed": refreshed}


def refresh_open_applications(db: Database, by: str) -> int:
    """Re-prepare applications that were waiting on information."""
    n = 0
    for a in db.all("SELECT id, opportunity_id FROM applications WHERE state IN ('PREPARATION','REVIEW') AND missing_json!='[]'"):
        try:
            _, created = TaskQueue(db).enqueue(TaskSpec(
                bot_id="applications", kind="evaluate_opportunity", payload={"opportunity_id": a["opportunity_id"], "refresh": True},
                requested_by=by, idempotency_key=f"refresh:{a['id']}:{int(now() // 60)}"))
            n += int(created)
        except BotDisabled:
            break
    return n


MISSING_TO_KEY = {"age": "age", "citizenship": "citizenship", "education level": "degree", "gender": "gender",
                  "short_bio (verified)": "short_bio"}


def missing_summary(db: Database) -> list[tuple[str, int, str]]:
    """(missing item, number of open applications waiting on it, how to supply it)."""
    counts: dict[str, int] = {}
    for a in db.all(f"SELECT missing_json FROM applications WHERE state IN ({','.join('?' * len(OPEN_APP_STATES))})",
                    OPEN_APP_STATES):
        for m in set(loads(a["missing_json"], [])):
            counts[m] = counts.get(m, 0) + 1
    out = []
    for item, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        key = MISSING_TO_KEY.get(item)
        if key:
            how = f"/profile set {key} <value>"
        elif item.startswith("unrecognized criterion"):
            how = "read the listing and decide; then /confirm or /skip"
        elif item.startswith("verified profile facts"):
            how = "/profile set <key> <value> for degree, citizenship, graduation_year…"
        else:
            how = "add the document under documents: in config/profile.yaml"
        out.append((item, n, how))
    return out


def log_work(db: Database, text: str, by: str) -> dict:
    """'/work [brand] <what you did>' -> posts for each brand and platform (Content Strategist)."""
    from .brands import load_brands
    text = (text or "").strip()
    first, _, rest = text.partition(" ")
    ids = {b["id"].lower(): b["id"] for b in load_brands()} | {b["name"].lower(): b["id"] for b in load_brands()}
    brand = ids.get(first.lower().lstrip("@"))
    if brand:
        text = rest.strip()
    if len(text) < 10:
        raise OwnerActionError("Tell me what you worked on, e.g. /work shipped the voice agent demo: Whisper + Llama, 300ms latency")
    TaskQueue(db).enqueue(TaskSpec(bot_id="content_strategist", kind="log_work", payload={"text": text[:2000], "brand": brand},
                                   requested_by=by, idempotency_key=f"work:{sha(text)[:16]}:{brand}"))
    return {"brand": brand}


def request_ideas(db: Database, by: str) -> dict:
    _, created = TaskQueue(db).enqueue(TaskSpec(bot_id="knowledge", kind="idea_scout", payload={}, requested_by=by,
                                                idempotency_key=f"ideas:{int(now() // 3600)}"))
    return {"queued": created}
