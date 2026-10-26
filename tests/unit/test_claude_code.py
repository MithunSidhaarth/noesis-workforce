"""Claude Code provider with a fake subprocess. The real CLI is never called."""
import json
import subprocess

import pytest

from workforce.contracts import BotConfig
from workforce.model_router.router import ClaudeCodeProvider, ModelRouter, ModelUnavailable, claude_code_status


class FakeRun:
    def __init__(self, stdout="", returncode=0, stderr="", raise_exc=None):
        self.stdout, self.returncode, self.stderr, self.raise_exc = stdout, returncode, stderr, raise_exc
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        if self.raise_exc:
            raise self.raise_exc
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, self.stderr)


def ok_json(text="hello", i=12, o=3):
    return json.dumps({"type": "result", "is_error": False, "result": text, "usage": {"input_tokens": i, "output_tokens": o}})


def test_command_disables_tools_and_sends_prompt_on_stdin(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    run = FakeRun(ok_json("hi there"))
    p = ClaudeCodeProvider(binary="claude", lock_path=str(tmp_path / "l"), runner=run)
    c = p.complete({"model": "haiku", "_timeout": 30}, "be brief", "SECRET PROMPT", 100)
    assert c.text == "hi there" and c.provider == "claude_code" and (c.input_tokens, c.output_tokens) == (12, 3)
    cmd, kw = run.calls[0]
    assert cmd[:2] == ["claude", "-p"]
    i = cmd.index("--tools")
    assert cmd[i + 1] == ""                       # every built-in tool disabled
    assert "--strict-mcp-config" in cmd and "--no-session-persistence" in cmd
    assert cmd[cmd.index("--system-prompt") + 1] == "be brief"
    assert "SECRET PROMPT" not in " ".join(cmd)   # prompt goes on stdin, not argv
    assert kw["input"] == "SECRET PROMPT" and kw["timeout"] == 30
    assert "ANTHROPIC_API_KEY" not in kw["env"]   # subscription login, never a pay-as-you-go key
    assert kw["cwd"] and "noesis-claude-" in kw["cwd"]


@pytest.mark.parametrize("run", [
    FakeRun(returncode=1, stderr="Claude AI usage limit reached"),
    FakeRun(json.dumps({"is_error": True, "result": "Invalid API key · Please run /login"})),
    FakeRun("not json at all", returncode=0),
    FakeRun(raise_exc=subprocess.TimeoutExpired("claude", 5)),
    FakeRun(raise_exc=FileNotFoundError("claude")),
])
def test_failures_become_model_unavailable(tmp_path, run):
    p = ClaudeCodeProvider(binary="claude", lock_path=str(tmp_path / "l"), runner=run)
    with pytest.raises(ModelUnavailable):
        p.complete({"model": "sonnet"}, "", "x", 10)


def test_router_prefers_claude_code_and_falls_back(env, monkeypatch):
    from tests.helpers import ScriptedProvider
    bot = BotConfig(id="echo_a", name="a", purpose="t", agent="test_echo", model={"allowed_tiers": ["cheap", "standard"]})
    cc = ClaudeCodeProvider(lock_path=str(env.tmp / "l"), runner=FakeRun(returncode=1, stderr="usage limit"))
    gem = ScriptedProvider(["from gemini"])
    r = ModelRouter(env.db, providers={"claude_code": cc, "gemini": gem})
    assert r.candidates(bot, "write")[0]["provider"] == "claude_code"
    c = r.complete(bot, "write", "p", cache=False)
    assert c.text == "from gemini" and gem.calls == 1
    rows = env.db.all("SELECT provider, ok, cost_usd FROM model_usage ORDER BY id")
    assert rows[0]["provider"] == "claude_code" and rows[0]["ok"] == 0 and rows[-1]["ok"] == 1


def test_provider_order_puts_gemini_first_for_bulk_bots(env):
    from tests.helpers import ScriptedProvider
    bot = BotConfig(id="echo_a", name="a", purpose="t", agent="test_echo",
                    model={"allowed_tiers": ["cheap"], "provider_order": ["gemini"]})
    r = ModelRouter(env.db, providers={"claude_code": ScriptedProvider(), "gemini": ScriptedProvider()})
    assert [m["provider"] for m in r.candidates(bot, "extract")][:2] == ["gemini", "claude_code"]


def test_env_gate(env, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_ENABLED", raising=False)
    assert ModelRouter(env.db).provider("claude_code") is None
    monkeypatch.setenv("CLAUDE_CODE_ENABLED", "0")
    assert ModelRouter(env.db).provider("claude_code") is None
    monkeypatch.setenv("CLAUDE_CODE_ENABLED", "1")
    assert isinstance(ModelRouter(env.db).provider("claude_code"), ClaudeCodeProvider)


def test_status_check(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_LOCK", str(tmp_path / "l"))
    assert claude_code_status(runner=FakeRun(ok_json("ok")))[0] is True
    ok, detail = claude_code_status(runner=FakeRun(returncode=1, stderr="Not logged in · Please run /login"))
    assert not ok and "logged out" in detail
