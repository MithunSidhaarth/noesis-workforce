"""Ready-made reels: a folder of finished videos plus a manifest, posted one per slot.

manifest.yaml in the folder:
    brand: cue                 # config/brands.yaml id (Cue posts on @msthinks with cue.notch as collaborator)
    series: cue-launch
    reels:
      - file: 00-intro.mp4
        posted: true           # already on Instagram: skipped
        caption: ...
      - file: 01-approvals.mp4
        title: Approve your agent from the notch
        caption: |
          Your AI agent wants to run a command...
Each reel is copied into data/media/ready/ (the folder the bots can read) and a
post_ready_reel task is queued for its slot. The task goes through QC and the
publishing policy, so nothing posts while publishing is disabled or dry-run is on.
"""
from __future__ import annotations

import hashlib
import shutil
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from .contracts import TaskSpec
from .core.db import Database
from .core.queue import TaskQueue
from .core.settings import get_settings
from .core.util import now


class ReelQueueError(ValueError):
    pass


def load_manifest(folder: str | Path) -> dict:
    folder = Path(folder)
    mf = folder / "manifest.yaml"
    if not mf.is_file():
        raise ReelQueueError(f"no manifest.yaml in {folder}")
    m = yaml.safe_load(mf.read_text(encoding="utf-8")) or {}
    reels = m.get("reels") or []
    if not reels:
        raise ReelQueueError("manifest.yaml lists no reels")
    for r in reels:
        if not (folder / str(r.get("file") or "")).is_file():
            raise ReelQueueError(f"missing video: {r.get('file')}")
        if not str(r.get("caption") or "").strip():
            raise ReelQueueError(f"{r['file']} has no caption")
        if len(str(r["caption"])) > 2200:
            raise ReelQueueError(f"{r['file']} caption is over Instagram's 2,200 characters")
    return m


def slots(count: int, every_hours: float = 3, window: str = "09:00-23:00", start: datetime | None = None) -> list[float]:
    """Posting times `every_hours` apart, kept inside the daily `window` (local time);
    a slot that would fall outside it moves to the window's start the next morning."""
    tz = ZoneInfo(get_settings().timezone)
    (h1, m1), (h2, m2) = (map(int, t.strip().split(":")) for t in window.split("-"))
    t = (start or datetime.fromtimestamp(now(), tz)) + timedelta(minutes=10)
    t = t.replace(second=0, microsecond=0)
    out = []
    while len(out) < count:
        open_, close = datetime.combine(t.date(), dtime(h1, m1), tz), datetime.combine(t.date(), dtime(h2, m2), tz)
        if t < open_:
            t = open_
        elif t > close:
            t = open_ + timedelta(days=1)
            continue
        out.append(t.timestamp())
        t += timedelta(hours=every_hours)
    return out


def queue_reels(db: Database, folder: str | Path, by: str, brand: str | None = None, every_hours: float = 3,
                window: str = "09:00-23:00") -> list[dict]:
    """Queue every reel in the manifest that isn't marked `posted: true`."""
    from .brands import get_brand
    folder = Path(folder)
    m = load_manifest(folder)
    brand = brand or m.get("brand")
    if brand and not get_brand(brand):
        raise ReelQueueError(f"unknown brand {brand!r}; check config/brands.yaml")
    dest = Path(get_settings().data_dir) / "media" / "ready"
    dest.mkdir(parents=True, exist_ok=True)
    q = TaskQueue(db)
    out = []
    todo = [r for r in m["reels"] if not r.get("posted")]
    for r, ts in zip(todo, slots(len(todo), every_hours, window)):
        src = folder / r["file"]
        digest = hashlib.sha256(src.read_bytes()).hexdigest()[:12]
        target = dest / f"{digest}-{Path(r['file']).name}"
        if not target.exists():
            shutil.copyfile(src, target)
        tid, created = q.enqueue(TaskSpec(
            bot_id="content_studio", kind="post_ready_reel", requested_by=by, run_after=ts,
            idempotency_key=f"ready-reel:{digest}:{brand}",
            payload={"path": str(target), "caption": str(r["caption"]).strip(), "title": r.get("title"), "brand": brand,
                     "hook": r.get("title") or str(r["caption"]).split("\n")[0],
                     "series": m.get("series"), "category": r.get("category") or m.get("category") or "project_update"}))
        out.append({"file": r["file"], "title": r.get("title") or str(r["caption"]).split("\n")[0], "at": ts,
                    "task": tid, "new": created})
    return out


def upcoming(db: Database, limit: int = 20) -> list[dict]:
    from .core.db import loads
    rows = db.all("SELECT id, status, run_after, payload_json FROM tasks WHERE kind='post_ready_reel' AND status IN ('pending','queued','deferred') "
                  "ORDER BY run_after LIMIT ?", (limit,))
    return [{"task": r["id"], "status": r["status"], "at": r["run_after"],
             "title": (loads(r["payload_json"], {}) or {}).get("title")} for r in rows]


def next_slot(db: Database, brand: str | None, every_hours: float = 3, window: str = "09:00-23:00") -> float:
    """The first free slot after the last reel already queued for this brand."""
    last = db.scalar("SELECT MAX(run_after) FROM tasks WHERE kind='post_ready_reel' AND status IN ('pending','queued','deferred') "
                     "AND json_extract(payload_json, '$.brand') IS ?", (brand,))
    tz = ZoneInfo(get_settings().timezone)
    start = None
    if last and last + every_hours * 3600 > now() + 600:
        start = datetime.fromtimestamp(last + every_hours * 3600 - 600, tz)
    return slots(1, every_hours, window, start=start)[0]


def queued_count(db: Database, brand: str | None) -> int:
    return db.scalar("SELECT COUNT(*) FROM tasks WHERE kind='post_ready_reel' AND status IN ('pending','queued','deferred') "
                     "AND json_extract(payload_json, '$.brand') IS ?", (brand,))


def used_hooks(db: Database, brand: str | None) -> set[str]:
    """Hooks of every reel made or queued for this brand, so no script is reused."""
    rows = db.all("SELECT json_extract(brief_json, '$.hook') AS h FROM content_items WHERE brand IS ? AND format='reel'", (brand,))
    rows += db.all("SELECT json_extract(payload_json, '$.hook') AS h FROM tasks WHERE kind='post_ready_reel' "
                   "AND json_extract(payload_json, '$.brand') IS ?", (brand,))
    return {norm(r["h"]) for r in rows if r["h"]}


def norm(text: str) -> str:
    return " ".join("".join(c for c in str(text).lower() if c.isalnum() or c.isspace()).split())
