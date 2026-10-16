"""Telegram Bot API client (official HTTPS API, long polling; no inbound port)."""
from __future__ import annotations

import httpx

from workforce.policies.engine import IntegrationError, IntegrationRevoked

API = "https://api.telegram.org"
MAX_LEN = 4000


class TelegramClient:
    def __init__(self, token: str, owner_id: str, transport: httpx.BaseTransport | None = None):
        if not token or not owner_id:
            raise IntegrationRevoked("TELEGRAM_BOT_TOKEN / TELEGRAM_OWNER_ID not configured")
        self.token = token
        self.owner_id = int(owner_id)
        self.client = httpx.Client(transport=transport, timeout=40)

    def _call(self, method: str, **params) -> dict:
        try:
            r = self.client.post(f"{API}/bot{self.token}/{method}", json=params)
        except httpx.HTTPError as e:
            raise IntegrationError(f"telegram unreachable: {type(e).__name__}") from e
        if r.status_code in (401, 404):
            e = IntegrationRevoked("telegram token rejected (revoked or wrong)")
            e.integration = "telegram"  # type: ignore[attr-defined]
            raise e
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code == 429:
            raise IntegrationError(f"telegram rate limited: retry after {data.get('parameters', {}).get('retry_after')}s")
        if not data.get("ok"):
            raise IntegrationError(f"telegram error {r.status_code}: {data.get('description', '')[:200]}")
        return data["result"]

    def send(self, chat_id: int, text: str) -> dict:
        res = None
        for i in range(0, max(1, len(text)), MAX_LEN):
            res = self._call("sendMessage", chat_id=chat_id, text=text[i:i + MAX_LEN], disable_web_page_preview=True)
        return res or {}

    def send_owner(self, text: str) -> dict:
        res = self.send(self.owner_id, text)
        return {"evidence": f"telegram message_id={res.get('message_id')}", "message_id": res.get("message_id")}

    def get_updates(self, offset: int | None, timeout: int = 30) -> list[dict]:
        return self._call("getUpdates", offset=offset, timeout=timeout, allowed_updates=["message", "callback_query"])

    def me(self) -> dict:
        return self._call("getMe")
