"""Operator CLI for use over SSH (inside the container: docker compose exec api python scripts/noesisctl.py ...).

  status                         queue depth, flags, bots, integrations
  tasks [--status S] [--bot B]   recent tasks
  run BOT KIND ['{"json": 1}']   enqueue a task now (e.g. run opportunity_scout sweep_sources)
  objective "text"               give the Chief of Staff an objective
  approvals                      pending approvals
  approve ID | deny ID           decide an approval (recorded as "cli")
  pause | resume                 global emergency stop
  dry-run on|off                 global dry-run switch
  enable BOT | disable BOT
  test-telegram                  send a real test message to TELEGRAM_OWNER_ID
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workforce.contracts import TaskSpec  # noqa: E402
from workforce.core.queue import TaskQueue  # noqa: E402
from workforce.core.state import SystemState  # noqa: E402
from workforce.core.util import iso  # noqa: E402
from workforce.factory import bootstrap  # noqa: E402
from workforce.policies.engine import decide_approval  # noqa: E402
from workforce.registry import BotRegistry  # noqa: E402


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    db = bootstrap()
    cmd, args = argv[0], argv[1:]
    st = SystemState(db)
    q = TaskQueue(db)
    if cmd == "status":
        print(f"paused={st.paused} dry_run={st.dry_run} queue={q.depth()}")
        for b in db.all("SELECT id, enabled, state, last_heartbeat FROM bots ORDER BY id"):
            print(f"  {b['id']:20} {'on ' if b['enabled'] else 'off'} {b['state']:8} heartbeat {iso(b['last_heartbeat']) if b['last_heartbeat'] else 'never'}")
        for i in db.all("SELECT name, status, detail FROM integrations ORDER BY name"):
            print(f"  [{i['status']}] {i['name']}: {i['detail'] or ''}")
    elif cmd == "tasks":
        status = args[args.index("--status") + 1] if "--status" in args else None
        bot = args[args.index("--bot") + 1] if "--bot" in args else None
        sql = "SELECT id, bot_id, kind, status, attempts, error FROM tasks WHERE (? IS NULL OR status=?) AND (? IS NULL OR bot_id=?) ORDER BY updated_at DESC LIMIT 30"
        for t in db.all(sql, (status, status, bot, bot)):
            print(f"{t['id']} {t['bot_id']:18} {t['kind']:22} {t['status']:16} {t['attempts']} {(t['error'] or '')[:80]}")
    elif cmd == "run":
        payload = json.loads(args[2]) if len(args) > 2 else {}
        tid, created = q.enqueue(TaskSpec(bot_id=args[0], kind=args[1], payload=payload, requested_by="cli"))
        print(tid, "(new)" if created else "(already queued)")
    elif cmd == "objective":
        tid, _ = q.enqueue(TaskSpec(bot_id="orchestrator", kind="objective", payload={"text": " ".join(args)}, requested_by="cli"))
        print(tid)
    elif cmd == "approvals":
        for a in db.all("SELECT id, bot_id, action, summary FROM approvals WHERE status='pending' ORDER BY created_at"):
            print(f"{a['id']} {a['bot_id']} {a['action']}: {a['summary']}")
    elif cmd in ("approve", "deny"):
        r = decide_approval(db, args[0], cmd == "approve", by="cli")
        print(r or "no pending approval with that id")
    elif cmd in ("pause", "resume"):
        st.set(SystemState.PAUSED, cmd == "pause", by="cli")
        print(f"paused={st.paused}")
    elif cmd == "dry-run":
        st.set(SystemState.DRY_RUN, args[0] == "on", by="cli")
        print(f"dry_run={st.dry_run}")
    elif cmd in ("enable", "disable"):
        BotRegistry(db).set_enabled(args[0], cmd == "enable", "cli")
        print("ok")
    elif cmd == "test-telegram":
        from workforce.factory import build_services
        tg = build_services().extra.get("telegram")
        if tg is None:
            print("Telegram is not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_OWNER_ID).")
            return 1
        print("bot:", tg.me().get("username"))
        print("sent message id:", tg.send_owner("NOESIS test message: Telegram is connected.").get("message_id"))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
