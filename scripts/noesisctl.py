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
  test-ntfy                      push a real test notification to NTFY_TOPIC
  reels FOLDER [--brand B] [--every-hours 3] [--window 09:00-23:00]
                                 queue finished reels from FOLDER/manifest.yaml, one per slot
  reels list                     reels waiting for their slot
  doctor                         check configuration and health; prints what to fix
  confirm REF [URL] | skip REF [REASON] | outcome REF accepted|rejected|no_response
  profile-set KEY VALUE          add a verified fact (same as Telegram /profile set)
  prune                          run the data-retention cleanup now
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
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
    elif cmd == "doctor":
        return doctor(db)
    elif cmd in ("confirm", "skip", "outcome", "profile-set"):
        from workforce import owner_actions as oa
        try:
            if cmd == "confirm":
                print(oa.confirm_opportunity(db, args[0], by="cli", url=args[1] if len(args) > 1 else None)["opportunity"]["title"])
            elif cmd == "skip":
                print(oa.skip_opportunity(db, args[0], by="cli", reason=" ".join(args[1:]))["title"])
            elif cmd == "outcome":
                print(oa.record_outcome(db, args[0], args[1], by="cli")["outcome"])
            else:
                print(oa.set_profile_fact(db, args[0], " ".join(args[1:]), by="cli"))
        except (oa.OwnerActionError, IndexError) as e:
            print(f"error: {e or 'missing argument'}")
            return 1
    elif cmd == "reels":
        from workforce import reels as rl
        if not args or args[0] == "list":
            items = rl.upcoming(db)
            print("\n".join(f"{iso(r['at'])}  {r['title'] or '(untitled)'}  [{r['status']}]" for r in items) or "No reels waiting.")
            return 0
        opt = {k: args[args.index(k) + 1] for k in ("--brand", "--every-hours", "--window") if k in args}
        try:
            out = rl.queue_reels(db, args[0], by="cli", brand=opt.get("--brand"), every_hours=float(opt.get("--every-hours", 3)),
                                 window=opt.get("--window", "09:00-23:00"))
        except (rl.ReelQueueError, ValueError) as e:
            print(f"error: {e}")
            return 1
        for r in out:
            print(f"{iso(r['at'])}  {r['title']}" + ("" if r["new"] else "  (already queued)"))
        print(f"{len(out)} reel(s) queued. Each goes through QC and the publishing policy at its time; "
              f"with dry-run on, nothing is posted.")
        return 0
    elif cmd == "test-ntfy":
        from integrations.ntfy.client import NtfyClient
        push = NtfyClient.from_env()
        if push is None:
            print("ntfy is not configured (set NTFY_TOPIC in .env).")
            return 1
        print(push.push("✅ NOESIS can reach your phone\n\nThis is a test push from noesisctl.")["evidence"])
    elif cmd == "prune":
        from workforce.scheduler import prune
        print(prune(db))
    else:
        print(__doc__)
        return 2
    return 0


def doctor(db) -> int:
    """Read-only checks. Exit code 1 if anything is broken (not just a warning)."""
    from workforce.core.settings import get_settings
    from workforce.sources.registry import load_profile, profile_problems
    s = get_settings()
    bad, warn, ok = [], [], []

    def check(cond: bool, good: str, problem: str, fatal: bool = True):
        (ok if cond else (bad if fatal else warn)).append(good if cond else problem)

    check(len(os.environ.get("NOESIS_SESSION_SECRET", "")) >= 32, "session secret set",
          "NOESIS_SESSION_SECRET missing or shorter than 32 chars")
    check(os.environ.get("NOESIS_ADMIN_PASSWORD_HASH", "").startswith("pbkdf2_sha256"), "dashboard password hash set",
          "NOESIS_ADMIN_PASSWORD_HASH missing: run scripts/hash_password.py")
    if "docker-compose.private.yml" in os.environ.get("COMPOSE_FILE", ""):
        ok.append("private mode: dashboard only over Tailscale, no public ports")
    else:
        dom = os.environ.get("NOESIS_DOMAIN", "")
        check(bool(dom) and not dom.endswith("example.com"), f"domain {dom}",
              "NOESIS_DOMAIN not set: set your domain, or use private mode (docs/ORACLE.md)")
    if s.telegram_token:
        check(s.telegram_owner_id.isdigit(), "Telegram owner id is numeric",
              "TELEGRAM_OWNER_ID must be your numeric Telegram user id (ask @userinfobot)")
    else:
        warn.append("Telegram not configured: you will get no alerts or morning brief")
    migrated = {r["name"] for r in db.all("SELECT name FROM schema_migrations")}
    files = {f.name for f in (Path(__file__).resolve().parents[1] / "database" / "migrations").glob("*.sql")}
    check(files <= migrated, f"database migrated ({len(migrated)} migrations)", f"migrations not applied: {sorted(files - migrated)}")
    if s.vault_path:
        p = Path(s.vault_path)
        check(p.is_dir() and os.access(p, os.W_OK), f"vault writable at {p}", f"vault path {p} missing or not writable", fatal=False)
    else:
        warn.append("NOESIS_VAULT_PATH not set: notes are staged in data/vault-staging")
    providers = [k for k in ("ANTHROPIC_API_KEY", "GROQ_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY") if os.environ.get(k)]
    check(bool(providers) or os.environ.get("OLLAMA_ENABLED") == "true", f"model providers: {', '.join(providers) or 'ollama'}",
          "no model provider configured: bots run deterministic-only (fine, but no drafting)", fatal=False)
    for p in profile_problems(load_profile()):
        warn.append(f"profile: {p}")
    st = SystemState(db)
    if st.dry_run:
        warn.append("dry-run is ON: nothing goes out for real (turn off in Settings when ready)")
    if st.paused:
        warn.append("emergency pause is ON")
    stale = db.all("SELECT id FROM bots WHERE enabled=1 AND (last_heartbeat IS NULL OR last_heartbeat < ?)", (time.time() - 300,))
    check(not stale, "all enabled bots heartbeating", f"bots not heartbeating: {', '.join(b['id'] for b in stale)}")
    hb = db.scalar("SELECT value FROM system_state WHERE key='scheduler_heartbeat'")
    check(bool(hb) and time.time() - float(hb) < 120, "scheduler alive", "scheduler heartbeat missing or stale")
    for i in db.all("SELECT name, status, detail FROM integrations WHERE status IN ('revoked','error')"):
        bad.append(f"integration {i['name']} is {i['status']}: {i['detail']}")
    unknown = db.scalar("SELECT COUNT(*) FROM external_actions WHERE status='unknown'")
    check(not unknown, "no actions with unknown outcome", f"{unknown} interrupted action(s) need checking (dashboard Logs)")
    free = shutil.disk_usage(s.data_dir if Path(s.data_dir).exists() else ".").free / 1e9
    check(free > 2, f"disk free {free:.1f} GB", f"low disk: {free:.1f} GB free", fatal=free < 0.5)
    backups = sorted(Path(s.data_dir, "backups").glob("*")) if Path(s.data_dir, "backups").exists() else []
    check(bool(backups) and time.time() - backups[-1].stat().st_mtime < 2 * 86400, "recent backup present",
          "no backup in the last 2 days (see DEPLOYMENT.md step 10)", fatal=False)
    check(shutil.which("ffmpeg") is not None, "ffmpeg installed", "ffmpeg missing: reels cannot be rendered", fatal=False)
    for x in ok:
        print(f"  ok    {x}")
    for x in warn:
        print(f"  warn  {x}")
    for x in bad:
        print(f"  FIX   {x}")
    print(f"\n{len(bad)} problem(s), {len(warn)} warning(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
