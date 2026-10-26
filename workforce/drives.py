"""Off-campus drive radar (config/drives.yaml).

Big yearly programmes (Google STEP, Microsoft Engage, Amazon WoW, Flipkart GRiD,
J.P. Morgan Code for Good, Goldman Sachs) don't show up on job boards until they open,
and they close fast. The radar warns you a month before the usual window, again when
it is usually open, and, for drives with watch: true, when the official page changes
(a hash of its visible text, fetched politely at most once a day).
The months are history, not a promise: every alert says to check the official page.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from .core.settings import get_settings
from .core.util import now, sha

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
PAGE_CHECK_S = 86400


def load_drives(config_dir: str | None = None) -> list[dict]:
    p = Path(config_dir or get_settings().config_dir) / "drives.yaml"
    if not p.exists():
        return []
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    out = []
    for d in data.get("drives") or []:
        if d.get("id") and d.get("name") and d.get("opens"):
            d["opens"] = sorted({int(m) for m in d["opens"] if 1 <= int(m) <= 12})
            out.append(d)
    return out


def phase(drive: dict, t: float | None = None) -> str | None:
    """'soon' the month before the usual window, 'open' during it, else None."""
    m = datetime.fromtimestamp(t or now(), ZoneInfo(get_settings().timezone)).month
    opens = drive["opens"]
    if m in opens:
        return "open"
    if (m % 12) + 1 == opens[0]:
        return "soon"
    return None


def window_label(drive: dict) -> str:
    o = drive["opens"]
    return MONTHS[o[0] - 1] + (f"–{MONTHS[o[-1] - 1]}" if len(o) > 1 else "")


def status(db, t: float | None = None) -> list[dict]:
    out = []
    for d in load_drives():
        st = db.one("SELECT * FROM drive_state WHERE id=?", (d["id"],)) or {}
        out.append({**d, "phase": phase(d, t), "window": window_label(d), "page_changed_at": st.get("page_changed_at"),
                    "page_checked_at": st.get("page_checked_at")})
    return out


def alert_text(d: dict, ph: str) -> str:
    head = {"soon": f"📅 {d['name']} usually opens next month ({window_label(d)})",
            "open": f"🚀 {d['name']} is usually open around now ({window_label(d)})",
            "changed": f"🔄 The {d['name']} page just changed"}[ph]
    return "\n".join([head, "", f"Who: {d.get('eligibility') or 'check the page'}", f"Official page: {d['url']}", "",
                      "Dates move every year, so check the page. If it's open, send /add <link> to track it."])


def radar(ctx, t: float | None = None) -> dict:
    t = t or now()
    year = datetime.fromtimestamp(t, ZoneInfo(get_settings().timezone)).year
    fetcher = ctx.services.get("fetcher")
    sent, changed = 0, 0
    for d in load_drives():
        ctx.db.execute("INSERT OR IGNORE INTO drive_state(id) VALUES (?)", (d["id"],))
        st = ctx.db.one("SELECT * FROM drive_state WHERE id=?", (d["id"],))
        ph = phase(d, t)
        if ph and st.get("last_alert_key") != f"{year}:{ph}":
            ctx.notify_owner(alert_text(d, ph), key=f"drive:{d['id']}:{year}:{ph}", urgent=(ph == "open"))
            ctx.db.execute("UPDATE drive_state SET last_alert_key=? WHERE id=?", (f"{year}:{ph}", d["id"]))
            sent += 1
        if d.get("watch") and fetcher is not None and t - (st.get("page_checked_at") or 0) >= PAGE_CHECK_S:
            try:
                from .agents.opportunity_scout import page_title_and_text
                r = fetcher.get(d["url"], max_bytes=3_000_000)
                h = sha(page_title_and_text(r.text)[1])
            except Exception as e:  # noqa: BLE001  (robots, 403, offline: try again tomorrow)
                ctx.log(f"drive page {d['id']} not checked: {e}", level="debug")
                ctx.db.execute("UPDATE drive_state SET page_checked_at=? WHERE id=?", (t, d["id"]))
                continue
            if st.get("page_hash") and st["page_hash"] != h:
                ctx.notify_owner(alert_text(d, "changed"), key=f"drive:{d['id']}:changed:{h[:12]}", urgent=True)
                ctx.db.execute("UPDATE drive_state SET page_changed_at=? WHERE id=?", (t, d["id"]))
                changed += 1
            ctx.db.execute("UPDATE drive_state SET page_hash=?, page_checked_at=? WHERE id=?", (h, t, d["id"]))
    return {"alerts": sent, "pages_changed": changed}
