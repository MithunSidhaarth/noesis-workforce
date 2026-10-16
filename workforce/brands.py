"""Your brands (accounts) and the channels each one posts to.

config/brands.yaml (copy config/brands.example.yaml). Not secret: it holds names,
voice notes and Postiz channel ids; the Postiz API key stays in .env.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from .core.settings import get_settings

PLATFORMS = ("instagram", "linkedin")


def load_brands(config_dir: str | None = None) -> list[dict]:
    p = Path(config_dir or get_settings().config_dir) / "brands.yaml"
    if not p.exists():
        return []
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    out = []
    for b in data.get("brands") or []:
        if not isinstance(b, dict) or not b.get("id"):
            continue
        chans = {k: (v or {}) for k, v in (b.get("channels") or {}).items() if k in PLATFORMS}
        out.append({**b, "id": str(b["id"]), "name": b.get("name") or b["id"], "channels": chans,
                    "topics": [str(t).lower() for t in b.get("topics") or []]})
    return out


def get_brand(brand_id: str | None, config_dir: str | None = None) -> dict | None:
    return next((b for b in load_brands(config_dir) if b["id"] == brand_id), None) if brand_id else None


def _mentions(text: str, word: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text) is not None


def brands_for(text: str, brands: list[dict]) -> list[dict]:
    """Brands a piece of work belongs to. Brands whose topics are mentioned (as whole
    words) take it; brands without topics cover the platforms those don't post on.
    So Cue work goes to the Cue brand on Instagram and still to your LinkedIn."""
    t = text.lower()
    matched = [b for b in brands if b["topics"] and any(_mentions(t, w) for w in b["topics"])]
    general = [b for b in brands if not b["topics"]]
    if not matched:
        return general or brands
    covered = {pl for b in matched for pl in b["channels"]}
    fill = [{**b, "channels": {k: v for k, v in b["channels"].items() if k not in covered}} for b in general]
    return matched + [b for b in fill if b["channels"]]


def voice_text(brand: dict | None) -> str:
    if not brand:
        return ""
    parts = [f"Account: {brand['name']}" + (f" ({brand['handle']})" if brand.get("handle") else "")]
    for k in ("about", "audience", "voice"):
        if brand.get(k):
            parts.append(f"{k.capitalize()}: {brand[k]}")
    if brand.get("link"):
        parts.append(f"Link: {brand['link']} (Instagram: say 'link in bio', never paste the URL; LinkedIn: include the URL once at the end)")
    return "\n".join(parts)


def link_line(brand: dict | None, platform: str) -> str:
    """Closing call-to-action for template drafts when the brand has a link."""
    if not brand or not brand.get("link"):
        return ""
    return "Link in bio." if platform == "instagram" else f"Try it: {brand['link']}"
