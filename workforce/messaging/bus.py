"""Persistent inter-agent message bus.

Messages are rows in `messages`. A recipient's runtime claims its inbox with a
lease (like tasks), processes each message, and acks it. Topic messages are
fanned out to subscribers as one row per recipient.

Controls enforced here (deterministically, never by an LLM):
  * schema validation (contracts.Message)
  * sender permission: sender's `can_message` / `publish_topics`
  * recipient must exist and be enabled
  * dedupe_key idempotency
  * hop-count / loop detection per correlation chain
  * expiry, bounded delivery attempts, dead-letter
"""
from __future__ import annotations

from typing import Iterable

from ..contracts import BotConfig, Message, MessageType
from ..core.db import Database, dumps, get_db, loads
from ..core.notify import wake
from ..core.state import audit, exec_log
from ..core.util import backoff, new_id, now, sha

HUMAN_SENDERS = {"user", "system", "scheduler", "telegram", "dashboard"}
MAX_HOPS = 12
MAX_SAME_EDGE_PER_CORRELATION = 6  # same sender->recipient->type within one chain


class MessageRejected(Exception):
    pass


class MessageBus:
    def __init__(self, db: Database | None = None):
        self.db = db or get_db()

    def _cfg(self, bot_id: str) -> tuple[BotConfig | None, bool]:
        r = self.db.one("SELECT config_json, enabled FROM bots WHERE id=?", (bot_id,))
        if not r:
            return None, False
        return BotConfig.model_validate(loads(r["config_json"])), bool(r["enabled"])

    def send(self, msg: Message | dict) -> list[str]:
        """Validate, authorize and persist. Returns created message ids (may be
        empty if deduplicated). Raises MessageRejected on policy violations."""
        m = msg if isinstance(msg, Message) else Message.model_validate(msg)
        if m.hop_count > MAX_HOPS:
            raise MessageRejected(f"hop limit exceeded ({m.hop_count})")

        # sender authorization
        if m.sender not in HUMAN_SENDERS:
            scfg, s_enabled = self._cfg(m.sender)
            if scfg is None:
                raise MessageRejected(f"unknown sender {m.sender}")
            if m.recipient and not ("*" in scfg.can_message or m.recipient in scfg.can_message):
                raise MessageRejected(f"{m.sender} is not permitted to message {m.recipient}")
            if m.topic and m.topic not in scfg.publish_topics:
                raise MessageRejected(f"{m.sender} is not permitted to publish to {m.topic}")
            if m.type in (MessageType.APPROVAL_GRANTED, MessageType.APPROVAL_DENIED):
                raise MessageRejected("only humans can grant or deny approvals")

        recipients: list[str]
        if m.recipient:
            recipients = [m.recipient]
        else:
            recipients = [r["bot_id"] for r in self.db.all("SELECT bot_id FROM subscriptions WHERE topic=?", (m.topic,))
                          if r["bot_id"] != m.sender]

        corr = m.correlation_id or new_id("corr")
        created = []
        for rcpt in recipients:
            rcfg, r_enabled = self._cfg(rcpt)
            if rcfg is None:
                raise MessageRejected(f"unknown recipient {rcpt}")
            if not r_enabled:
                if m.recipient:
                    raise MessageRejected(f"recipient {rcpt} is disabled")
                continue
            # loop detection: same edge repeated too often in one correlation chain
            n = self.db.scalar("SELECT COUNT(*) FROM messages WHERE correlation_id=? AND sender=? AND recipient=? AND type=?",
                               (corr, m.sender, rcpt, m.type.value))
            if n >= MAX_SAME_EDGE_PER_CORRELATION:
                audit(self.db, m.sender, "message.loop_blocked", corr, {"recipient": rcpt, "type": m.type.value})
                raise MessageRejected(f"loop detected: {m.sender}->{rcpt} {m.type.value} x{n} in {corr}")
            dkey = m.dedupe_key
            if dkey and len(recipients) > 1:
                dkey = f"{dkey}:{rcpt}"
            if dkey and self.db.scalar("SELECT 1 FROM messages WHERE dedupe_key=?", (dkey,)):
                continue
            mid = m.id if (m.id and len(recipients) == 1) else new_id("msg")
            t = now()
            try:
                self.db.execute(
                    "INSERT INTO messages(id, sender, recipient, topic, type, workflow_id, correlation_id, parent_task_id, in_reply_to, "
                    "priority, payload_json, expires_at, response_type, hop_count, dedupe_key, auth_json, status, run_after, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (mid, m.sender, rcpt, m.topic, m.type.value, m.workflow_id, corr, m.parent_task_id, m.in_reply_to,
                     m.priority, dumps(m.payload), m.expires_at, m.response_type.value if m.response_type else None,
                     m.hop_count, dkey, dumps(m.auth), "pending", t, t))
            except Exception as e:  # unique dedupe race
                if "UNIQUE" in str(e):
                    continue
                raise
            created.append(mid)
            wake(rcpt)
        return created

    def reply(self, original: dict, sender: str, type_: MessageType, payload: dict, dedupe: bool = True) -> list[str]:
        """Reply to a message row, preserving correlation/workflow and bumping hops."""
        return self.send(Message(
            sender=sender, recipient=original["sender"] if original["sender"] not in HUMAN_SENDERS else None,
            topic=None if original["sender"] not in HUMAN_SENDERS else "human.inbox",
            type=type_, workflow_id=original.get("workflow_id"), correlation_id=original.get("correlation_id"),
            parent_task_id=original.get("parent_task_id"), in_reply_to=original["id"], payload=payload,
            hop_count=int(original.get("hop_count", 0)) + 1,
            dedupe_key=sha("reply", original["id"], type_.value) if dedupe else None,
        ))

    # ---- receiving -------------------------------------------------------
    def claim(self, bot_id: str, worker_id: str, limit: int = 5, lease_s: float = 120) -> list[dict]:
        t = now()
        with self.db.tx() as c:
            # expire stale messages first
            c.execute("UPDATE messages SET status='expired' WHERE recipient=? AND status='pending' AND expires_at IS NOT NULL AND expires_at < ?",
                      (bot_id, t))
            rows = c.execute(
                "SELECT * FROM messages WHERE recipient=? AND status='pending' AND run_after<=? ORDER BY priority, created_at LIMIT ?",
                (bot_id, t, limit)).fetchall()
            out = []
            for r in rows:
                c.execute("UPDATE messages SET status='delivered', attempts=attempts+1, lease_owner=?, lease_expires=?, delivered_at=? WHERE id=?",
                          (worker_id, t + lease_s, t, r["id"]))
                d = dict(r)
                d["payload"] = loads(d.pop("payload_json"), {})
                d["auth"] = loads(d.pop("auth_json"), {})
                d["attempts"] += 1
                out.append(d)
            return out

    def ack(self, message_id: str) -> None:
        self.db.execute("UPDATE messages SET status='processed', processed_at=?, lease_owner=NULL, error=NULL WHERE id=?",
                        (now(), message_id))

    def nack(self, message_id: str, error: str, retryable: bool = True) -> str:
        r = self.db.one("SELECT attempts, max_attempts, recipient, correlation_id FROM messages WHERE id=?", (message_id,))
        if r is None:
            return "missing"
        if retryable and r["attempts"] < r["max_attempts"]:
            self.db.execute("UPDATE messages SET status='pending', error=?, run_after=?, lease_owner=NULL WHERE id=?",
                            (error[:1000], now() + backoff(r["attempts"], 5, 600), message_id))
            return "pending"
        self.db.execute("UPDATE messages SET status=?, error=?, lease_owner=NULL WHERE id=?",
                        ("dead" if retryable else "rejected", error[:1000], message_id))
        exec_log(self.db, "error", f"message dead-lettered: {error[:200]}", bot_id=r["recipient"], correlation_id=r["correlation_id"])
        return "dead"

    def reclaim_expired(self) -> int:
        t = now()
        rows = self.db.all("SELECT id FROM messages WHERE status='delivered' AND lease_expires < ?", (t,))
        for r in rows:
            self.nack(r["id"], "delivery lease expired", retryable=True)
        return len(rows)

    def cancel_correlation(self, correlation_id: str) -> int:
        return self.db.execute("UPDATE messages SET status='expired', error='cancelled' WHERE correlation_id=? AND status='pending'",
                               (correlation_id,)).rowcount

    def history(self, bot_id: str | None = None, correlation_id: str | None = None, q: str | None = None,
                limit: int = 100) -> list[dict]:
        where, args = [], []
        if bot_id:
            where.append("(sender=? OR recipient=?)")
            args += [bot_id, bot_id]
        if correlation_id:
            where.append("correlation_id=?")
            args.append(correlation_id)
        if q:
            where.append("(payload_json LIKE ? OR type LIKE ?)")
            args += [f"%{q}%", f"%{q}%"]
        sql = "SELECT * FROM messages" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        rows = self.db.all(sql, tuple(args))
        for r in rows:
            r["payload"] = loads(r.pop("payload_json"), {})
            r.pop("auth_json", None)
        return rows


def topics_for(cfgs: Iterable[BotConfig]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for c in cfgs:
        for t in c.subscribe_topics:
            out.setdefault(t, []).append(c.id)
    return out
