"""The owner feedback loop: batched notifications, Telegram buttons, confirm/skip,
reminders, profile facts, outcomes, briefs and housekeeping."""
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from workforce import notifications as nt
from workforce import owner_actions as oa
from workforce.contracts import TaskSpec
from workforce.core.db import dumps, loads
from workforce.runtime import BotRuntime


def add_opp(db, title="Open Source AI Fellowship", days=10, status="needs_review", score=0.8, typ="fellowship", **kw):
    oid = f"opp_{abs(hash((title, days))) % 10**12:012d}abcdef"
    db.execute("INSERT INTO opportunities(id, fingerprint, title, official_url, discovery_source, type, deadline, discovered_at, "
               "verification_status, relevance_score, eligibility_json, raw_json, verification_notes_json) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (oid, oid, title, kw.get("url", "https://aggregator.example.net/x"), "fixture", typ,
                time.time() + days * 86400 if days is not None else None, time.time(), status, score,
                dumps(["Open to undergraduate students"]),
                dumps({"raw_text": title, "candidate_links": ["https://fellowship.example.org/apply"]}),
                dumps(["listing comes from an aggregator; official organizer page not confirmed (aggregator.example.net)"])))
    return db.one("SELECT * FROM opportunities WHERE id=?", (oid,))


def drain(services, bots, rounds=20):
    rts = [BotRuntime(b, services, "t") for b in bots]
    for _ in range(rounds):
        if sum(rt.step() for rt in rts) == 0:
            return


# ---- notifications ---------------------------------------------------------------
def test_quiet_hours_and_daily_cap(env, services, monkeypatch):
    tz = ZoneInfo("Asia/Kolkata")
    at = lambda h, m=0: datetime.now(tz).replace(hour=h, minute=m, second=0, microsecond=0).timestamp()  # noqa: E731
    assert nt.in_quiet_hours(at(23, 30), "23:00-07:30") and nt.in_quiet_hours(at(3), "23:00-07:30")
    assert not nt.in_quiet_hours(at(7, 31), "23:00-07:30") and not nt.in_quiet_hours(at(12), "23:00-07:30")
    assert not nt.in_quiet_hours(at(3), "off")

    from workforce.agents.base import AgentContext
    ctx = AgentContext(services.registry.get("personal_ops"), env.db, services.queue, services.bus, services.gateway,
                       services.router, "t", services=services.extra)
    tg = services.extra["telegram"]
    assert ctx.notify_owner("FYI", key="k1") is None and tg.sent == []           # non-urgent: held for the brief
    assert ctx.notify_owner("Now!", key="k2", urgent=True).status == "succeeded"  # urgent: sent
    assert ctx.notify_owner("Now!", key="k2", urgent=True) is None and len(tg.sent) == 1   # deduplicated

    monkeypatch.setenv("NOESIS_MAX_PINGS_PER_DAY", "1")
    assert ctx.notify_owner("Second urgent", key="k3", urgent=True) is None       # over the cap: held
    monkeypatch.setenv("NOESIS_QUIET_HOURS", "00:00-23:59")
    monkeypatch.setenv("NOESIS_MAX_PINGS_PER_DAY", "50")
    assert ctx.notify_owner("Night urgent", key="k4", urgent=True) is None        # quiet hours: held
    assert ctx.notify_owner("Brief", key="k5", force_now=True).status == "succeeded"   # the brief itself always goes
    # when quiet hours end, Personal Ops flushes the held urgent ones (not the FYI)
    monkeypatch.setenv("NOESIS_QUIET_HOURS", "off")
    services.queue.enqueue(TaskSpec(bot_id="personal_ops", kind="send_due_reminders"))
    drain(services, ["personal_ops"])
    assert "Second urgent" in tg.sent and "Night urgent" in tg.sent and "FYI" not in tg.sent


def test_reminder_not_marked_sent_without_telegram(env, services):
    services.extra.pop("telegram")
    env.db.execute("INSERT INTO reminders(id, title, due_at, source, dedupe_key, created_at) VALUES ('r1','x',0,'owner','r1',0)")
    services.queue.enqueue(TaskSpec(bot_id="personal_ops", kind="send_due_reminders"))
    drain(services, ["personal_ops"])
    assert env.db.scalar("SELECT status FROM reminders WHERE id='r1'") == "pending"   # still owed, not silently dropped


# ---- approvals with buttons ------------------------------------------------------
def test_approval_is_pushed_with_buttons_and_decided_by_tap(env, services):
    from services.telegram.bot import CommandHandler
    tid, _ = services.queue.enqueue(TaskSpec(bot_id="echo_a", kind="send_email", payload={"key": "b1", "to": "x@corp.com"}))
    BotRuntime("echo_a", services, "t").step()
    tg = services.extra["telegram"]
    msg, buttons = tg.sent[-1], tg.buttons[-1]
    assert "needs your OK" in msg and "Why it's asking" in msg and "•" in msg
    approve = buttons[0][0]["data"]
    assert approve.startswith("a:") and approve.endswith(":y") and len(approve) <= 64

    h = CommandHandler(env.db, owner_id=111)
    stranger = h.handle({"callback_query": {"id": "c0", "from": {"id": 999}, "data": approve,
                                            "message": {"chat": {"id": 999, "type": "private"}, "message_id": 5, "text": msg}}})
    assert stranger["answer"] == "Not allowed." and services.queue.get(tid).status == "waiting_approval"
    out = h.handle({"callback_query": {"id": "c1", "from": {"id": 111}, "data": approve,
                                       "message": {"chat": {"id": 111, "type": "private"}, "message_id": 5, "text": msg}}})
    assert "Approved" in out["answer"] and "Approved" in out["edit"] and out["message_id"] == 5
    BotRuntime("echo_a", services, "t").step()
    assert services.queue.get(tid).status == "succeeded"
    again = h.handle({"callback_query": {"id": "c2", "from": {"id": 111}, "data": approve,
                                         "message": {"chat": {"id": 111, "type": "private"}, "message_id": 5}}})
    assert "already granted" in again["answer"]


def test_inbox_is_numbered_and_approve_by_number(env, services):
    from services.telegram.bot import CommandHandler
    services.queue.enqueue(TaskSpec(bot_id="echo_a", kind="send_email", payload={"key": "n1", "to": "x@corp.com"}))
    BotRuntime("echo_a", services, "t").step()
    h = CommandHandler(env.db, owner_id=111)
    say = lambda t: h.handle({"message": {"from": {"id": 111}, "chat": {"id": 111, "type": "private"}, "text": t}})  # noqa: E731
    assert say("/inbox").startswith("1. ")
    assert "Approved" in say("/approve 1")
    assert "Nothing waiting" in say("/inbox")
    assert "No item #3" in say("/approve 3")


# ---- confirm / skip / remind ----------------------------------------------------
def test_confirm_unblocks_application_and_schedules_reminders(env, services):
    from services.telegram.bot import CommandHandler
    opp = add_opp(env.db)
    card, buttons = oa.opportunity_card(opp)
    assert "Closes in 9 days" in card or "Closes in 10 days" in card
    assert "listing site" in card and "Possible official page (unconfirmed): https://fellowship.example.org/apply" in card
    h = CommandHandler(env.db, owner_id=111)
    say = lambda t: h.handle({"message": {"from": {"id": 111}, "chat": {"id": 111, "type": "private"}, "text": t}})  # noqa: E731
    assert "must be a full https" in say(f"/confirm {oa.ref(opp['id'])} javascript:alert(1)")
    assert "Confirmed" in say(f"/confirm {oa.ref(opp['id'])} https://fellowship.example.org/apply")
    o = env.db.one("SELECT * FROM opportunities WHERE id=?", (opp["id"],))
    assert o["verification_status"] == "verified" and o["confirmed_by"] == "telegram:owner"
    assert o["official_url"] == "https://fellowship.example.org/apply"
    assert env.db.scalar("SELECT COUNT(*) FROM reminders WHERE ref_id=? AND status='pending'", (opp["id"],)) == 3
    drain(services, ["applications"])
    app = env.db.one("SELECT * FROM applications WHERE opportunity_id=?", (opp["id"],))
    assert app["state"] == "REVIEW"
    assert any(e["event"] == "opportunity.confirmed" for e in env.db.all("SELECT event FROM audit_log"))


def test_skip_button_closes_and_cancels_reminders(env, services):
    from services.telegram.bot import CommandHandler
    opp = add_opp(env.db, title="Crypto Hackathon", typ="hackathon")
    oa.schedule_deadline_reminders(env.db, opp, "test")
    h = CommandHandler(env.db, owner_id=111)
    out = h.handle({"callback_query": {"id": "c", "from": {"id": 111}, "data": f"o:{opp['id']}:s",
                                       "message": {"chat": {"id": 111, "type": "private"}, "message_id": 1, "text": "card"}}})
    assert "Skipped" in out["answer"]
    o = env.db.one("SELECT * FROM opportunities WHERE id=?", (opp["id"],))
    assert o["application_status"] == "CLOSED" and o["skip_reason"]
    assert env.db.scalar("SELECT COUNT(*) FROM reminders WHERE ref_id=? AND status='pending'", (opp["id"],)) == 0
    assert opp["id"] not in [r["id"] for r in oa.shortlist(env.db)]


def test_shortlist_orders_by_deadline_and_numbers_resolve(env, services):
    later = add_opp(env.db, title="Later Grant", days=20, typ="grant")
    sooner = add_opp(env.db, title="Sooner Internship", days=3, typ="internship")
    add_opp(env.db, title="Weak Match", days=2, score=0.1)
    rows = oa.shortlist(env.db)
    assert [r["id"] for r in rows] == [sooner["id"], later["id"]]
    assert oa.find_opportunity(env.db, "2")["id"] == later["id"]
    assert len(oa.shortlist(env.db, include_all=True)) == 3


def test_remind_parser_is_strict():
    base = datetime(2026, 10, 16, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata")).timestamp()
    due, what = oa.parse_when("tomorrow 09:00 call the bank", base)
    assert datetime.fromtimestamp(due, ZoneInfo("Asia/Kolkata")) == datetime(2026, 10, 17, 9, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert what == "call the bank"
    assert oa.parse_when("in 2h submit", base)[0] == base + 7200
    assert datetime.fromtimestamp(oa.parse_when("fri 18:00 essay", base)[0], ZoneInfo("Asia/Kolkata")).day == 16  # today is Friday
    assert datetime.fromtimestamp(oa.parse_when("fri 08:00 essay", base)[0], ZoneInfo("Asia/Kolkata")).day == 23  # next week
    for bad in ("next tuesday-ish do it", "tomorrow 25:00 x", "2026-10-01 10:00 past", "soon call mom"):
        with pytest.raises(oa.OwnerActionError):
            oa.parse_when(bad, base)


# ---- profile, missing, outcomes ---------------------------------------------------
def test_profile_facts_typed_by_owner_are_used_and_refresh_applications(env, services):
    from services.telegram.bot import CommandHandler
    from workforce.sources.registry import load_profile
    opp = add_opp(env.db, status="verified")
    oa.confirm_opportunity(env.db, opp["id"], by="test")
    drain(services, ["applications"])
    h = CommandHandler(env.db, owner_id=111)
    say = lambda t: h.handle({"message": {"from": {"id": 111}, "chat": {"id": 111, "type": "private"}, "text": t}})  # noqa: E731
    assert "short_bio" in say("/missing")
    out = say("/profile set short_bio I build open-source AI tools.")
    assert "Saved short_bio (answers)" in out and "Re-preparing 1" in out
    assert load_profile()["answers"]["short_bio"] == {"value": "I build open-source AI tools.", "verified": True,
                                                     "sensitive": False, "source": "owner"}
    drain(services, ["applications"])
    app = env.db.one("SELECT * FROM applications WHERE opportunity_id=?", (opp["id"],))
    assert app["state"] == "REVIEW" and "short_bio (verified)" not in loads(app["missing_json"])
    assert loads(app["answers_json"])["short_bio"]["status"] == "verified"
    say("/profile set date_of_birth 2004-01-01")
    assert "•••" in say("/profile") and "2004" not in say("/profile")
    assert not any("2004" in (e["details_json"] or "") for e in env.db.all("SELECT details_json FROM audit_log"))


def test_outcome_and_follow_up(env, services):
    from services.telegram.bot import CommandHandler
    opp = add_opp(env.db, status="verified")
    oa.confirm_opportunity(env.db, opp["id"], by="test")
    drain(services, ["applications"])
    app = env.db.one("SELECT * FROM applications WHERE opportunity_id=?", (opp["id"],))
    h = CommandHandler(env.db, owner_id=111)
    say = lambda t: h.handle({"message": {"from": {"id": 111}, "chat": {"id": 111, "type": "private"}, "text": t}})  # noqa: E731
    assert "Recorded as submitted" in say(f"/submitted {oa.ref(app['id'])} confirmation #A123")
    assert env.db.scalar("SELECT COUNT(*) FROM reminders WHERE ref_id=? AND status='pending'", (opp["id"],)) == 0
    env.db.execute("UPDATE applications SET follow_up_at=0 WHERE id=?", (app["id"],))
    services.queue.enqueue(TaskSpec(bot_id="applications", kind="follow_up_sweep"))
    drain(services, ["applications"])
    assert env.db.scalar("SELECT state FROM applications WHERE id=?", (app["id"],)) == "FOLLOW_UP"
    assert env.db.scalar("SELECT COUNT(*) FROM notifications WHERE key=?", (f"followup:{app['id']}",)) == 1
    assert "Outcome must be" in say(f"/outcome {oa.ref(app['id'])} maybe")
    assert "Congratulations" in say(f"/outcome {oa.ref(app['id'])} accepted")
    a = env.db.one("SELECT * FROM applications WHERE id=?", (app["id"],))
    assert a["state"] == "CLOSED" and a["outcome"] == "accepted"


# ---- reports -----------------------------------------------------------------------
def test_weekly_summary_funnel_and_suggestions(env, services):
    from integrations.obsidian.vault import Vault
    services.extra["vault"] = Vault(env.vault)
    for i in range(3):
        o = add_opp(env.db, title=f"Paid bootcamp {i}", days=5 + i)
        oa.skip_opportunity(env.db, o["id"], by="test", reason="paid program")
    services.queue.enqueue(TaskSpec(bot_id="orchestrator", kind="weekly_summary"))
    drain(services, ["orchestrator"])
    text = services.extra["telegram"].sent[-1]
    assert "Funnel: 3 found" in text and "paid program (3)" in text and "exclude_keywords" in text
    assert list((env.vault / "08 Reviews").glob("Workforce *-W*.md"))


def test_daily_summary_shows_health_only_when_broken(env, services):
    services.queue.enqueue(TaskSpec(bot_id="orchestrator", kind="daily_summary", idempotency_key="d1"))
    drain(services, ["orchestrator"])
    assert "Needs fixing" not in services.extra["telegram"].sent[-1]
    env.db.execute("UPDATE integrations SET status='revoked', detail='token expired' WHERE name='telegram'")
    env.db.execute("INSERT OR REPLACE INTO integrations(name, status, detail, updated_at) VALUES ('gmail','revoked','token expired',0)")
    env.db.execute("DELETE FROM notifications")
    env.db.execute("DELETE FROM external_actions")
    services.queue.enqueue(TaskSpec(bot_id="orchestrator", kind="daily_summary", idempotency_key="d2"))
    drain(services, ["orchestrator"])
    assert "gmail is revoked" in services.extra["telegram"].sent[-1]


# ---- housekeeping ---------------------------------------------------------------
def test_housekeeping_expires_approvals_and_reports_workflows(env, services, monkeypatch):
    from workforce.scheduler import Scheduler
    tid, _ = services.queue.enqueue(TaskSpec(bot_id="echo_a", kind="send_email", payload={"key": "x1", "to": "x@corp.com"}))
    BotRuntime("echo_a", services, "t").step()
    env.db.execute("UPDATE approvals SET created_at=0")
    hk = Scheduler(env.db).housekeeping()
    assert hk["approvals_expired"] == 1 and services.queue.get(tid).status == "cancelled"
    assert env.db.scalar("SELECT COUNT(*) FROM notifications WHERE key LIKE 'apprexp:%'") == 1

    otid, _ = services.queue.enqueue(TaskSpec(bot_id="orchestrator", kind="objective", payload={"text": "find hackathons"},
                                              requested_by="telegram"))
    drain(services, ["orchestrator", "opportunity_scout"])
    assert Scheduler(env.db).housekeeping()["workflows_finished"] == 1
    n = env.db.one("SELECT * FROM notifications WHERE key LIKE 'wfdone:%'")
    assert n and n["urgent"] and "Search finished" in n["text"]


def test_stuck_schedule_alerts_once(env, services):
    from workforce.scheduler import Scheduler
    s = Scheduler(env.db)
    env.db.execute("UPDATE schedules SET next_run=0 WHERE id='personal_ops:reminder_sweep'")
    s.tick()
    env.db.execute("UPDATE tasks SET status='waiting_approval' WHERE kind='send_due_reminders'")
    for _ in range(4):
        env.db.execute("UPDATE schedules SET next_run=0 WHERE id='personal_ops:reminder_sweep'")
        s.tick()
    assert env.db.scalar("SELECT COUNT(*) FROM notifications WHERE key LIKE 'stuck:personal_ops:reminder_sweep:%'") == 1


def test_retention_prune_keeps_audit_trail(env, services):
    from workforce.scheduler import prune
    old = time.time() - 400 * 86400
    env.db.execute("INSERT INTO exec_log(ts, level, message) VALUES (?, 'info', 'old')", (old,))
    env.db.execute("INSERT INTO audit_log(ts, actor, event) VALUES (?, 'x', 'old')", (old,))
    tid, _ = services.queue.enqueue(TaskSpec(bot_id="echo_a", kind="echo"))
    env.db.execute("UPDATE tasks SET status='succeeded', finished_at=? WHERE id=?", (old, tid))
    out = prune(env.db)
    assert out["exec_log"] >= 1 and out["tasks"] == 1
    assert env.db.scalar("SELECT COUNT(*) FROM audit_log WHERE event='old'") == 1


def test_retried_objective_reuses_its_workflow(env, services):
    tid, _ = services.queue.enqueue(TaskSpec(bot_id="orchestrator", kind="objective", payload={"text": "find internships"}))
    rt = BotRuntime("orchestrator", services, "t")
    rt.step()
    env.db.execute("UPDATE tasks SET status='queued', run_after=0 WHERE id=?", (tid,))   # as if it crashed and was retried
    rt.step()
    assert env.db.scalar("SELECT COUNT(*) FROM workflows") == 1
    assert env.db.scalar("SELECT COUNT(*) FROM tasks WHERE workflow_id IS NOT NULL") == 1


def test_restarted_worker_hands_back_its_tasks(env, services):
    tid, _ = services.queue.enqueue(TaskSpec(bot_id="echo_a", kind="echo"))
    t = services.queue.claim("echo_a", "host:echo_a", 1)[0]
    assert services.queue.release_worker("host:echo_a", "worker restarted") == 1
    row = env.db.one("SELECT * FROM tasks WHERE id=?", (t.id,))
    assert row["status"] == "deferred" and row["attempts"] == 0 and row["run_after"] <= time.time()


def test_duplicates_are_caught_across_types(env, services):
    from workforce.agents.opportunity_scout import OpportunityScout
    from workforce.agents.base import AgentContext
    add_opp(env.db, title="Global AI Builders Challenge 2026", typ="hackathon")
    ctx = AgentContext(services.registry.get("opportunity_scout"), env.db, services.queue, services.bus, services.gateway,
                       services.router, "t", services=services.extra)
    assert OpportunityScout()._is_duplicate(ctx, {"title": "Global AI Builders Challenge 2026", "type": "competition"}, "fp-new")


def test_quality_surfaces_candidate_official_link(env, services):
    opp = add_opp(env.db, status="unverified")
    env.db.execute("INSERT OR IGNORE INTO sources(id, name, kind, url, access_method, interval_s) "
                   "VALUES ('fixture','f','fixture','x','local fixture for tests',3600)")
    from workforce.agents.quality import QualityAgent
    from workforce.agents.base import AgentContext
    ctx = AgentContext(services.registry.get("quality"), env.db, services.queue, services.bus, services.gateway,
                       services.router, "t", services=services.extra)
    r = QualityAgent().verify_opportunity(ctx, {"opportunity_id": opp["id"]})
    assert r["status"] == "needs_review"
    assert "possible official page (unconfirmed): https://fellowship.example.org/apply" in r["notes"]


def test_timed_out_task_still_counts_against_concurrency(env, services):
    import asyncio
    services.queue.enqueue(TaskSpec(bot_id="echo_b", kind="sleep", payload={"s": 1.2}, timeout_s=0.3))
    rt = BotRuntime("echo_b", services, "t")
    seen = {}

    async def go():
        stop, wake = asyncio.Event(), asyncio.Event()
        run = asyncio.create_task(rt.run_forever(wake, stop))
        await asyncio.sleep(0.8)
        seen["zombies"] = set(rt._zombies)
        seen["status"] = env.db.one("SELECT status, error FROM tasks WHERE kind='sleep'")
        await asyncio.sleep(1.0)
        seen["after"] = set(rt._zombies)
        stop.set()
        wake.set()
        await run

    asyncio.run(go())
    assert len(seen["zombies"]) == 1 and "timed out" in seen["status"]["error"]
    assert seen["after"] == set()          # the thread finished and released its slot


def test_offline_alert_only_for_a_bot_that_died_after_startup(env, services):
    from workforce.scheduler import Scheduler
    s = Scheduler(env.db)
    s.housekeeping()                                             # fresh stack: nothing has heartbeated yet
    assert env.db.scalar("SELECT COUNT(*) FROM notifications WHERE key LIKE 'offline:%'") == 0
    s.started -= 3600
    env.db.execute("UPDATE bots SET state='idle', last_heartbeat=? WHERE id='quality'", (time.time() - 900,))
    s.housekeeping()
    s.housekeeping()
    assert env.db.scalar("SELECT COUNT(*) FROM notifications WHERE key LIKE 'offline:quality:%'") == 1
