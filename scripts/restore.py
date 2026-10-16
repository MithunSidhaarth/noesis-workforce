"""Restore a backup produced by scripts/backup.py.

  docker compose stop worker scheduler telegram api
  docker compose run --rm --no-deps api python scripts/restore.py /data/backups/noesis-YYYYmmdd-HHMMSS.tar.gz
  docker compose up -d

The current database is kept as noesis.db.pre-restore-<time> before being replaced.
Config, policies and prompts are restored into $NOESIS_DATA_DIR/restore-<time>/ for you to compare;
they are not overwritten automatically.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main(archive: str) -> None:
    db_path = Path(os.environ.get("NOESIS_DB_PATH", ROOT / "data" / "noesis.db"))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    work = Path(os.environ.get("NOESIS_DATA_DIR", ROOT / "data")) / f"restore-{stamp}"
    with tarfile.open(archive) as tar:
        tar.extractall(work, filter="data")
    restored = work / "noesis.db"
    chk = sqlite3.connect(restored)
    ok = chk.execute("PRAGMA integrity_check").fetchone()[0]
    chk.close()
    if ok != "ok":
        sys.exit("backup database failed its integrity check; nothing changed")
    if db_path.exists():
        shutil.copy2(db_path, db_path.with_name(f"{db_path.name}.pre-restore-{stamp}"))
        for ext in ("-wal", "-shm"):
            Path(str(db_path) + ext).unlink(missing_ok=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(restored, db_path)
    print(f"database restored to {db_path}")
    print(f"config/policies/prompts from the backup are in {work} for comparison")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
