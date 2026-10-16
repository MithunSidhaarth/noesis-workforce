"""ntfy push notifications (ntfy.sh or a self-hosted server), free, no account.

The phone app subscribes to a topic; NOESIS publishes to it over HTTPS. On the
public ntfy.sh server anyone who knows the topic name can read it, so only the
headline (first line) of a notification is pushed; the full text stays in
Telegram and the dashboard. Set NTFY_FULL_TEXT=1 to push everything (only do
that with a private server or an access token).
"""
from __future__ import annotations

import os
import re

import httpx

from workforce.policies.engine import IntegrationError, IntegrationRevoked

# leading emoji -> ntfy tag (shown as the icon) and default priority
_TAGS = {"🔔": ("bell", 4), "⏰": ("alarm_clock", 4), "✋": ("raised_hand", 4), "⚠️": ("warning", 4),
         "🚨": ("rotating_light", 5), "📬": ("mailbox_with_mail", 4), "💡": ("bulb", 3), "🎯": ("dart", 3),
         "☀️": ("sunny", 3), "🌅": ("sunrise", 3), "📅": ("calendar", 3), "✅": ("white_check_mark", 3),
         "🎬": ("clapper", 3), "📝": ("memo", 3)}


def headline(text: str) -> str:
    return (text or "").strip().split("\n", 1)[0].strip()[:120] or "NOESIS"


class NtfyClient:
    def __init__(self, topic: str, server: str | None = None, token: str | None = None, full_text: bool | None = None,
                 click: str | None = None, transport: httpx.BaseTransport | None = None):
        topic = (topic or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", topic):
            raise IntegrationRevoked("NTFY_TOPIC is missing or not a valid topic name")
        self.topic = topic
        self.server = (server or "https://ntfy.sh").rstrip("/")
        self.token = token or ""
        self.full_text = bool(int(os.environ.get("NTFY_FULL_TEXT", "0") or 0)) if full_text is None else full_text
        self.click = click or ""
        self.client = httpx.Client(transport=transport, timeout=20)

    @classmethod
    def from_env(cls) -> "NtfyClient | None":
        topic = os.environ.get("NTFY_TOPIC", "").strip()
        if not topic:
            return None
        return cls(topic, os.environ.get("NTFY_SERVER") or None, os.environ.get("NTFY_TOKEN") or None,
                   click=os.environ.get("NTFY_CLICK_URL") or None)

    def message(self, text: str, urgent: bool = False) -> dict:
        """Build the ntfy JSON body: title = headline without its emoji, emoji -> tag."""
        head = headline(text)
        tag, prio = None, 3
        for emo, (t, p) in _TAGS.items():
            if head.startswith(emo):
                tag, prio, head = t, p, head[len(emo):].strip()
                break
        rest = (text or "").strip().split("\n", 1)[1].strip() if "\n" in (text or "").strip() else ""
        if self.full_text and rest:
            body = rest[:3500]
        else:
            body = "Open Telegram for the details." if rest else head
        msg = {"topic": self.topic, "title": head[:100], "message": body,
               "priority": 4 if urgent and prio < 4 else prio}
        if tag:
            msg["tags"] = [tag]
        if self.click:
            msg["click"] = self.click
        return msg

    def push(self, text: str, urgent: bool = False) -> dict:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        try:
            r = self.client.post(self.server, json=self.message(text, urgent), headers=headers)
        except httpx.HTTPError as e:
            raise IntegrationError(f"ntfy unreachable: {type(e).__name__}") from e
        if r.status_code in (401, 403):
            e = IntegrationRevoked(f"ntfy refused the message (HTTP {r.status_code}); check NTFY_TOKEN")
            e.integration = "ntfy"  # type: ignore[attr-defined]
            raise e
        if r.status_code == 429:
            raise IntegrationError("ntfy rate limited this server; try later")
        if r.status_code >= 300:
            raise IntegrationError(f"ntfy error HTTP {r.status_code}: {r.text[:200]}")
        mid = (r.json() or {}).get("id") if r.headers.get("content-type", "").startswith("application/json") else None
        if not mid:
            raise IntegrationError("ntfy accepted the request but returned no message id")
        return {"evidence": f"ntfy message id={mid} on topic {self.topic}", "message_id": mid}
