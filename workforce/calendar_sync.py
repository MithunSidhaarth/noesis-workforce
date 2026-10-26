"""Google Calendar sync: your own deadlines, OAs and interviews on your primary calendar.

One-way (NOESIS -> calendar), owner only: events carry no guests, so nobody else is
invited or emailed. Each event is created once (calendar_sync.key); a stage logged
with a new time gets a new event. Off until policies/calendar.yaml sync_enabled: true.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .core.settings import get_settings
from .core.util import now, sha
from .pipeline import LABELS

ACTIVE_FOR_DEADLINE = ("shortlisted", "preparing", "applied")
EVENT_STAGES = ("online_assessment", "interview_1", "interview_2", "hr")


def wanted_events(db, cfg: dict, t: float | None = None) -> list[dict]:
    t = t or now()
    out = []
    if cfg.get("deadlines", True):
        for o in db.all(f"SELECT id, title, organizer, deadline, official_url, stage FROM opportunities WHERE deadline>? AND "
                        f"stage IN ({','.join('?' * len(ACTIVE_FOR_DEADLINE))})", (t, *ACTIVE_FOR_DEADLINE)):
            out.append({"key": f"opp:{o['id']}:deadline:{int(o['deadline'])}", "start": o["deadline"] - 60 * int(cfg.get("deadline_minutes", 30)),
                        "minutes": int(cfg.get("deadline_minutes", 30)),
                        "title": f"⏰ Deadline: {o['title'][:80]}" + (f" ({o['organizer']})" if o.get("organizer") else ""),
                        "description": o.get("official_url") or ""})
    if cfg.get("stage_events", True):
        for o in db.all(f"SELECT id, title, organizer, stage, next_event_at, official_url FROM opportunities WHERE next_event_at>? "
                        f"AND stage IN ({','.join('?' * len(EVENT_STAGES))})", (t, *EVENT_STAGES)):
            out.append({"key": f"stage:{o['id']}:{o['stage']}:{int(o['next_event_at'])}", "start": o["next_event_at"],
                        "minutes": int(cfg.get("event_minutes", 60)),
                        "title": f"🎯 {LABELS.get(o['stage'], o['stage'])}: {(o.get('organizer') or o['title'])[:80]}",
                        "description": f"{o['title']}\n{o.get('official_url') or ''}".strip()})
    return out


def sync(ctx) -> dict:
    from .policies.engine import Policies
    cfg = Policies().get("calendar")
    cal = ctx.services.get("calendar")
    if not cfg.get("sync_enabled"):
        return {"skipped": "calendar sync is off in policies/calendar.yaml"}
    if cal is None:
        return {"skipped": "Google is not connected"}
    tz = ZoneInfo(get_settings().timezone)
    made = 0
    for ev in wanted_events(ctx.db, cfg):
        if ctx.db.scalar("SELECT 1 FROM calendar_sync WHERE key=?", (ev["key"],)):
            continue
        start = datetime.fromtimestamp(ev["start"], tz)
        end = start + timedelta(minutes=ev["minutes"])
        res = ctx.act("calendar.owner_event", f"cal:{sha(ev['key'])[:24]}",
                      lambda r: cal.create_event(r.params["title"], r.params["start"], r.params["end"], attendees=None,
                                                 send_updates=False),
                      target="owner", params={"title": ev["title"], "start": start.isoformat(), "end": end.isoformat()},
                      summary=ev["title"], wait_on_approval=False)
        # a dry run is a rehearsal: no row, so the real event is created once test mode is off
        if res.status == "succeeded" or (res.status == "duplicate" and not res.dry_run):
            ctx.db.execute("INSERT OR IGNORE INTO calendar_sync(key, event_id, start_at, title, synced_at) VALUES (?,?,?,?,?)",
                           (ev["key"], (res.result or {}).get("event_id"), ev["start"], ev["title"], now()))
            made += 1
        elif res.status == "blocked":
            return {"created": made, "blocked": res.reasons}
    return {"created": made}
