"""Trend signals for project ideas, from official public APIs only.

* GitHub REST search: repositories created in the last N days, most starred.
* Hugging Face Hub API: trending models and Spaces.
* Hacker News (Algolia API): recent Show HN posts with traction.
* arXiv API: newest papers in the categories you pick.
No scraping, no login. Unauthenticated GitHub search allows 10 requests a
minute; set GITHUB_TOKEN (read-only, no scopes) in .env to raise it.
"""
from __future__ import annotations

import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import httpx

from workforce.core.settings import get_settings


# Trending lists regularly include adult models and repos; never show them.
BLOCK_WORDS = ("nsfw", "porn", "hentai", "nude", "nudity", "erotic", "sexual", "onlyfans", "undress", "xxx", "18+")


def safe(item: dict) -> bool:
    text = f"{item.get('title', '')} {item.get('summary', '')} {' '.join(map(str, item.get('tags') or []))}".lower()
    return not any(w in text for w in BLOCK_WORDS)


def _client(transport=None) -> httpx.Client:
    return httpx.Client(transport=transport, timeout=20, follow_redirects=True,
                        headers={"User-Agent": get_settings().http_user_agent})


def github_rising(c: httpx.Client, days: int = 7, n: int = 15, topics: list[str] | None = None) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    h = {"Accept": "application/vnd.github+json"}
    if os.environ.get("GITHUB_TOKEN"):
        h["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    out: list[dict] = []
    for q in [f"created:>{since}"] + [f"created:>{since} topic:{t}" for t in (topics or [])[:3]]:
        r = c.get("https://api.github.com/search/repositories", params={"q": q, "sort": "stars", "order": "desc", "per_page": n},
                  headers=h)
        r.raise_for_status()
        for it in r.json().get("items", []):
            out.append({"source": "GitHub", "title": it["full_name"], "url": it["html_url"],
                        "summary": (it.get("description") or "")[:300], "score": it.get("stargazers_count", 0),
                        "tags": (it.get("topics") or [])[:6] + ([it["language"]] if it.get("language") else [])})
    seen, uniq = set(), []
    for x in sorted(out, key=lambda x: -x["score"]):
        if x["url"] not in seen:
            seen.add(x["url"])
            uniq.append(x)
    return uniq[:n]


def hf_trending(c: httpx.Client, n: int = 10) -> list[dict]:
    out = []
    for kind, base in (("models", "https://huggingface.co/"), ("spaces", "https://huggingface.co/spaces/")):
        r = c.get(f"https://huggingface.co/api/{kind}", params={"sort": "trendingScore", "limit": n})
        r.raise_for_status()
        for it in r.json():
            rid = it.get("id") or it.get("modelId")
            if not rid:
                continue
            out.append({"source": f"Hugging Face {kind}", "title": rid, "url": base + rid,
                        "summary": it.get("pipeline_tag") or (it.get("cardData") or {}).get("short_description") or "",
                        "score": it.get("trendingScore") or it.get("likes") or 0, "tags": (it.get("tags") or [])[:5]})
    return out


def show_hn(c: httpx.Client, days: int = 7, n: int = 15, min_points: int = 50) -> list[dict]:
    since = int(time.time() - days * 86400)
    r = c.get("https://hn.algolia.com/api/v1/search", params={"tags": "show_hn", "numericFilters": f"created_at_i>{since},points>{min_points}",
                                                               "hitsPerPage": n})
    r.raise_for_status()
    return [{"source": "Show HN", "title": h.get("title") or "", "url": h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}",
             "summary": f"{h.get('points', 0)} points, {h.get('num_comments', 0)} comments", "score": h.get("points", 0), "tags": []}
            for h in r.json().get("hits", [])]


def arxiv_recent(c: httpx.Client, cats: list[str], n: int = 12) -> list[dict]:
    if not cats:
        return []
    q = " OR ".join(f"cat:{x}" for x in cats)
    r = c.get("https://export.arxiv.org/api/query", params={"search_query": q, "sortBy": "submittedDate", "sortOrder": "descending",
                                                            "max_results": n})
    r.raise_for_status()
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out = []
    for e in ET.fromstring(r.content).findall("a:entry", ns):
        out.append({"source": "arXiv", "title": " ".join((e.findtext("a:title", "", ns)).split()),
                    "url": e.findtext("a:id", "", ns), "summary": " ".join((e.findtext("a:summary", "", ns)).split())[:300],
                    "score": 0, "tags": [t.get("term") for t in e.findall("a:category", ns)][:4]})
    return out


def collect(cfg: dict | None = None, transport=None) -> tuple[list[dict], list[str]]:
    """All signals, plus a list of sources that failed (reported, never hidden)."""
    cfg = cfg or {}
    c = _client(transport)
    items, errors = [], []
    jobs = [("GitHub", lambda: github_rising(c, cfg.get("days", 3), topics=cfg.get("github_topics"))),
            ("Hugging Face", lambda: hf_trending(c)),
            ("Show HN", lambda: show_hn(c, cfg.get("days", 3))),
            ("arXiv", lambda: arxiv_recent(c, cfg.get("arxiv_categories", ["cs.AI", "cs.CL", "cs.LG"])))]
    for name, fn in jobs:
        try:
            items += fn()
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {type(e).__name__} {str(e)[:120]}")
    return [i for i in items if safe(i)], errors
