"""Runtime settings, read from environment variables only (never from bots)."""
from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    db_path: str = field(default_factory=lambda: os.environ.get("NOESIS_DB_PATH", str(ROOT / "data" / "noesis.db")))
    config_dir: str = field(default_factory=lambda: os.environ.get("NOESIS_CONFIG_DIR", str(ROOT / "config")))
    policies_dir: str = field(default_factory=lambda: os.environ.get("NOESIS_POLICIES_DIR", str(ROOT / "policies")))
    prompts_dir: str = field(default_factory=lambda: os.environ.get("NOESIS_PROMPTS_DIR", str(ROOT / "prompts")))
    data_dir: str = field(default_factory=lambda: os.environ.get("NOESIS_DATA_DIR", str(ROOT / "data")))
    # Obsidian vault. Configurable: a Windows path on the dev machine, a synced
    # working copy on the VPS. Unset = knowledge writes go to data/vault-staging.
    vault_path: str = field(default_factory=lambda: os.environ.get("NOESIS_VAULT_PATH", ""))
    timezone: str = field(default_factory=lambda: os.environ.get("NOESIS_TIMEZONE", "Asia/Kolkata"))
    # Safety defaults: dry-run ON until explicitly turned off.
    dry_run_default: bool = field(default_factory=lambda: _bool("NOESIS_DRY_RUN", True))
    telegram_token: str = field(default_factory=lambda: os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    telegram_owner_id: str = field(default_factory=lambda: os.environ.get("TELEGRAM_OWNER_ID", ""))
    redis_url: str = field(default_factory=lambda: os.environ.get("REDIS_URL", ""))
    worker_id: str = field(default_factory=lambda: os.environ.get("NOESIS_WORKER_ID") or socket.gethostname())
    # stable across restarts of the same container, so a restarted worker hands back its own leases at once
    poll_interval_s: float = field(default_factory=lambda: float(os.environ.get("NOESIS_POLL_INTERVAL", "2")))
    http_user_agent: str = field(default_factory=lambda: os.environ.get(
        "NOESIS_USER_AGENT", "NOESIS-Workforce/0.1 (+personal opportunity monitor; contact owner)"))


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    global _settings
    _settings = None
