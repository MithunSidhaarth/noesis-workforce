"""Test-only agents, fakes and fixtures. Fakes are clearly fakes: nothing here
talks to a real external service."""
from __future__ import annotations

import threading
import time

from workforce.agents.base import Agent, register
from workforce.model_router.router import Completion, ModelUnavailable
from workforce.policies.engine import IntegrationError, IntegrationRevoked


class FakeTelegram:
    def __init__(self):
        self.sent: list[str] = []
        self.buttons: list[list | None] = []

    def send_owner(self, text: str, buttons: list | None = None) -> dict:
        self.sent.append(text)
        self.buttons.append(buttons)
        return {"evidence": f"fake-telegram-message-{len(self.sent)}", "message_id": len(self.sent)}


class ScriptedProvider:
    """A fake model provider returning scripted responses."""

    def __init__(self, responses: list[str] | None = None, fail: bool = False):
        self.responses = list(responses or [])
        self.calls = 0
        self.fail = fail

    def complete(self, model, system, prompt, max_tokens):
        self.calls += 1
        if self.fail:
            raise ModelUnavailable("scripted outage")
        text = self.responses.pop(0) if self.responses else '{"ok": true}'
        return Completion(text=text, model=model["model"], provider=model["provider"], input_tokens=100, output_tokens=50)


CONCURRENCY = {"max": 0, "now": 0}
_lock = threading.Lock()
SIDE_EFFECTS: list[str] = []


@register("test_echo")
class EchoAgent(Agent):
    accepts = {"echo", "sleep", "flaky", "always_fail", "revoked", "send_email"}

    def handlers(self):
        return {
            "echo": lambda ctx, t: {"echo": t.payload.get("text"), "deps": ctx.queue.dependency_results(t)},
            "sleep": self.sleep,
            "flaky": self.flaky,
            "always_fail": self.always_fail,
            "revoked": self.revoked,
            "send_email": self.send_email,
            "delegate": self.delegate,
        }

    def sleep(self, ctx, t):
        with _lock:
            CONCURRENCY["now"] += 1
            CONCURRENCY["max"] = max(CONCURRENCY["max"], CONCURRENCY["now"])
        time.sleep(t.payload.get("s", 0.3))
        with _lock:
            CONCURRENCY["now"] -= 1
        return {"slept": True}

    def flaky(self, ctx, t):
        if t.attempts < 2:
            raise IntegrationError("temporary outage")
        return {"ok": True, "attempts": t.attempts}

    def always_fail(self, ctx, t):
        raise IntegrationError("service down")

    def revoked(self, ctx, t):
        e = IntegrationRevoked("token revoked by provider")
        e.integration = "gmail"
        raise e

    def send_email(self, ctx, t):
        def executor(req):
            SIDE_EFFECTS.append(req.idempotency_key)
            return {"evidence": f"sent:{req.idempotency_key}"}
        res = ctx.act("email.send", t.payload["key"], executor, target=t.payload.get("to", "a@example.com"),
                      params={"body": t.payload.get("body", "")}, attributes=t.payload.get("attrs", {}))
        return res.model_dump()

    def delegate(self, ctx, t):
        ctx.delegate(t.payload["to"], "echo", {"text": t.payload.get("text", "hi")})
        return {"delegated": True}

    def on_task_result(self, ctx, msg):
        ctx.memory_set("last_result", msg["payload"])

    def on_alert(self, ctx, msg):
        # an echo that would loop forever if the bus did not stop it
        other = "echo_b" if ctx.bot.id == "echo_a" else "echo_a"
        ctx.bus.send({"sender": ctx.bot.id, "recipient": other, "type": "ALERT", "payload": msg["payload"],
                      "correlation_id": msg["correlation_id"], "hop_count": msg["hop_count"] + 1})
