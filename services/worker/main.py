"""Worker service.

  python -m services.worker.main                  # all enabled bots, one process each (isolated)
  python -m services.worker.main --shared         # all bots as separate runtimes in one process (low RAM)
  python -m services.worker.main --bots a,b       # only these bots

Isolated mode runs a supervisor that restarts a crashed bot process with
exponential backoff, so one bot failing never stops the others.
"""
from __future__ import annotations

import argparse
import asyncio
import multiprocessing as mp
import os
import signal
import sys
import time

from workforce.core.db import get_db
from workforce.core.notify import subscribe
from workforce.core.state import configure_logging, exec_log


def enabled_bots(only: list[str] | None) -> list[str]:
    rows = get_db().all("SELECT id FROM bots WHERE enabled=1 ORDER BY id")
    ids = [r["id"] for r in rows]
    return [b for b in ids if not only or b in only]


async def run_runtimes(bot_ids: list[str]) -> None:
    from workforce.factory import build_services
    from workforce.runtime import BotRuntime
    services = build_services()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    wakes = {b: asyncio.Event() for b in bot_ids}
    subscribe(lambda bid: wakes.get(bid) and loop.call_soon_threadsafe(wakes[bid].set))
    await asyncio.gather(*(BotRuntime(b, services).run_forever(wakes[b], stop) for b in bot_ids))


def _child(bot_id: str) -> None:
    configure_logging()
    asyncio.run(run_runtimes([bot_id]))


WATCHDOG_S = 180      # a live process whose bot has not heartbeated for this long is hung
RESCAN_S = 30         # pick up bots enabled/disabled from the dashboard without a restart


def _stale(bot_id: str, started: float) -> bool:
    if time.time() - started < WATCHDOG_S:
        return False
    hb = get_db().scalar("SELECT last_heartbeat FROM bots WHERE id=?", (bot_id,))
    return hb is None or time.time() - hb > WATCHDOG_S


def supervise(bot_ids: list[str], only: list[str] | None = None) -> None:
    procs: dict[str, tuple[mp.Process, float, int]] = {}
    stopping = False
    last_scan = time.time()

    def _stop(*_):
        nonlocal stopping
        stopping = True
        for p, _, _ in procs.values():
            p.terminate()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    restarts: dict[str, int] = {b: 0 for b in bot_ids}
    next_start: dict[str, float] = {b: 0 for b in bot_ids}
    while not stopping:
        if time.time() - last_scan > RESCAN_S:
            last_scan = time.time()
            try:
                wanted = enabled_bots(only)
            except Exception:  # noqa: BLE001
                wanted = bot_ids
            for b in [b for b in procs if b not in wanted]:
                exec_log(get_db(), "info", "bot disabled; stopping its process", bot_id=b)
                procs.pop(b)[0].terminate()
            for b in wanted:
                restarts.setdefault(b, 0)
                next_start.setdefault(b, 0)
            bot_ids = wanted
        for b in bot_ids:
            cur = procs.get(b)
            if cur and cur[0].is_alive():
                if _stale(b, cur[1]):
                    exec_log(get_db(), "error", f"bot heartbeat older than {WATCHDOG_S}s; killing the hung process", bot_id=b)
                    cur[0].kill()
                    cur[0].join(timeout=10)
                elif time.time() - cur[1] > 600:
                    restarts[b] = 0  # healthy for 10 min -> reset backoff
                    continue
                else:
                    continue
            if cur:
                restarts[b] += 1
                delay = min(300, 2 ** restarts[b])
                next_start[b] = time.time() + delay
                exec_log(get_db(), "error", f"bot process exited (code {cur[0].exitcode}); restarting in {delay}s", bot_id=b)
                procs.pop(b)
            if time.time() >= next_start[b]:
                p = mp.Process(target=_child, args=(b,), name=f"bot-{b}", daemon=False)
                p.start()
                procs[b] = (p, time.time(), restarts[b])
        time.sleep(2)
    for p, _, _ in procs.values():
        p.join(timeout=70)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bots", default="")
    ap.add_argument("--shared", action="store_true")
    a = ap.parse_args()
    a.shared = a.shared or os.environ.get("NOESIS_WORKER_MODE", "isolated").lower() == "shared"
    configure_logging()
    from workforce.factory import bootstrap
    bootstrap()
    only = [b for b in a.bots.split(",") if b] or None
    ids = enabled_bots(only)
    if not ids:
        print("no enabled bots", file=sys.stderr)
        sys.exit(1)
    if a.shared:
        asyncio.run(run_runtimes(ids))
    else:
        mp.set_start_method("spawn", force=True)
        supervise(ids, only)


if __name__ == "__main__":
    main()
