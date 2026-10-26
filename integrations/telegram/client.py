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

    def send(self, chat_id: int, text: str, buttons: list | None = None) -> dict:
        """buttons: rows of {"text", "data"} rendered as an inline keyboard on the last chunk."""
        res = None
        chunks = [text[i:i + MAX_LEN] for i in range(0, max(1, len(text)), MAX_LEN)]
        for n, chunk in enumerate(chunks):
            extra = {}
            if buttons and n == len(chunks) - 1:
                extra["reply_markup"] = {"inline_keyboard": [[{"text": b["text"], "callback_data": b["data"][:64]} for b in row]
                                                             for row in buttons]}
            res = self._call("sendMessage", chat_id=chat_id, text=chunk, disable_web_page_preview=True, **extra)
        return res or {}

    def answer_callback(self, callback_id: str, text: str = "") -> dict:
        return self._call("answerCallbackQuery", callback_query_id=callback_id, text=text[:190])

    def edit_message(self, chat_id: int, message_id: int, text: str) -> dict:
        return self._call("editMessageText", chat_id=chat_id, message_id=message_id, text=text[:MAX_LEN],
                          disable_web_page_preview=True)

    def send_owner(self, text: str, buttons: list | None = None) -> dict:
        res = self.send(self.owner_id, text, buttons)
        return {"evidence": f"telegram message_id={res.get('message_id')}", "message_id": res.get("message_id")}

    def get_updates(self, offset: int | None, timeout: int = 30) -> list[dict]:
        return self._call("getUpdates", offset=offset, timeout=timeout, allowed_updates=["message", "callback_query"])

    def me(self) -> dict:
        return self._call("getMe")

    def typing(self, chat_id: int) -> None:
        try:
            self._call("sendChatAction", chat_id=chat_id, action="typing")
        except IntegrationError:
            pass   # cosmetic

    def download(self, file_id: str, max_bytes: int = 10_000_000) -> bytes:
        """A file the owner sent (voice note). Telegram bots can fetch files up to 20 MB."""
        info = self._call("getFile", file_id=file_id)
        if int(info.get("file_size") or 0) > max_bytes:
            raise IntegrationError("file too large")
        try:
            r = self.client.get(f"{API}/file/bot{self.token}/{info['file_path']}")
        except httpx.HTTPError as e:
            raise IntegrationError(f"telegram file download failed: {type(e).__name__}") from e
        if r.status_code != 200 or len(r.content) > max_bytes:
            raise IntegrationError(f"telegram file download failed: HTTP {r.status_code}")
        return r.content
