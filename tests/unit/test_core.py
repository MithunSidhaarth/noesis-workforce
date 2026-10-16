"""Core runtime: queue, messaging, delegation, dependencies, concurrency,
recovery, retries, dead letters, disabled bots, scheduling."""
import asyncio
import threading
import time

import pytest

from tests.helpers import CONCURRENCY
from workforce.contracts import Message, MessageType, TaskSpec
from workforce.core.queue import BotDisabled, DelegationLimit, TaskQueue
from workforce.messaging.bus import MessageBus, MessageRejected
from workforce.registry import BotRegistry
from workforce.runtime import BotRuntime, Services
from workforce.scheduler import Scheduler, next_run


def drain(services, bots=("echo_a", "echo_b"), rounds=20):
    rts = {b: BotRuntime(b, services, worker_id="t") for b in bots}
    for _ in range(rounds):
        if sum(rt.step() for rt in rts.values()) == 0:
            break
    return rts


def test_registry_loads_all_ten_specialists(env):
    ids = {b["id"] for b in BotRegistry(env.db).all()}
    for b in ["orchestrator", "opportunity_scout", "applications", "content_strategist", "content_studio",
              "communications", "outreach", "knowledge", "personal_ops", "quality"]:
        assert b in ids
    cfg = BotRegistry(env.db).get("opportunity_scout")
    assert cfg.tools and "email.send" not in cfg.tools  # least privilege


def test_each_bot_runs_independently(services):
    q = services.queue
    a, _ = q.enqueue(TaskSpec(bot_id="echo_a", kind="echo", payload={"text": "a"}))
    b, _ = q.enqueue(TaskSpec(bot_id="echo_b", kind="echo", payload={"text": "b"}))
    BotRuntime("echo_a", services, "t").step()
    assert q.get(a).status == "succeeded"
    assert q.get(b).status == "queued"  # echo_b's work is untouched by echo_a's runtime
    BotRuntime("echo_b", services, "t").step()
    assert q.get(b).result["echo"] == "b"


def test_structured_message_exchange_and_delegation(services):
    q = services.queue
    tid, _ = q.enqueue(TaskSpec(bot_id="echo_a", kind="delegate", payload={"to": "echo_b", "text": "ping"}))
    drain(services)
    msgs = services.bus.history(correlation_id=q.get(tid).correlation_id)
    types = [m["type"] for m in reversed(msgs)]
    assert types[:3] == ["TASK_REQUEST", "TASK_ACCEPTED", "TASK_RESULT"]
    req = next(m for m in msgs if m["type"] == "TASK_REQUEST")
    assert req["sender"] == "echo_a" and req["recipient"] == "echo_b"
    child = services.db.one("SELECT * FROM tasks WHERE bot_id='echo_b'")
    assert child["delegation_depth"] == 1 and child["correlation_id"] == q.get(tid).correlation_id
    mem = services.db.one("SELECT value_json FROM bot_memory WHERE bot_id='echo_a' AND key='last_result'")
    assert '"echo": "ping"' in mem["value_json"]


def test_message_schema_and_permissions(services):
    bus = services.bus
    with pytest.raises(Exception):
        Message(sender="echo_a", type="NOT_A_TYPE", recipient="echo_b")
    with pytest.raises(MessageRejected):  # echo_b may not message quality
        bus.send(Message(sender="echo_b", recipient="quality", type=MessageType.INFORMATION_REQUEST))
    with pytest.raises(MessageRejected):  # bots cannot grant approvals
        bus.send(Message(sender="echo_a", recipient="echo_b", type=MessageType.APPROVAL_GRANTED))


def test_message_dedupe(services):
    bus = services.bus
    m = dict(sender="echo_a", recipient="echo_b", type=MessageType.INFORMATION_REQUEST, dedupe_key="k1")
    assert len(bus.send(Message(**m))) == 1
    assert bus.send(Message(**m)) == []


def test_loop_detection_stops_ping_pong(services):
    services.bus.send({"sender": "user", "recipient": "echo_a", "type": "ALERT", "payload": {"x": 1}, "correlation_id": "c1"})
    drain(services, rounds=40)
    n = services.db.scalar("SELECT COUNT(*) FROM messages WHERE correlation_id='c1'")
    assert n <= 14  # bounded, not infinite
    assert services.db.scalar("SELECT COUNT(*) FROM audit_log WHERE event='message.loop_blocked'") >= 1 or n <= 13


def test_dependencies_respected(services):
    q = services.queue
    a, _ = q.enqueue(TaskSpec(bot_id="echo_a", kind="echo", payload={"text": "first"}))
    b, _ = q.enqueue(TaskSpec(bot_id="echo_b", kind="echo", payload={"text": "second"}, depends_on=[a]))
    assert q.get(b).status == "pending"
    BotRuntime("echo_b", services, "t").step()
    assert q.get(b).status == "pending"  # still blocked
    BotRuntime("echo_a", services, "t").step()
    assert q.get(b).status == "queued"
    BotRuntime("echo_b", services, "t").step()
    assert a in q.get(b).result["deps"]


def test_dead_dependency_cancels_dependents(services):
    q = services.queue
    a, _ = q.enqueue(TaskSpec(bot_id="echo_b", kind="always_fail"))
    b, _ = q.enqueue(TaskSpec(bot_id="echo_a", kind="echo", depends_on=[a]))
    drain(services, rounds=10)
    assert q.get(a).status == "dead"
    assert q.get(b).status == "cancelled"


def test_concurrency_within_limits(services):
    q = services.queue
    CONCURRENCY.update(max=0, now=0)
    for i in range(6):
        q.enqueue(TaskSpec(bot_id="echo_a", kind="sleep", payload={"s": 0.3}))

    async def go():
        rt = BotRuntime("echo_a", services, "t")
        stop, wake = asyncio.Event(), asyncio.Event()
        runner = asyncio.create_task(rt.run_forever(wake, stop))
        t0 = time.time()
        while services.db.scalar("SELECT COUNT(*) FROM tasks WHERE status='succeeded'") < 6 and time.time() - t0 < 10:
            await asyncio.sleep(0.05)
            wake.set()
        stop.set()
        await runner
        return time.time() - t0

    elapsed = asyncio.run(go())
    assert services.db.scalar("SELECT COUNT(*) FROM tasks WHERE status='succeeded'") == 6
    assert CONCURRENCY["max"] == 3       # max_concurrency for echo_a
    assert elapsed < 6 * 0.3            # ran in parallel, not serially


def test_restart_does_not_lose_jobs(env):
    from workforce.core.db import Database
    q = TaskQueue(env.db)
    tid, _ = q.enqueue(TaskSpec(bot_id="echo_a", kind="echo", payload={"text": "persist"}))
    env.db.close()
    db2 = Database(env.db.path)  # a brand-new process view of the same file
    assert TaskQueue(db2).get(tid).status == "queued"


def test_worker_crash_recovery(services):
    q = services.queue
    tid, _ = q.enqueue(TaskSpec(bot_id="echo_a", kind="echo", payload={"text": "x"}, timeout_s=1))
    claimed = q.claim("echo_a", "crashed-worker")
    assert claimed and claimed[0].id == tid
    # worker dies; lease expires
    services.db.execute("UPDATE tasks SET lease_expires=? WHERE id=?", (time.time() - 1, tid))
    assert Scheduler(services.db).housekeeping()["tasks_reclaimed"] == 1
    assert q.complete(tid, {"late": True}, "crashed-worker") is False  # zombie result discarded
    drain(services, bots=("echo_a",))
    assert q.get(tid).status == "succeeded"


def test_bounded_retry_then_dead_letter_visible(services):
    q = services.queue
    flaky, _ = q.enqueue(TaskSpec(bot_id="echo_a", kind="flaky"))
    dead, _ = q.enqueue(TaskSpec(bot_id="echo_a", kind="always_fail"))
    drain(services, bots=("echo_a",), rounds=10)
    assert q.get(flaky).status == "succeeded" and q.get(flaky).attempts == 2
    d = q.get(dead)
    assert d.status == "dead" and d.attempts == 3 and "service down" in d.error
    assert services.db.scalar("SELECT COUNT(*) FROM exec_log WHERE level='error' AND task_id=?", (dead,)) >= 1


def test_revoked_credential_is_clear_and_not_retried(services):
    q = services.queue
    tid, _ = q.enqueue(TaskSpec(bot_id="echo_a", kind="revoked"))
    drain(services, bots=("echo_a",))
    t = q.get(tid)
    assert t.status == "dead" and t.attempts == 1 and "credential" in t.error
    assert services.db.one("SELECT status FROM integrations WHERE name='gmail'")["status"] == "revoked"


def test_disabled_bot_gets_no_new_work(services):
    services.registry.set_enabled("echo_b", False, by="user")
    with pytest.raises(BotDisabled):
        services.queue.enqueue(TaskSpec(bot_id="echo_b", kind="echo"))
    with pytest.raises(MessageRejected):
        services.bus.send(Message(sender="echo_a", recipient="echo_b", type=MessageType.TASK_REQUEST, payload={"kind": "echo"}))


def test_delegation_depth_limit(services):
    with pytest.raises(DelegationLimit):
        services.queue.enqueue(TaskSpec(bot_id="echo_a", kind="echo", delegation_depth=4))


def test_enqueue_idempotent(services):
    a = services.queue.enqueue(TaskSpec(bot_id="echo_a", kind="echo", idempotency_key="same"))
    b = services.queue.enqueue(TaskSpec(bot_id="echo_a", kind="echo", idempotency_key="same"))
    assert a[0] == b[0] and b[1] is False


def test_scheduler_no_duplicate_or_overlapping_runs(services):
    s = Scheduler(services.db)
    t0 = time.time()
    s.tick(t0)
    first = services.db.scalar("SELECT COUNT(*) FROM tasks WHERE kind='sweep_sources'")
    assert first == 1
    s.tick(t0)  # same instant (e.g. second scheduler process) -> nothing new
    assert services.db.scalar("SELECT COUNT(*) FROM tasks WHERE kind='sweep_sources'") == 1
    s.tick(t0 + 16 * 60)  # next slot but previous run unfinished -> skipped
    assert services.db.scalar("SELECT COUNT(*) FROM tasks WHERE kind='sweep_sources'") == 1
    services.db.execute("UPDATE tasks SET status='succeeded' WHERE kind='sweep_sources'")
    s.tick(t0 + 32 * 60)
    assert services.db.scalar("SELECT COUNT(*) FROM tasks WHERE kind='sweep_sources'") == 2


def test_daily_schedule_next_run():
    s = {"every_s": None, "at_time": "21:00", "weekdays": "sun", "tz": "Asia/Kolkata", "next_run": None}
    from datetime import datetime
    from zoneinfo import ZoneInfo
    n = next_run(s, datetime(2026, 10, 16, 12, tzinfo=ZoneInfo("Asia/Kolkata")).timestamp())
    d = datetime.fromtimestamp(n, ZoneInfo("Asia/Kolkata"))
    assert d.weekday() == 6 and d.hour == 21
