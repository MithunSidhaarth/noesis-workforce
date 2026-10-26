"""Opportunity source registry: config/sources/*.yaml -> sources table.

Besides plain `sources:`, a YAML file may list `company_boards:` — one line per
company on Greenhouse, Lever or Ashby (their official public job-board JSON
APIs, published for automated reading). Each board expands into one source that
keeps only internship roles in Bengaluru / India / remote. Boards the owner adds
or removes in the dashboard live in the `company_boards` table."""
from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from ..core.db import Database, dumps, get_db
from ..core.settings import get_settings
from ..core.util import now
from ..registry import parse_every

INTERN_WORDS = ["intern", "interns", "internship", "internships", "trainee", "apprentice", "apprenticeship", "summer analyst"]
PLACE_WORDS = ["bengaluru", "bangalore", "india", "remote"]

# official ATS job-board APIs: URL, where the rows are, field paths, and the hosts their job links live on
ATS: dict[str, dict] = {
    "greenhouse": {
        "url": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
        "items": "jobs", "title": "title", "url_field": "absolute_url", "location": "location.name",
        "body": ["location.name", "content"], "published": "updated_at",
        "hosts": ["boards.greenhouse.io", "job-boards.greenhouse.io"], "max_bytes": 15_000_000,
    },
    "lever": {
        "url": "https://api.lever.co/v0/postings/{slug}?mode=json",
        "items": None, "title": "text", "url_field": "hostedUrl", "location": "categories.location",
        "body": ["categories.location", "categories.commitment", "categories.team", "descriptionPlain"],
        "published": "createdAt", "hosts": ["jobs.lever.co"], "max_bytes": 10_000_000,
    },
    "ashby": {
        "url": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
        "items": "jobs", "title": "title", "url_field": "jobUrl", "location": "location",
        "body": ["location", "employmentType", "descriptionPlain"], "published": "publishedAt",
        "hosts": ["jobs.ashbyhq.com"], "max_bytes": 10_000_000,
    },
}


class CompanyBoard(BaseModel):
    company: str = Field(min_length=1, max_length=80)
    ats: str = Field(pattern=r"^(greenhouse|lever|ashby)$")
    slug: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$")
    domain: str | None = Field(default=None, max_length=120)
    interval: str = Field(default="6h", pattern=r"^\d+[smhd]$")
    enabled: bool = True

    @property
    def id(self) -> str:
        return "board_" + re.sub(r"[^a-z0-9_]", "_", f"{self.ats}_{self.slug.lower()}")[:54]


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


def board_source(b: CompanyBoard) -> SourceConfig:
    a = ATS[b.ats]
    domains = list(a["hosts"]) + ([b.domain.lower().removeprefix("www.")] if b.domain else [])
    fields = {"title": a["title"], "url": a["url_field"], "location": a["location"], "body": a["body"],
              "published": a["published"]}
    parser = {"fields": fields, "match_any": {a["title"]: INTERN_WORDS, a["location"]: PLACE_WORDS},
              "organizer_name": b.company, "max_bytes": a["max_bytes"], "rolling": True}
    if a["items"]:
        parser["items"] = a["items"]
    return SourceConfig(id=b.id, name=f"{b.company} careers ({b.ats})", kind="json", url=a["url"].format(slug=b.slug),
                        access_method=f"Official {b.ats.capitalize()} public job-board API (published for automated reading)",
                        interval=b.interval, default_type="internship", official_domains=domains, parser=parser,
                        enabled=b.enabled)


def load_boards(config_dir: str | None = None) -> list[CompanyBoard]:
    d = Path(config_dir or get_settings().config_dir) / "sources"
    out = []
    for f in sorted(d.glob("*.yaml")):
        for b in (yaml.safe_load(f.read_text(encoding="utf-8")) or {}).get("company_boards", []) or []:
            out.append(CompanyBoard.model_validate(b))
    return out


def load_sources(config_dir: str | None = None, include_boards: bool = True) -> list[SourceConfig]:
    d = Path(config_dir or get_settings().config_dir) / "sources"
    out = []
    for f in sorted(d.glob("*.yaml")):
        for s in (yaml.safe_load(f.read_text(encoding="utf-8")) or {}).get("sources", []) or []:
            out.append(SourceConfig.model_validate(s))
    if include_boards:
        out += [board_source(b) for b in load_boards(config_dir)]
    return out


def sync_boards(db: Database, config_dir: str | None = None) -> list[CompanyBoard]:
    """YAML boards are seeded into company_boards; a board the owner disabled in the
    dashboard stays disabled (like bots). Returns every board, YAML and dashboard ones."""
    for b in load_boards(config_dir):
        if not db.scalar("SELECT 1 FROM company_boards WHERE id=?", (b.id,)):
            db.execute("INSERT OR IGNORE INTO company_boards(id, company, ats, slug, domain, interval, enabled, origin, created_at) "
                       "VALUES (?,?,?,?,?,?,?,'yaml',?)", (b.id, b.company, b.ats, b.slug, b.domain, b.interval, int(b.enabled), now()))
        else:
            db.execute("UPDATE company_boards SET company=?, domain=?, interval=? WHERE id=?", (b.company, b.domain, b.interval, b.id))
    return [CompanyBoard(company=r["company"], ats=r["ats"], slug=r["slug"], domain=r["domain"], interval=r["interval"],
                         enabled=bool(r["enabled"])) for r in db.all("SELECT * FROM company_boards ORDER BY company")]


def _upsert_source(db: Database, s: SourceConfig) -> None:
    params = (s.name, s.kind, s.url, s.access_method, parse_every(s.interval), dumps({**s.parser, "author_is_organizer": s.author_is_organizer}),
              dumps(s.official_domains), s.default_type, int(s.enabled))
    if db.scalar("SELECT 1 FROM sources WHERE id=?", (s.id,)):
        db.execute("UPDATE sources SET name=?, kind=?, url=?, access_method=?, interval_s=?, parser_json=?, "
                   "official_domains_json=?, default_type=?, enabled=? WHERE id=?", params + (s.id,))
    else:
        db.execute("INSERT INTO sources(name, kind, url, access_method, interval_s, parser_json, official_domains_json, "
                   "default_type, enabled, id) VALUES (?,?,?,?,?,?,?,?,?,?)", params + (s.id,))


def sync_board(db: Database, b: CompanyBoard) -> str:
    _upsert_source(db, board_source(b))
    return b.id


def sync_sources(db: Database | None = None, config_dir: str | None = None) -> list[str]:
    db = db or get_db()
    ids = []
    try:
        boards = sync_boards(db, config_dir)
    except Exception:  # noqa: BLE001  (company_boards not migrated yet)
        boards = load_boards(config_dir)
    for b in boards:
        ids.append(sync_board(db, b))
    for s in load_sources(config_dir, include_boards=False):
        ids.append(s.id)
        _upsert_source(db, s)
    # the owner's pasted links (Telegram /add, dashboard) are stored under this source; never swept
    _upsert_source(db, SourceConfig(id="owner_links", name="Links you sent", kind="json", url="owner://links",
                                    access_method="Single pages the owner pasted; fetched once, politely (robots.txt honored)",
                                    interval="1d", enabled=False))
    return ids


def load_profile(config_dir: str | None = None) -> dict:
    d = Path(config_dir or get_settings().config_dir)
    p = d / "profile.yaml"
    if not p.exists():
        p = d / "profile.example.yaml"
    profile = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return merge_owner_facts(profile)


def merge_owner_facts(profile: dict, db: Database | None = None) -> dict:
    """Facts the owner typed (Telegram /profile set, dashboard) override the YAML.
    They count as verified because the owner stated them directly."""
    try:
        rows = (db or get_db()).all("SELECT key, value, section, sensitive FROM profile_facts")
    except Exception:  # noqa: BLE001  (table not migrated yet, or no DB in this process)
        return profile
    for r in rows:
        sec = profile.get(r["section"]) or {}
        sec[r["key"]] = {"value": r["value"], "verified": True, "sensitive": bool(r["sensitive"]), "source": "owner"}
        profile[r["section"]] = sec
    return profile


def profile_problems(profile: dict) -> list[str]:
    """Startup check: things that will silently stall applications if left empty."""
    out = []
    facts = {k: v for k, v in (profile.get("facts") or {}).items() if isinstance(v, dict) and v.get("verified")}
    answers = {k: v for k, v in (profile.get("answers") or {}).items() if isinstance(v, dict) and v.get("verified")}
    if not facts:
        out.append("no verified facts yet: applications cannot check eligibility")
    for k in ("degree", "citizenship", "graduation_year"):
        if facts and k not in facts:
            out.append(f"missing fact: {k}")
    if "short_bio" not in answers:
        out.append("missing answer: short_bio")
    if not profile.get("interests"):
        out.append("no interests listed: every opportunity will score low")
    return out
