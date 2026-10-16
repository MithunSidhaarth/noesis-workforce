"""Model-agnostic router with budgets, caching, dedupe and fallbacks.

Bots call `router.complete(bot, capability, ...)`. The router:
  1. checks the result cache (identical system+prompt+capability)
  2. checks per-bot daily call/budget limits and the global daily budget
  3. picks the first configured model allowed by the bot's tiers and capable of the task
  4. on provider failure tries the next candidate; records every attempt in model_usage
Raises BudgetExceeded (caller should defer the task) or ModelUnavailable (caller
should retry later or fall back to deterministic logic). Never fabricates output.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import yaml

from ..contracts import BotConfig
from ..core.db import Database, dumps, get_db, loads
from ..core.settings import get_settings
from ..core.util import now, sha


class BudgetExceeded(Exception):
    pass


class ModelUnavailable(Exception):
    pass


@dataclass
class Completion:
    text: str
    model: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached: bool = False

    def json(self) -> Any:
        t = self.text.strip()
        if t.startswith("```"):
            t = t.split("\n", 1)[1].rsplit("```", 1)[0]
        return json.loads(t)


class Provider(Protocol):
    def complete(self, model: dict, system: str, prompt: str, max_tokens: int) -> Completion: ...


class AnthropicProvider:
    """Official Anthropic SDK. Requires a paid API key; a Claude Pro subscription
    does not authorize unattended runtime API calls."""

    def __init__(self):
        import anthropic  # optional dependency
        self._anthropic = anthropic
        self.client = anthropic.Anthropic(max_retries=2, timeout=120.0)

    def complete(self, model: dict, system: str, prompt: str, max_tokens: int) -> Completion:
        a = self._anthropic
        kwargs: dict[str, Any] = {}
        if model.get("effort"):
            kwargs["output_config"] = {"effort": model["effort"]}
        if model["model"] in ("claude-opus-5-5", "claude-sonnet-5-5"):
            # route refusals to a fallback model server-side
            kwargs["extra_headers"] = {"anthropic-beta": "server-side-fallback-2026-07-01"}
            kwargs["extra_body"] = {"fallbacks": "default"}
        try:
            timeout = float(model.get("_timeout", 120))
            client = self.client.with_options(timeout=timeout, max_retries=2 if timeout >= 90 else 0)
            resp = client.messages.create(
                model=model["model"], max_tokens=max_tokens, system=system,
                messages=[{"role": "user", "content": prompt}], **kwargs)
        except a.AuthenticationError as e:
            raise ModelUnavailable(f"anthropic auth failed: {e}") from e
        except a.RateLimitError as e:
            raise ModelUnavailable(f"anthropic rate limited: {e}") from e
        except a.APIStatusError as e:
            raise ModelUnavailable(f"anthropic error {e.status_code}: {e}") from e
        except a.APIConnectionError as e:
            raise ModelUnavailable(f"anthropic connection error: {e}") from e
        if resp.stop_reason == "refusal":
            raise ModelUnavailable("model declined the request")
        text = "".join(b.text for b in resp.content if b.type == "text")
        return Completion(text=text, model=model["model"], provider="anthropic",
                          input_tokens=resp.usage.input_tokens, output_tokens=resp.usage.output_tokens)


class OpenAICompatibleProvider:
    """Groq / Gemini / OpenRouter / Ollama via their OpenAI-compatible chat endpoint."""

    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def complete(self, model: dict, system: str, prompt: str, max_tokens: int) -> Completion:
        import httpx
        try:
            r = httpx.post(f"{self.base_url}/chat/completions", timeout=float(model.get("_timeout", 120)),
                           headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {},
                           json={"model": model["model"], "max_tokens": max_tokens,
                                 "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]})
        except httpx.HTTPError as e:
            raise ModelUnavailable(f"{model['provider']} connection error: {e}") from e
        if r.status_code != 200:
            raise ModelUnavailable(f"{model['provider']} HTTP {r.status_code}: {r.text[:200]}")
        d = r.json()
        u = d.get("usage") or {}
        return Completion(text=d["choices"][0]["message"]["content"] or "", model=model["model"], provider=model["provider"],
                          input_tokens=u.get("prompt_tokens", 0), output_tokens=u.get("completion_tokens", 0))


class ModelRouter:
    def __init__(self, db: Database | None = None, config_path: str | None = None,
                 providers: dict[str, Provider] | None = None):
        self.db = db or get_db()
        path = Path(config_path or Path(get_settings().config_dir) / "models.yaml")
        self.cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
        self._providers: dict[str, Provider] = dict(providers or {})
        self._explicit = providers is not None

    def provider(self, name: str) -> Provider | None:
        if name in self._providers:
            return self._providers[name]
        if self._explicit:
            return None
        pc = self.cfg["providers"].get(name)
        if not pc or not os.environ.get(pc["env"]):
            return None
        try:
            if pc["kind"] == "anthropic":
                p: Provider = AnthropicProvider()
            else:
                key = os.environ.get(pc["env"], "")
                p = OpenAICompatibleProvider(pc["base_url"], "" if pc["env"] == "OLLAMA_ENABLED" else key)
        except Exception:
            return None
        self._providers[name] = p
        return p

    def candidates(self, bot: BotConfig, capability: str) -> list[dict]:
        out = []
        for m in self.cfg["models"]:
            if m["tier"] not in bot.model.allowed_tiers or capability not in m["capabilities"]:
                continue
            if self.provider(m["provider"]) is not None:
                out.append(m)
        return out

    def available(self) -> list[str]:
        return [m["id"] for m in self.cfg["models"] if self.provider(m["provider"]) is not None]

    def _day_start(self) -> float:
        tz = ZoneInfo(get_settings().timezone)
        return datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    def usage_today(self, bot_id: str | None = None) -> dict:
        q = "SELECT COUNT(*) calls, COALESCE(SUM(cost_usd),0) cost FROM model_usage WHERE ts>=? AND cached=0"
        args: tuple = (self._day_start(),)
        if bot_id:
            q += " AND bot_id=?"
            args += (bot_id,)
        return self.db.one(q, args) or {"calls": 0, "cost": 0}

    def complete(self, bot: BotConfig, capability: str, prompt: str, system: str = "", max_tokens: int = 1500,
                 task_id: str | None = None, cache: bool = True, time_left: float | None = None) -> Completion:
        """time_left: seconds the calling task has left; provider timeouts are capped to it."""
        key = sha(capability, system, prompt, max_tokens)
        if cache:
            r = self.db.one("SELECT response_json, created_at FROM model_cache WHERE key=?", (key,))
            if r and now() - r["created_at"] < self.cfg.get("cache_ttl_s", 0):
                d = loads(r["response_json"])
                self._record(bot.id, task_id, d["provider"], d["model"], capability, 0, 0, 0.0, cached=True)
                return Completion(text=d["text"], model=d["model"], provider=d["provider"], cached=True)

        mine = self.usage_today(bot.id)
        if mine["calls"] >= bot.model.daily_call_limit:
            raise BudgetExceeded(f"{bot.id} reached its daily model call limit ({bot.model.daily_call_limit})")
        if bot.model.daily_budget_usd and mine["cost"] >= bot.model.daily_budget_usd:
            raise BudgetExceeded(f"{bot.id} reached its daily model budget (${bot.model.daily_budget_usd})")
        cands = self.candidates(bot, capability)
        if not cands:
            raise ModelUnavailable(f"no configured model for capability '{capability}' in tiers {bot.model.allowed_tiers}")
        errors = []
        started = now()
        for m in cands:
            est = (len(prompt + system) / 4 * m["in"] + max_tokens * m["out"]) / 1e6
            if est > 0 and self.usage_today()["cost"] + est > self.cfg.get("global_daily_budget_usd", 0):
                errors.append(f"{m['id']}: global daily budget would be exceeded")
                continue
            if est > 0 and bot.model.daily_budget_usd and mine["cost"] + est > bot.model.daily_budget_usd:
                errors.append(f"{m['id']}: bot daily budget would be exceeded")
                continue
            if time_left is not None:
                left = time_left - (now() - started)
                if left < 20:
                    errors.append(f"{m['id']}: not enough task time left")
                    break
                m = {**m, "_timeout": min(120.0, left - 10)}
            try:
                c = self.provider(m["provider"]).complete(m, system, prompt, max_tokens)
            except ModelUnavailable as e:
                self._record(bot.id, task_id, m["provider"], m["model"], capability, 0, 0, 0.0, ok=False, error=str(e))
                errors.append(f"{m['id']}: {e}")
                continue
            cost = (c.input_tokens * m["in"] + c.output_tokens * m["out"]) / 1e6
            self._record(bot.id, task_id, m["provider"], m["model"], capability, c.input_tokens, c.output_tokens, cost)
            if cache:
                self.db.execute("INSERT OR REPLACE INTO model_cache(key, response_json, created_at) VALUES (?,?,?)",
                                (key, dumps({"text": c.text, "model": c.model, "provider": c.provider}), now()))
            return c
        if errors and all("budget" in e for e in errors):
            raise BudgetExceeded("; ".join(errors))
        raise ModelUnavailable("; ".join(errors))

    def _record(self, bot_id, task_id, provider, model, capability, i, o, cost, cached=False, ok=True, error=None):
        self.db.execute(
            "INSERT INTO model_usage(ts, bot_id, task_id, provider, model, capability, input_tokens, output_tokens, cost_usd, cached, ok, error) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (now(), bot_id, task_id, provider, model, capability, i, o, cost, int(cached), int(ok), error))
