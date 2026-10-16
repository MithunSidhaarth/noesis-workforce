"""Opportunity source registry: config/sources/*.yaml -> sources table."""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from ..core.db import Database, dumps, get_db
from ..core.settings import get_settings
from ..registry import parse_every


class SourceConfig(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9_]{2,60}$")
    name: str
    kind: str = Field(pattern=r"^(rss|html|json|fixture)$")
    url: str
    access_method: str = Field(min_length=10)
    interval: str = "12h"
    default_type: str | None = None
    official_domains: list[str] = Field(default_factory=list)
    author_is_organizer: bool = False
    parser: dict = Field(default_factory=dict)
    enabled: bool = True


def load_sources(config_dir: str | None = None) -> list[SourceConfig]:
    d = Path(config_dir or get_settings().config_dir) / "sources"
    out = []
    for f in sorted(d.glob("*.yaml")):
        for s in (yaml.safe_load(f.read_text(encoding="utf-8")) or {}).get("sources", []):
            out.append(SourceConfig.model_validate(s))
    return out


def sync_sources(db: Database | None = None, config_dir: str | None = None) -> list[str]:
    db = db or get_db()
    ids = []
    for s in load_sources(config_dir):
        ids.append(s.id)
        params = (s.name, s.kind, s.url, s.access_method, parse_every(s.interval), dumps({**s.parser, "author_is_organizer": s.author_is_organizer}),
                  dumps(s.official_domains), s.default_type, int(s.enabled))
        if db.scalar("SELECT 1 FROM sources WHERE id=?", (s.id,)):
            db.execute("UPDATE sources SET name=?, kind=?, url=?, access_method=?, interval_s=?, parser_json=?, "
                       "official_domains_json=?, default_type=?, enabled=? WHERE id=?", params + (s.id,))
        else:
            db.execute("INSERT INTO sources(name, kind, url, access_method, interval_s, parser_json, official_domains_json, "
                       "default_type, enabled, id) VALUES (?,?,?,?,?,?,?,?,?,?)", params + (s.id,))
    return ids


def load_profile(config_dir: str | None = None) -> dict:
    d = Path(config_dir or get_settings().config_dir)
    p = d / "profile.yaml"
    if not p.exists():
        p = d / "profile.example.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
