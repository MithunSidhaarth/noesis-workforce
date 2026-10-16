"""Consistent online backup of the database plus config and generated media.

  python scripts/backup.py [--dest DIR] [--keep 14]     (default DIR: $NOESIS_DATA_DIR/backups)

Uses SQLite's backup API, so it is safe while bots are running. The vault is not
included: it is synced with your own computer and Obsidian is its source of truth.
Secrets (.env) are not included either; keep them in your password manager.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def backup(dest: Path, keep: int) -> Path:
    db_path = Path(os.environ.get("NOESIS_DB_PATH", ROOT / "data" / "noesis.db"))
    data_dir = Path(os.environ.get("NOESIS_DATA_DIR", ROOT / "data"))
    dest.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    snap = dest / f"noesis-{stamp}.db"
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(snap)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    chk = sqlite3.connect(snap)
    ok = chk.execute("PRAGMA integrity_check").fetchone()[0]
    chk.close()
    if ok != "ok":
        snap.unlink()
        sys.exit(f"integrity check failed: {ok}")
    archive = dest / f"noesis-{stamp}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(snap, arcname="noesis.db")
        for sub in ("config", "policies", "prompts"):
            p = ROOT / sub
            if p.exists():
                tar.add(p, arcname=sub)
        media = data_dir / "media"
        if media.exists():
            tar.add(media, arcname="media")
    snap.unlink()
    old = sorted(dest.glob("noesis-*.tar.gz"))[:-keep] if keep > 0 else []
    for o in old:
        o.unlink()
    return archive


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", default=str(Path(os.environ.get("NOESIS_DATA_DIR", ROOT / "data")) / "backups"))
    ap.add_argument("--keep", type=int, default=14)
    a = ap.parse_args()
    print(backup(Path(a.dest), a.keep))
