"""Durable task queue on SQLite.

Guarantees:
  * enqueue is idempotent on idempotency_key
  * a task is claimed by exactly one worker (BEGIN IMMEDIATE + lease)
  * expired leases (crashed worker) are reclaimed and retried
  * failures retry with exponential backoff + jitter, then dead-letter
  * tasks with dependencies stay 'pending' until every dependency succeeded;
    a dead/cancelled dependency cancels dependents
  * disabled bots receive no new work; paused bots are skipped at claim time
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..contracts import TERMINAL_TASK_STATES, TaskSpec
from .db import Database, dumps, get_db, loads
from .notify import wake
from .state import audit, exec_log
from .util import backoff, new_id, now, redact


class QueueError(Exception):
    pass


class BotDisabled(QueueError):
    pass


class DelegationLimit(QueueError):
    pass


@dataclass
class Task:
    id: str
    bot_id: str
    kind: str
    payload: dict
    status: str
    priority: int
    workflow_id: str | None
    parent_task_id: str | None
    correlation_id: str | None
    requested_by: str | None
    delegation_depth: int
    depends_on: list[str]
    attempts: int
    max_attempts: int
    timeout_s: float
    checkpoint: dict | None
    result: Any
    error: str | None
    deadline: float | None

    @classmethod
    def from_row(cls, r: dict) -> "Task":
        return cls(
            id=r["id"], bot_id=r["bot_id"], kind=r["kind"], payload=loads(r["payload_json"], {}),
            status=r["status"], priority=r["priority"], workflow_id=r["workflow_id"],
            parent_task_id=r["parent_task_id"], correlation_id=r["correlation_id"],
            requested_by=r["requested_by"], delegation_depth=r["delegation_depth"],
            depends_on=loads(r["depends_on_json"], []), attempts=r["attempts"],
            max_attempts=r["max_attempts"], timeout_s=r["timeout_s"],
            checkpoint=loads(r["checkpoint_json"]), result=loads(r["result_json"]), error=r["error"],
            deadline=r["deadline"],
        )


class TaskQueue:
    def __init__(self, db: Database | None = None, max_global_depth: int = 6):
        self.db = db or get_db()
        self.max_global_depth = max_global_depth

    # ---- enqueue -----------------------------------------------------------
    def enqueue(self, spec: TaskSpec) -> tuple[str, bool]:
        """Returns (task_id, created). created=False means idempotent duplicate."""
        bot = self.db.one("SELECT enabled, config_json FROM bots WHERE id=?", (spec.bot_id,))
        if bot is None:
            raise QueueError(f"unknown bot {spec.bot_id}")
        if not bot["enabled"]:
            raise BotDisabled(f"bot {spec.bot_id} is disabled")
        cfg = loads(bot["config_json"], {})
        limit = min(self.max_global_depth, cfg.get("max_delegation_depth", self.max_global_depth))
        if spec.delegation_depth > limit:
            raise DelegationLimit(f"delegation depth {spec.delegation_depth} exceeds {limit} for {spec.bot_id}")

        t = now()
        with self.db.tx() as c:
            if spec.idempotency_key:
                r = c.execute("SELECT id FROM tasks WHERE idempotency_key=?", (spec.idempotency_key,)).fetchone()
                if r:
                    return r[0], False
            deps = list(dict.fromkeys(spec.depends_on))
            status = "queued"
            if deps:
                rows = c.execute(
                    f"SELECT id, status FROM tasks WHERE id IN ({','.join('?' * len(deps))})", deps
                ).fetchall()
                found = {r[0]: r[1] for r in rows}
                missing = [d for d in deps if d not in found]
                if missing:
                    raise QueueError(f"unknown dependencies: {missing}")
                if any(s in ("dead", "cancelled") for s in found.values()):
                    status = "cancelled"
                elif not all(s == "succeeded" for s in found.values()):
                    status = "pending"
            tid = new_id("task")
            c.execute(
                "INSERT INTO tasks(id, bot_id, kind, payload_json, status, priority, workflow_id, parent_task_id, "
                "correlation_id, requested_by, delegation_depth, depends_on_json, idempotency_key, max_attempts, "
                "timeout_s, run_after, deadline, created_at, updated_at, error) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tid, spec.bot_id, spec.kind, dumps(spec.payload), status, spec.priority, spec.workflow_id,
                 spec.parent_task_id, spec.correlation_id or tid, spec.requested_by, spec.delegation_depth,
                 dumps(deps), spec.idempotency_key,
                 spec.max_attempts or cfg.get("retry", {}).get("max_attempts", 3),
                 spec.timeout_s or cfg.get("task_timeout_s", 300),
                 spec.run_after or t, spec.deadline, t, t,
                 "dependency failed" if status == "cancelled" else None),
            )
        if status == "queued":
            wake(spec.bot_id)
        audit(self.db, spec.requested_by, "task.enqueued", spec.correlation_id or tid,
              {"task_id": tid, "bot_id": spec.bot_id, "kind": spec.kind, "status": status})
        return tid, True

    # ---- claim -------------------------------------------------------------
    def claim(self, bot_id: str, worker_id: str, limit: int = 1) -> list[Task]:
        t = now()
        with self.db.tx() as c:
            bot = c.execute("SELECT enabled, state FROM bots WHERE id=?", (bot_id,)).fetchone()
            if not bot or not bot["enabled"] or bot["state"] == "paused":
                return []
            rows = c.execute(
                "SELECT * FROM tasks WHERE bot_id=? AND status IN ('queued','failed','deferred') AND run_after<=? "
                "ORDER BY priority ASC, run_after ASC LIMIT ?",
                (bot_id, t, limit),
            ).fetchall()
            out = []
            for r in rows:
                lease = t + float(r["timeout_s"]) + 30
                c.execute(
                    "UPDATE tasks SET status='running', lease_owner=?, lease_expires=?, attempts=attempts+1, "
                    "started_at=COALESCE(started_at, ?), updated_at=? WHERE id=?",
                    (worker_id, lease, t, t, r["id"]),
                )
                d = dict(r)
                d["status"] = "running"
                d["attempts"] = r["attempts"] + 1
                out.append(Task.from_row(d))
            return out

    def heartbeat(self, task_id: str, worker_id: str, extend_s: float) -> bool:
        cur = self.db.execute(
            "UPDATE tasks SET lease_expires=?, updated_at=? WHERE id=? AND lease_owner=? AND status='running'",
            (now() + extend_s, now(), task_id, worker_id),
        )
        return cur.rowcount == 1

    def checkpoint(self, task_id: str, data: dict) -> None:
        self.db.execute("UPDATE tasks SET checkpoint_json=?, updated_at=? WHERE id=?", (dumps(data), now(), task_id))

    # ---- completion ----------------------------------------------------------
    def complete(self, task_id: str, result: Any = None, worker_id: str | None = None) -> bool:
        t = now()
        with self.db.tx() as c:
            q = "UPDATE tasks SET status='succeeded', result_json=?, error=NULL, finished_at=?, updated_at=?, lease_owner=NULL WHERE id=? AND status='running'"
            args: tuple = (dumps(result), t, t, task_id)
            if worker_id:
                q += " AND lease_owner=?"
                args += (worker_id,)
            if c.execute(q, args).rowcount != 1:
                return False  # lease lost (e.g. reclaimed after timeout) -> result discarded
            self._release_dependents(c, task_id)
            row = c.execute("SELECT bot_id FROM tasks WHERE id=?", (task_id,)).fetchone()
            c.execute("UPDATE bots SET tasks_succeeded=tasks_succeeded+1, last_success=?, last_execution=? WHERE id=?",
                      (t, t, row["bot_id"]))
        return True

    def fail(self, task_id: str, error: str, retryable: bool = True, worker_id: str | None = None) -> str:
        """Record a failure. Returns the new status ('failed' = will retry, 'dead')."""
        error = redact(error)
        t = now()
        with self.db.tx() as c:
            r = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if r is None or r["status"] != "running" or (worker_id and r["lease_owner"] != worker_id):
                return r["status"] if r else "missing"
            cfg = loads(c.execute("SELECT config_json FROM bots WHERE id=?", (r["bot_id"],)).fetchone()[0], {})
            rp = cfg.get("retry", {})
            if retryable and r["attempts"] < r["max_attempts"]:
                delay = backoff(r["attempts"], rp.get("base_delay_s", 10), rp.get("max_delay_s", 3600))
                c.execute("UPDATE tasks SET status='failed', error=?, run_after=?, lease_owner=NULL, updated_at=? WHERE id=?",
                          (error[:2000], t + delay, t, task_id))
                status = "failed"
            else:
                c.execute("UPDATE tasks SET status='dead', error=?, finished_at=?, lease_owner=NULL, updated_at=? WHERE id=?",
                          (error[:2000], t, t, task_id))
                self._cancel_dependents(c, task_id, "dependency dead-lettered")
                status = "dead"
            c.execute("UPDATE bots SET tasks_failed=tasks_failed+1, last_execution=? WHERE id=?", (t, r["bot_id"]))
        exec_log(self.db, "warning" if status == "failed" else "error", f"task {status}: {error[:300]}",
                 bot_id=r["bot_id"], task_id=task_id, correlation_id=r["correlation_id"])
        return status

    def defer(self, task_id: str, reason: str, delay_s: float) -> None:
        """Re-queue without consuming an attempt (budget exhausted, global pause)."""
        t = now()
        self.db.execute(
            "UPDATE tasks SET status='deferred', attempts=MAX(0, attempts-1), error=?, run_after=?, lease_owner=NULL, updated_at=? "
            "WHERE id=? AND status='running'", (reason, t + delay_s, t, task_id))

    def wait_for_approval(self, task_id: str, approval_id: str) -> None:
        self.db.execute(
            "UPDATE tasks SET status='waiting_approval', error=?, lease_owner=NULL, updated_at=? WHERE id=? AND status='running'",
            (f"waiting for approval {approval_id}", now(), task_id))

    def resume_after_approval(self, task_id: str) -> None:
        self.db.execute(
            "UPDATE tasks SET status='queued', attempts=MAX(0, attempts-1), run_after=?, updated_at=? WHERE id=? AND status='waiting_approval'",
            (now(), now(), task_id))

    def cancel(self, task_id: str, by: str, reason: str = "cancelled") -> bool:
        t = now()
        with self.db.tx() as c:
            r = c.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
            if r is None or r["status"] in TERMINAL_TASK_STATES:
                return False
            c.execute("UPDATE tasks SET status='cancelled', error=?, finished_at=?, lease_owner=NULL, updated_at=? WHERE id=?",
                      (reason, t, t, task_id))
            self._cancel_dependents(c, task_id, f"dependency cancelled: {reason}")
        audit(self.db, by, "task.cancelled", details={"task_id": task_id, "reason": reason})
        return True

    def cancel_workflow(self, workflow_id: str, by: str) -> int:
        ids = [r["id"] for r in self.db.all("SELECT id FROM tasks WHERE workflow_id=?", (workflow_id,))]
        n = sum(1 for i in ids if self.cancel(i, by, "workflow cancelled"))
        self.db.execute("UPDATE workflows SET status='cancelled', updated_at=? WHERE id=?", (now(), workflow_id))
        return n

    def is_cancelled(self, task_id: str) -> bool:
        return self.db.scalar("SELECT status FROM tasks WHERE id=?", (task_id,)) == "cancelled"

    # ---- recovery ------------------------------------------------------------
    def reclaim_expired(self) -> int:
        """Leases that expired belong to crashed/hung workers: retry or dead-letter them."""
        t = now()
        rows = self.db.all("SELECT id, lease_owner FROM tasks WHERE status='running' AND lease_expires < ?", (t,))
        for r in rows:
            self.fail(r["id"], f"lease expired (worker {r['lease_owner']} crashed or timed out)", retryable=True)
        return len(rows)

    # ---- dependency helpers ------------------------------------------------
    def _release_dependents(self, c, task_id: str) -> None:
        rows = c.execute("SELECT id, depends_on_json FROM tasks WHERE status='pending' AND depends_on_json LIKE ?",
                         (f'%"{task_id}"%',)).fetchall()
        for r in rows:
            deps = loads(r["depends_on_json"], [])
            sts = c.execute(f"SELECT status FROM tasks WHERE id IN ({','.join('?' * len(deps))})", deps).fetchall()
            if all(s[0] == "succeeded" for s in sts):
                c.execute("UPDATE tasks SET status='queued', run_after=?, updated_at=? WHERE id=?", (now(), now(), r["id"]))

    def _cancel_dependents(self, c, task_id: str, reason: str) -> None:
        rows = c.execute("SELECT id FROM tasks WHERE status='pending' AND depends_on_json LIKE ?", (f'%"{task_id}"%',)).fetchall()
        for r in rows:
            c.execute("UPDATE tasks SET status='cancelled', error=?, finished_at=?, updated_at=? WHERE id=?",
                      (reason, now(), now(), r["id"]))
            self._cancel_dependents(c, r["id"], reason)

    def dependency_results(self, task: Task) -> dict[str, Any]:
        if not task.depends_on:
            return {}
        rows = self.db.all(f"SELECT id, kind, result_json FROM tasks WHERE id IN ({','.join('?' * len(task.depends_on))})",
                           tuple(task.depends_on))
        return {r["id"]: {"kind": r["kind"], "result": loads(r["result_json"])} for r in rows}

    def get(self, task_id: str) -> Task | None:
        r = self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        return Task.from_row(r) if r else None

    def depth(self) -> dict[str, int]:
        return {r["status"]: r["n"] for r in self.db.all("SELECT status, COUNT(*) n FROM tasks GROUP BY status")}
