import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TEST_BOTS = {
    "echo_a.yaml": """
id: echo_a
name: Echo A
purpose: test bot
agent: test_echo
tools: [telegram.notify_owner, email.send, social.publish, outreach.send, application.submit]
can_message: [echo_b]
max_concurrency: 3
retry: {max_attempts: 3, base_delay_s: 0, max_delay_s: 0.01}
max_delegation_depth: 3
""",
    "echo_b.yaml": """
id: echo_b
name: Echo B
purpose: test bot
agent: test_echo
tools: []
can_message: [echo_a]
max_concurrency: 2
retry: {max_attempts: 2, base_delay_s: 0, max_delay_s: 0.01}
""",
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated DB, config (real bots + test bots), policies, vault."""
    cfg = tmp_path / "config"
    shutil.copytree(ROOT / "config", cfg)
    for name, body in TEST_BOTS.items():
        (cfg / "bots" / name).write_text(body)
    pol = tmp_path / "policies"
    shutil.copytree(ROOT / "policies", pol)
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("NOESIS_DB_PATH", str(tmp_path / "noesis.db"))
    monkeypatch.setenv("NOESIS_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("NOESIS_POLICIES_DIR", str(pol))
    monkeypatch.setenv("NOESIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NOESIS_VAULT_PATH", str(vault))
    monkeypatch.setenv("NOESIS_DRY_RUN", "false")
    for k in ("ANTHROPIC_API_KEY", "GROQ_API_KEY", "GEMINI_API_KEY", "OLLAMA_ENABLED", "TELEGRAM_BOT_TOKEN"):
        monkeypatch.delenv(k, raising=False)

    from workforce.core import db as dbmod
    from workforce.core import settings as setmod
    setmod.reset_settings()
    dbmod.set_db(None)
    db = dbmod.get_db()
    db.migrate()
    import tests.helpers  # noqa: F401  registers test agents
    from workforce.registry import BotRegistry
    BotRegistry(db).sync()

    class Env:
        pass

    e = Env()
    e.db, e.tmp, e.config, e.policies, e.vault = db, tmp_path, cfg, pol, vault
    yield e
    db.close()
    dbmod.set_db(None)
    setmod.reset_settings()


@pytest.fixture
def services(env):
    from tests.helpers import FakeTelegram, ScriptedProvider
    from workforce.model_router.router import ModelRouter
    from workforce.runtime import Services
    router = ModelRouter(env.db, providers={})
    s = Services(env.db, router=router, extra={"telegram": FakeTelegram()})
    return s
