"""Builds the per-process service handles from environment configuration.

Integrations that are not configured are simply absent; agents degrade to
draft/notify-less behaviour and the integrations table records why."""
from __future__ import annotations

import os

from .core.db import Database, get_db
from .core.settings import get_settings
from .core.state import set_integration_status
from .policies.engine import Policies
from .runtime import Services


def _configured(db: Database, name: str, detail: str) -> None:
    """Credentials are present but no live call has succeeded yet. Never report
    "ok" here: only a successful real call (Telegram poll, Gmail list, publish)
    marks an integration ok."""
    cur = db.one("SELECT status FROM integrations WHERE name=?", (name,))
    if not cur or cur["status"] in ("not_configured", "configured", "revoked"):
        set_integration_status(db, name, "configured", detail)


def build_services(db: Database | None = None) -> Services:
    db = db or get_db()
    s = get_settings()
    extra: dict = {}

    from integrations.web.fetcher import Fetcher
    extra["fetcher"] = Fetcher()

    if s.telegram_token and s.telegram_owner_id and not s.telegram_owner_id.strip().isdigit():
        set_integration_status(db, "telegram", "error", "TELEGRAM_OWNER_ID must be your numeric Telegram user id")
    elif s.telegram_token and s.telegram_owner_id:
        from integrations.telegram.client import TelegramClient
        extra["telegram"] = TelegramClient(s.telegram_token, s.telegram_owner_id)
        _configured(db, "telegram", "credentials set; not yet verified by a live call")
    else:
        set_integration_status(db, "telegram", "not_configured", "TELEGRAM_BOT_TOKEN / TELEGRAM_OWNER_ID missing")

    from integrations.ntfy.client import NtfyClient
    from .policies.engine import IntegrationRevoked
    try:
        push = NtfyClient.from_env()
    except IntegrationRevoked as e:
        push = None
        set_integration_status(db, "ntfy", "error", str(e))
    if push is not None:
        extra["ntfy"] = push
        _configured(db, "ntfy", f"topic {push.topic} on {push.server}; not yet verified by a live push")
    elif not os.environ.get("NTFY_TOPIC"):
        set_integration_status(db, "ntfy", "not_configured", "NTFY_TOPIC not set in .env")

    if s.vault_path:
        from integrations.obsidian.vault import Vault, VaultError
        try:
            extra["vault"] = Vault(s.vault_path, s.timezone)
            set_integration_status(db, "obsidian", "ok", f"vault at {s.vault_path}")
        except VaultError as e:
            set_integration_status(db, "obsidian", "error", str(e))
    else:
        set_integration_status(db, "obsidian", "not_configured", "NOESIS_VAULT_PATH not set")

    from integrations.gmail.client import CalendarClient, GmailClient, GoogleAuth
    auth = GoogleAuth()
    if auth.configured():
        extra["mailbox"] = GmailClient(auth)
        extra["calendar"] = CalendarClient(auth)
        from integrations.applications.email_submit import make_email_submitter
        from workforce.sources.registry import load_profile
        extra["application_submitters"] = {"email": make_email_submitter(extra["mailbox"], load_profile, s.data_dir)}
        _configured(db, "gmail", "credentials set; not yet verified by a live call")
    else:
        set_integration_status(db, "gmail", "not_configured", "Google OAuth not set up")

    from integrations.social.publishers import InstagramPublisher, LinkedInPublisher
    li, ig = LinkedInPublisher(), InstagramPublisher()
    extra["publishers"] = {"linkedin": li, "instagram": ig}
    for name, pub in (("linkedin", li), ("instagram", ig)):
        if pub.configured():
            _configured(db, name, "credentials set; publishing disabled by policy until you enable it")
        else:
            set_integration_status(db, name, "not_configured", "")
    from integrations.social.publishers import PostizPublisher, ZernioPublisher
    from workforce.brands import load_brands
    postiz = zernio = 0
    for b in load_brands():
        for platform, ch in b["channels"].items():
            if ch.get("zernio_account"):
                extra["publishers"][f"{platform}:{b['id']}"] = ZernioPublisher(str(ch["zernio_account"]), platform,
                                                                                collaborators=ch.get("collaborators"))
                zernio += 1
            elif ch.get("postiz_integration"):
                extra["publishers"][f"{platform}:{b['id']}"] = PostizPublisher(str(ch["postiz_integration"]), platform)
                postiz += 1
    if zernio and os.environ.get("ZERNIO_API_KEY"):
        _configured(db, "zernio", f"{zernio} account(s) in config/brands.yaml; not yet verified by a live call")
    elif zernio:
        set_integration_status(db, "zernio", "not_configured", "ZERNIO_API_KEY missing in .env")
    if postiz and os.environ.get("POSTIZ_API_KEY"):
        _configured(db, "postiz", f"{postiz} channel(s) in config/brands.yaml; not yet verified by a live call")
    elif postiz:
        set_integration_status(db, "postiz", "not_configured", "POSTIZ_API_KEY missing in .env")

    pol = Policies()
    extra["policies_outreach"] = pol.get("outreach")
    return Services(db, extra=extra)


def bootstrap(db: Database | None = None) -> Database:
    """Migrate, sync bots and sources. Safe to run on every start."""
    from .registry import BotRegistry
    from .sources.registry import sync_sources
    db = db or get_db()
    db.migrate()
    BotRegistry(db).sync()
    sync_sources(db)
    return db
