"""Custom agents' Telegram bots: one long-polling thread per enabled agent token.

Each agent you create in the dashboard is its own BotFather bot. This service polls
every enabled one (long polling, no public webhook), accepts only messages from
TELEGRAM_OWNER_ID in a private chat, and turns them into `custom_agents` tasks for the
worker. It reloads the agent list every 30 s, so creating, disabling, editing or
deleting an agent takes effect without a restart. Offsets are stored per agent, so a
restart neither loses nor repeats messages.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from workforce.contracts import TaskSpec
from workforce.core.db import Database, get_db
from workforce.core.queue import BotDisabled, TaskQueue
from workforce.core.settings import get_settings
from workforce.core.state import audit, configure_logging, exec_log, set_integration_status
from workforce.core.util import now, redact
from workforce.policies.engine import IntegrationError, IntegrationRevoked

RELOAD_S = 30
VOICE_MAX_S = 300
VOICE_MAX_BYTES = 10_000_000


class AgentPoller:
    """Message handling for one agent's bot, separated from the network loop for tests."""

    def __init__(self, db: Database, owner_id: int, data_dir: str | None = None):
        self.db, self.owner_id = db, int(owner_id)
        self.queue = TaskQueue(db)
        self.data_dir = Path(data_dir or get_settings().data_dir)

    def authorized(self, agent: dict, msg: dict) -> bool:
        sender = (msg.get("from") or {}).get("id")
        chat = msg.get("chat") or {}
        if sender != self.owner_id or chat.get("type") != "private":
            audit(self.db, f"telegram:{agent['id']}", "telegram.unauthorized", None,
                  {"from_id": sender, "chat_type": chat.get("type"), "agent": agent["id"]})
            return False
        return True

    def intro(self, agent: dict) -> str:
        from workforce.core.db import loads
        from workforce.custom_agents import TOOLS
        tools = [TOOLS[t]["label"] for t in loads(agent["tools_json"], []) if t in TOOLS]
        return "\n".join([f"{agent['emoji']} {agent['name']} here.", "", "My job: " + agent["task"][:600], "",
                          "I can: " + ("; ".join(tools) if tools else "just talk"),
                          f"Schedule: {agent['schedule']}" if agent.get("schedule") else "I only act when you message me.",
                          "", "Send text or a voice note. /forget clears what I remember, /run does my scheduled job now."])

    def handle_update(self, agent: dict, update: dict, client=None) -> str | None:
        """Returns a direct reply for commands; normal messages become tasks (the worker replies)."""
        from workforce.custom_agents import forget
        msg = update.get("message") or {}
        if not msg or not self.authorized(agent, msg):
            return None
        text = (msg.get("text") or msg.get("caption") or "").strip()
        uid = update.get("update_id")
        if text.startswith("/"):
            cmd = text.split()[0][1:].split("@")[0].lower()
            if cmd in ("start", "help"):
                return self.intro(agent)
            if cmd == "forget":
                n = forget(self.db, agent["id"])
                return f"Done. I forgot our last {n} messages."
            if cmd == "run":
                self._enqueue(agent, "run_task", {"agent_id": agent["id"]}, f"carunnow:{agent['id']}:{uid}")
                return "On it."
            return "Commands: /help, /forget, /run. Anything else, just say it."
        payload: dict = {"agent_id": agent["id"], "text": text[:4000]}
        voice = msg.get("voice") or msg.get("audio")
        if voice:
            if int(voice.get("duration") or 0) > VOICE_MAX_S:
                return "That voice note is over 5 minutes. Send a shorter one (or a few)."
            if client is None:
                return None
            try:
                data = client.download(voice["file_id"], max_bytes=VOICE_MAX_BYTES)
            except IntegrationError as e:
                return f"I couldn't download that voice note ({redact(str(e))[:80]})."
            d = self.data_dir / "voice" / agent["id"]
            d.mkdir(parents=True, exist_ok=True)
            path = d / f"{uid}.ogg"
            path.write_bytes(data)
            payload["voice_path"] = str(path)
        elif not text:
            return "I read text and voice notes."
        if not self._enqueue(agent, "chat", payload, f"ca:{agent['id']}:{uid}"):
            return "The custom agents engine is switched off in the dashboard (Bots), so I can't answer right now."
        if client is not None:
            client.typing(self.owner_id)
        return None

    def _enqueue(self, agent: dict, kind: str, payload: dict, key: str) -> bool:
        try:
            self.queue.enqueue(TaskSpec(bot_id="custom_agents", kind=kind, payload=payload, requested_by="telegram",
                                        idempotency_key=key))
        except BotDisabled:
            return False
        return True

    def save_offset(self, agent_id: str, offset: int) -> None:
        self.db.execute("UPDATE custom_agents SET updates_offset=? WHERE id=?", (offset, agent_id))

    def poll_once(self, agent: dict, client, timeout: int = 25) -> int:
        offset = self.db.scalar("SELECT updates_offset FROM custom_agents WHERE id=?", (agent["id"],))
        n = 0
        for u in client.get_updates(offset, timeout=timeout):
            reply = self.handle_update(agent, u, client)
            if reply:
                client.send(self.owner_id, reply)
            self.save_offset(agent["id"], u["update_id"] + 1)
            n += 1
        return n


def enabled_agents(db: Database) -> list[dict]:
    return db.all("SELECT * FROM custom_agents WHERE enabled=1 AND builtin=0 AND telegram_token_enc IS NOT NULL")


class Supervisor:
    """Starts / stops one polling thread per enabled agent; a changed row restarts its thread."""

    def __init__(self, db: Database, poller: AgentPoller, client_factory=None):
        from integrations.telegram.client import TelegramClient
        self.db, self.poller = db, poller
        self.factory = client_factory or TelegramClient
        self.threads: dict[str, tuple[float, threading.Event, threading.Thread]] = {}

    def reload(self) -> dict:
        want = {a["id"]: a for a in enabled_agents(self.db)}
        stopped = started = 0
        for aid, (ver, stop, _t) in list(self.threads.items()):
            if aid not in want or want[aid]["updated_at"] != ver:
                stop.set()
                del self.threads[aid]
                stopped += 1
        for aid, a in want.items():
            if aid in self.threads:
                continue
            stop = threading.Event()
            t = threading.Thread(target=self._run, args=(a, stop), name=f"agent-{aid}", daemon=True)
            self.threads[aid] = (a["updated_at"], stop, t)
            t.start()
            started += 1
        return {"running": len(self.threads), "started": started, "stopped": stopped}

    def _run(self, agent: dict, stop: threading.Event) -> None:  # pragma: no cover - network loop
        from workforce.secrets import SecretKeyMissing, decrypt
        try:
            client = self.factory(decrypt(agent["telegram_token_enc"]), str(self.poller.owner_id))
        except SecretKeyMissing as e:
            self._error(agent, str(e))
            return
        backoff = 5
        while not stop.is_set():
            try:
                self.poller.poll_once(agent, client)
                self.db.execute("UPDATE custom_agents SET last_error=NULL WHERE id=? AND last_error IS NOT NULL", (agent["id"],))
                backoff = 5
            except IntegrationRevoked as e:
                self._error(agent, f"Telegram rejected this bot's token ({e}). Paste a new one in the dashboard.")
                stop.wait(300)
            except IntegrationError as e:
                self._error(agent, str(e))
                stop.wait(10)
            except Exception as e:  # noqa: BLE001
                self._error(agent, f"{type(e).__name__}: {e}")
                stop.wait(backoff)
                backoff = min(backoff * 2, 300)

    def _error(self, agent: dict, msg: str) -> None:
        msg = redact(msg)[:300]
        self.db.execute("UPDATE custom_agents SET last_error=? WHERE id=?", (msg, agent["id"]))
        exec_log(self.db, "warning", f"agent bot {agent['id']}: {msg}")


def main() -> None:  # pragma: no cover - network loop
    from workforce.factory import bootstrap
    configure_logging()
    db = bootstrap()
    s = get_settings()
    if not (s.telegram_owner_id or "").strip().isdigit():
        print("agents disabled: TELEGRAM_OWNER_ID must be your numeric Telegram user id")
        while True:
            time.sleep(60)
    sup = Supervisor(db, AgentPoller(get_db(), int(s.telegram_owner_id)))
    exec_log(db, "info", "agents service started")
    while True:
        try:
            st = sup.reload()
            set_integration_status(db, "custom_agents", "ok" if st["running"] else "not_configured",
                                   f"{st['running']} agent bot(s) polling")
            db.execute("INSERT INTO system_state(key, value, updated_at, updated_by) VALUES ('agents_heartbeat', ?, ?, 'agents') "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at", (str(now()), now()))
        except Exception as e:  # noqa: BLE001
            exec_log(db, "error", f"agents supervisor error: {e}")
        time.sleep(RELOAD_S)


if __name__ == "__main__":
    main()
