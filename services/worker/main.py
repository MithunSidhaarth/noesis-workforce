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
from workforce.core.settings import get_settings
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
    os.environ["NOESIS_WORKER_ID"] = f"{get_settings().worker_id}-{bot_id}"
    asyncio.run(run_runtimes([bot_id]))


def supervise(bot_ids: list[str]) -> None:
    procs: dict[str, tuple[mp.Process, float, int]] = {}
    stopping = False

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
        for b in bot_ids:
            cur = procs.get(b)
            if cur and cur[0].is_alive():
                if time.time() - cur[1] > 600:
                    restarts[b] = 0  # healthy for 10 min -> reset backoff
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
    configure_logging()
    from workforce.factory import bootstrap
    bootstrap()
    ids = enabled_bots([b for b in a.bots.split(",") if b])
    if not ids:
        print("no enabled bots", file=sys.stderr)
        sys.exit(1)
    if a.shared:
        asyncio.run(run_runtimes(ids))
    else:
        mp.set_start_method("spawn", force=True)
        supervise(ids)


if __name__ == "__main__":
    main()
