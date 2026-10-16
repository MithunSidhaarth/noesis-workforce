"""Scheduler service: fires schedules, reclaims crashed work, records liveness."""
from __future__ import annotations

import signal
import time

from workforce.core.state import configure_logging, exec_log
from workforce.factory import bootstrap
from workforce.scheduler import Scheduler

TICK_S = 15
HOUSEKEEPING_S = 60


def check_profile(db) -> None:
    """Gaps in the profile silently stall applications: say so once a day, in the brief."""
    import datetime
    from workforce.scheduler import owner_alert
    from workforce.sources.registry import load_profile, profile_problems
    probs = profile_problems(load_profile())
    if probs:
        owner_alert(db, f"profile:{datetime.date.today()}", "🧩 Your profile has gaps that will stall applications:\n"
                    + "\n".join(f"• {p}" for p in probs) + "\nFix with /profile set <key> <value> or edit config/profile.yaml.")


def main() -> None:
    configure_logging()
    db = bootstrap()
    s = Scheduler(db)

    running = True

    def _stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    last_hk = 0.0
    exec_log(db, "info", "scheduler started")
    check_profile(db)
    while running:
        try:
            fired = s.tick()
            if fired:
                exec_log(db, "info", f"scheduler fired {len(fired)} task(s)")
            if time.time() - last_hk > HOUSEKEEPING_S:
                hk = s.housekeeping()
                if any(hk.values()):
                    exec_log(db, "warning", f"housekeeping reclaimed {hk}")
                last_hk = time.time()
            db.execute("INSERT INTO system_state(key, value, updated_at, updated_by) VALUES ('scheduler_heartbeat', ?, ?, 'scheduler') "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at", (str(time.time()), time.time()))
        except Exception as e:  # noqa: BLE001
            exec_log(db, "error", f"scheduler error: {e}")
        for _ in range(TICK_S):
            if not running:
                break
            time.sleep(1)


if __name__ == "__main__":
    main()
