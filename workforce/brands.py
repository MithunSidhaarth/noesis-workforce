"""Your brands (accounts) and the channels each one posts to.

config/brands.yaml (copy config/brands.example.yaml). Not secret: it holds names,
voice notes and Postiz channel ids; the Postiz API key stays in .env.
"""
from __future__ import annotations

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


def brands_for(text: str, brands: list[dict]) -> list[dict]:
    """Brands a piece of work belongs to: those whose topics match, or all brands without topics."""
    t = text.lower()
    matched = [b for b in brands if b["topics"] and any(w in t for w in b["topics"])]
    return matched or [b for b in brands if not b["topics"]] or brands


def voice_text(brand: dict | None) -> str:
    if not brand:
        return ""
    parts = [f"Account: {brand['name']}" + (f" ({brand['handle']})" if brand.get("handle") else "")]
    for k in ("about", "audience", "voice"):
        if brand.get(k):
            parts.append(f"{k.capitalize()}: {brand[k]}")
    return "\n".join(parts)
