"""Bot runtime: one independent execution loop per bot.

Each BotRuntime claims only its own inbox and task queue, runs up to
`max_concurrency` tasks at once, heartbeats, and translates outcomes into queue
transitions. Runtimes are woken by notifications (in-process event or Redis
pub/sub when REDIS_URL is set) and otherwise check the DB on a short interval;
no LLM is ever polled to ask whether there is work.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .agents.base import AGENT_REGISTRY, Agent, AgentContext, Deferred, PermanentError, WaitingApproval
from .contracts import BotConfig, MessageType
from .core.db import Database, get_db
from .core.queue import Task, TaskQueue
from .core.settings import get_settings
from .core.state import SystemState, exec_log, set_integration_status
from .core.util import now
from .messaging.bus import MessageBus, MessageRejected
from .model_router.router import BudgetExceeded, ModelRouter, ModelUnavailable
from .policies.engine import ActionGateway, IntegrationError, IntegrationRevoked
from .registry import BotRegistry


HEARTBEAT_S = 30


class Services:
    """Shared, stateless handles built once per process."""

    def __init__(self, db: Database | None = None, router: ModelRouter | None = None, extra: dict | None = None):
        self.db = db or get_db()
        self.queue = TaskQueue(self.db)
        self.bus = MessageBus(self.db)
        self.gateway = ActionGateway(self.db)
        self.router = router or ModelRouter(self.db)
        self.registry = BotRegistry(self.db)
        self.state = SystemState(self.db)
        self.extra = extra or {}


def load_agents() -> None:
    from . import agents  # noqa: F401  (imports register every agent class)


class BotRuntime:
    def __init__(self, bot_id: str, services: Services, worker_id: str | None = None):
        load_agents()
        self.bot_id = bot_id
        self.s = services
        self.worker_id = f"{worker_id or get_settings().worker_id}:{bot_id}"
        self._running: set[str] = set()
        self._zombies: set[str] = set()   # timed out, thread still running: still counts against concurrency
        self._lock = threading.Lock()
        self._last_hb = 0.0

    def config(self) -> BotConfig:
        cfg = self.s.registry.get(self.bot_id)
        if cfg is None:
            raise RuntimeError(f"bot {self.bot_id} not registered")
        return cfg

    def agent(self, cfg: BotConfig) -> Agent:
        cls = AGENT_REGISTRY.get(cfg.agent)
        if cls is None:
            raise RuntimeError(f"no agent class '{cfg.agent}' for bot {cfg.id}")
        return cls()

    def ctx(self, cfg: BotConfig, task: Task | None = None) -> AgentContext:
        return AgentContext(cfg, self.s.db, self.s.queue, self.s.bus, self.s.gateway, self.s.router,
                            self.worker_id, task, services=self.s.extra)

    # ---- synchronous single step (used by tests and the async loop) -------
    def process_messages(self, limit: int = 10) -> int:
        cfg = self.config()
        msgs = self.s.bus.claim(self.bot_id, self.worker_id, limit)
        agent = self.agent(cfg)
        for m in msgs:
            try:
                agent.handle_message(self.ctx(cfg), m)
                self.s.bus.ack(m["id"])
            except MessageRejected as e:
                self.s.bus.nack(m["id"], str(e), retryable=False)
            except Exception as e:  # noqa: BLE001
                self.s.bus.nack(m["id"], f"{type(e).__name__}: {e}")
        return len(msgs)

    def claim_tasks(self) -> list[Task]:
        cfg = self.config()
        with self._lock:
            free = cfg.max_concurrency - len(self._running) - len(self._zombies)
            if free <= 0:
                return []
            tasks = self.s.queue.claim(self.bot_id, self.worker_id, free)
            for t in tasks:
                self._running.add(t.id)
        return tasks

    def run_task(self, task: Task) -> str:
        """Execute one claimed task to a queue transition. Returns final status."""
        cfg = self.config()
        ctx = self.ctx(cfg, task)
        self.s.registry.heartbeat(self.bot_id, "busy", task.id)
        try:
            result = self.agent(cfg).handle_task(ctx, task)
            ok = self.s.queue.complete(task.id, result, self.worker_id)
            status = "succeeded" if ok else "lease_lost"
            if ok:
                self._reply_result(ctx, task, "succeeded", result)
            return status
        except WaitingApproval as e:
            self.s.queue.wait_for_approval(task.id, e.approval_id, self.worker_id)
            try:
                from .owner_actions import approval_message
                text, buttons = approval_message(self.s.db, e.approval_id, cfg.name)
                ctx.notify_owner(text, key=f"approval:{e.approval_id}", urgent=True, buttons=buttons)
            except Exception as ne:  # noqa: BLE001  (the approval is stored; the brief and dashboard still show it)
                ctx.log(f"approval notification failed: {ne}", level="warning")
            return "waiting_approval"
        except (Deferred, BudgetExceeded) as e:
            delay = getattr(e, "delay_s", 3600)
            self.s.queue.defer(task.id, str(e), delay, self.worker_id)
            ctx.log(f"deferred: {e}", level="warning")
            return "deferred"
        except IntegrationRevoked as e:
            integ = str(getattr(e, "integration", "unknown"))
            set_integration_status(self.s.db, integ, "revoked", str(e))
            st = self.s.queue.fail(task.id, f"credential error: {e}", retryable=False, worker_id=self.worker_id)
            # one alert per integration per day, not one per failed task
            self._alert(ctx, task, f"{integ} needs reconnecting\n\n{integ} rejected its credentials ({e}). Tasks that need it "
                        "will fail until you reconnect it. Check the dashboard Integrations page.",
                        key=f"alert:cred:{integ}:{int(now() // 86400)}")
            self._reply_result(ctx, task, "failed", {"error": str(e)})
            return st
        except PermanentError as e:
            st = self.s.queue.fail(task.id, str(e), retryable=False, worker_id=self.worker_id)
            self._reply_result(ctx, task, "failed", {"error": str(e)})
            return st
        except (IntegrationError, ModelUnavailable, MessageRejected, Exception) as e:  # noqa: BLE001
            st = self.s.queue.fail(task.id, f"{type(e).__name__}: {e}", retryable=True, worker_id=self.worker_id)
            if st == "dead":
                self._alert(ctx, task, f"{task.kind.replace('_', ' ')} failed\n\nI tried {task.attempts} times and stopped. "
                            f"Last error: {str(e)[:200]}\nSee the dashboard Tasks page for details.",
                            key=f"alert:dead:{self.bot_id}:{task.kind}:{int(now() // 3600)}")
                self._reply_result(ctx, task, "failed", {"error": str(e)})
            return st
        finally:
            with self._lock:
                self._running.discard(task.id)
            self.s.registry.heartbeat(self.bot_id, "idle" if not self._running else "busy",
                                      next(iter(self._running), None))

    def _reply_result(self, ctx: AgentContext, task: Task, status: str, result: Any) -> None:
        rt = task.payload.get("_reply_to")
        if not rt:
            return
        original = ctx.db.one("SELECT * FROM messages WHERE id=?", (rt["message_id"],))
        if not original:
            return
        try:
            ctx.bus.reply(original, ctx.bot.id, MessageType.TASK_RESULT,
                          {"status": status, "task_id": task.id, "kind": task.kind, "result": result})
        except MessageRejected as e:
            ctx.log(f"could not deliver result: {e}", level="error")

    def _alert(self, ctx: AgentContext, task: Task, text: str, key: str) -> None:
        exec_log(self.s.db, "error", text, bot_id=self.bot_id, task_id=task.id, correlation_id=task.correlation_id)
        try:
            ctx.notify_owner(f"⚠️ {ctx.bot.name}: {text}", key=key, urgent=True)
        except Exception:  # noqa: BLE001
            pass

    def step(self) -> int:
        """Process inbox then run claimed tasks sequentially. Returns work done."""
        n = self.process_messages()
        for t in self.claim_tasks():
            self.run_task(t)
            n += 1
        self.s.registry.heartbeat(self.bot_id)
        return n

    def _idle_heartbeat(self) -> None:
        if time.monotonic() - self._last_hb >= HEARTBEAT_S:
            self.s.registry.heartbeat(self.bot_id)
            self._last_hb = time.monotonic()

    # ---- async loop ------------------------------------------------------
    async def run_forever(self, wake: asyncio.Event, stop: asyncio.Event) -> None:
        interval = get_settings().poll_interval_s
        cfg = self.config()
        # own pools: long tasks can never starve this bot's claims, inbox and heartbeats
        self._tasks_pool = ThreadPoolExecutor(max_workers=cfg.max_concurrency * 2 + 1, thread_name_prefix=f"{self.bot_id}-task")
        self._ctl_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"{self.bot_id}-ctl")
        loop = asyncio.get_running_loop()
        inflight: set[asyncio.Task] = set()
        released = self.s.queue.release_worker(self.worker_id, "worker restarted")
        exec_log(self.s.db, "info", f"runtime started ({self.worker_id}); released {released} task(s) from a previous run",
                 bot_id=self.bot_id)
        while not stop.is_set():
            try:
                await loop.run_in_executor(self._ctl_pool, self.process_messages)
                for t in await loop.run_in_executor(self._ctl_pool, self.claim_tasks):
                    at = asyncio.create_task(self._run_with_timeout(t))
                    inflight.add(at)
                    at.add_done_callback(inflight.discard)
                await loop.run_in_executor(self._ctl_pool, self._idle_heartbeat)
            except Exception as e:  # noqa: BLE001
                exec_log(self.s.db, "error", f"runtime loop error: {e}", bot_id=self.bot_id)
            if len(self._zombies) > max(2, cfg.max_concurrency):
                exec_log(self.s.db, "error", f"{len(self._zombies)} timed-out tasks still running; restarting the process",
                         bot_id=self.bot_id)
                os._exit(75)   # the supervisor / container restart policy brings it back clean
            try:
                await asyncio.wait_for(wake.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            wake.clear()
        # graceful shutdown: let in-flight tasks finish, then hand back anything left
        if inflight:
            await asyncio.wait(inflight, timeout=60)
        self.s.queue.release_worker(self.worker_id, "worker shut down")

    async def _run_with_timeout(self, task: Task) -> None:
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(self._tasks_pool, self.run_task, task)
        done, _ = await asyncio.wait({fut}, timeout=task.timeout_s)
        if not done:
            # the thread cannot be killed. Failing the task drops its lease, so a late
            # completion is discarded; the gateway's in-flight reservation stops a retry
            # from repeating an external action the old thread is still performing.
            self.s.queue.fail(task.id, f"timed out after {task.timeout_s}s", retryable=True, worker_id=self.worker_id)
            with self._lock:
                self._running.discard(task.id)
                self._zombies.add(task.id)

            def _gone(_f, tid=task.id):
                with self._lock:
                    self._zombies.discard(tid)
            fut.add_done_callback(_gone)
