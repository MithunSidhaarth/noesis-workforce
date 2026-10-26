"""Sending as a custom agent's own Telegram bot.

The BotFather tokens live encrypted in custom_agents.telegram_token_enc. Only this
sender and the agents poller (services/agents/main.py) decrypt them; agents get a
`send(agent_id, text)` handle, never the token. Messages only ever go to the owner's
numeric Telegram id, in the private chat with that bot.
"""
from __future__ import annotations

from workforce.core.db import Database
from workforce.policies.engine import IntegrationRevoked
from workforce.secrets import decrypt

from .client import TelegramClient


class CustomBotSender:
    def __init__(self, db: Database, owner_id: str | int, client_factory=TelegramClient):
        if not str(owner_id or "").strip().isdigit():
            raise IntegrationRevoked("TELEGRAM_OWNER_ID must be your numeric Telegram user id")
        self.db, self.owner_id, self.factory = db, int(owner_id), client_factory
        self._clients: dict[str, tuple[float, TelegramClient]] = {}

    def client(self, agent_id: str) -> TelegramClient:
        row = self.db.one("SELECT telegram_token_enc, updated_at, enabled FROM custom_agents WHERE id=?", (agent_id,))
        if not row or not row["telegram_token_enc"]:
            raise IntegrationRevoked(f"agent {agent_id} has no Telegram bot token")
        cached = self._clients.get(agent_id)
        if cached and cached[0] == row["updated_at"]:
            return cached[1]
        c = self.factory(decrypt(row["telegram_token_enc"]), str(self.owner_id))
        self._clients[agent_id] = (row["updated_at"], c)
        return c

    def send(self, agent_id: str, text: str, buttons: list | None = None) -> dict:
        res = self.client(agent_id).send(self.owner_id, text, buttons)
        return {"evidence": f"telegram ({agent_id}) message_id={res.get('message_id')}", "message_id": res.get("message_id")}
