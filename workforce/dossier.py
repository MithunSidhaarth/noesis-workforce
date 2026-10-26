"""Company dossier: a one-screen brief before an OA or interview.

Sources, each labelled in the brief:
  * your own data: listings from this company, where each stands, ghost risk, your
    referrals and the questions you logged after earlier rounds there;
  * Wikipedia's public REST summary (what the company does);
  * Google News RSS headlines from the last weeks (public feed);
  * a model summary of those, marked as such, with talking points and questions to ask.
External text is data for the model, never instructions. Nothing here logs in anywhere.
"""
from __future__ import annotations

import json
from urllib.parse import quote, quote_plus

from .core.util import now
from .owner_actions import clip, day_label

CACHE_S = 3 * 86400


def own_data(db, company: str) -> dict:
    like = company.strip().lower()
    opps = db.all("SELECT id, title, stage, deadline, ghost_risk, relevance_score FROM opportunities WHERE lower(organizer)=? "
                  "ORDER BY discovered_at DESC LIMIT 8", (like,))
    qs = db.all("SELECT question, stage, topic FROM interview_questions WHERE lower(company)=? ORDER BY asked_at DESC LIMIT 6", (like,))
    refs = db.all("SELECT name, status FROM referrals WHERE lower(company)=? LIMIT 6", (like,))
    return {"listings": opps, "questions": qs, "referrals": refs}


def wikipedia(fetcher, company: str) -> str | None:
    if fetcher is None:
        return None
    try:
        r = fetcher.get(f"https://en.wikipedia.org/api/rest_v1/page/summary/{quote(company.strip().replace(' ', '_'))}",
                        max_bytes=200_000)
        data = json.loads(r.text)
        if data.get("type") == "disambiguation":
            return None
        return (data.get("extract") or "")[:900] or None
    except Exception:  # noqa: BLE001  (missing page, robots, offline: the brief just goes without it)
        return None


def news(fetcher, company: str, limit: int = 5) -> list[dict]:
    if fetcher is None:
        return []
    from .sources.parsing import parse_feed
    try:
        url = f"https://news.google.com/rss/search?q={quote_plus(chr(34) + company + chr(34))}+when:30d&hl=en-IN&gl=IN&ceid=IN:en"
        r = fetcher.get(url, max_bytes=1_000_000)
        items = parse_feed(r.content, url)
    except Exception:  # noqa: BLE001
        return []
    return [{"title": i["title"][:160], "url": i["url"], "published": i.get("published")} for i in items[:limit]]


def build_dossier(ctx, company: str, use_cache: bool = True) -> dict:
    from .agents.base import untrusted
    company = (company or "").strip()[:80]
    key = f"dossier:{company.lower()}"
    cached = ctx.memory_get(key) if use_cache else None
    if cached and now() - cached.get("at", 0) < CACHE_S:
        return cached
    fetcher = ctx.services.get("fetcher")
    mine, wiki, headlines = own_data(ctx.db, company), wikipedia(fetcher, company), news(fetcher, company)
    summary = None
    if ctx.has_model("summarize"):
        prompt = ("Write a short interview brief about this company for a student interviewing for an internship in Bengaluru. "
                  'Reply JSON: {"what_they_do": "<1-2 sentences>", "recent": ["<up to 3 points from the headlines>"], '
                  '"talking_points": ["<3 points to mention>"], "questions_to_ask": ["<3 questions>"]}. Use only the '
                  "material below and general public knowledge; say 'unknown' rather than guess.\n"
                  + untrusted(json.dumps({"company": company, "wikipedia": wiki, "headlines": [h["title"] for h in headlines]}), 4000))
        try:
            data = ctx.llm("summarize", prompt, max_tokens=500).json()
            summary = data if isinstance(data, dict) else None
        except Exception as e:  # noqa: BLE001
            ctx.log(f"dossier summary unavailable: {e}", level="debug")
    out = {"company": company, "own": mine, "wikipedia": wiki, "news": headlines, "summary": summary, "at": now(),
           "text": render(company, mine, wiki, headlines, summary)}
    ctx.memory_set(key, out)
    return out


def _lst(summary: dict | None, k: str) -> list[str]:
    v = (summary or {}).get(k) or []
    return [str(x)[:200] for x in v if isinstance(x, (str, int, float))][:3] if isinstance(v, list) else []


def render(company: str, mine: dict, wiki: str | None, headlines: list[dict], summary: dict | None) -> str:
    from .pipeline import LABELS
    lines = [f"🏢 {company}: interview brief", ""]
    what = (summary or {}).get("what_they_do")
    if what and str(what).lower() != "unknown":
        lines += [str(what)[:300], ""]
    elif wiki:
        lines += [clip(wiki, 300), ""]
    if mine["listings"]:
        lines.append("Your listings here:")
        for o in mine["listings"][:4]:
            ghost = " ⚠️ possible ghost listing" if (o.get("ghost_risk") or 0) >= 0.5 else ""
            lines.append(f"• {clip(o['title'], 60)}: {LABELS.get(o['stage'], o['stage'])}{ghost}")
        lines.append("")
    if mine["questions"]:
        lines.append("Asked in your earlier rounds:")
        lines += [f"• {clip(q['question'], 100)}" for q in mine["questions"][:4]]
        lines.append("")
    if mine["referrals"]:
        lines.append("Referrals: " + ", ".join(f"{r['name']} ({r['status'].replace('_', ' ')})" for r in mine["referrals"]))
        lines.append("")
    recent = _lst(summary, "recent")
    if recent:
        lines += ["Recent (from news headlines):"] + [f"• {p}" for p in recent] + [""]
    elif headlines:
        lines += ["Recent headlines:"] + [f"• {clip(h['title'], 100)} ({day_label(h['published'])})" if h.get("published")
                                          else f"• {clip(h['title'], 100)}" for h in headlines[:3]] + [""]
    tp, qa = _lst(summary, "talking_points"), _lst(summary, "questions_to_ask")
    if tp:
        lines += ["Talking points:"] + [f"• {p}" for p in tp] + [""]
    if qa:
        lines += ["Questions to ask them:"] + [f"• {p}" for p in qa] + [""]
    src = ["your NOESIS data"] + (["Wikipedia"] if wiki else []) + (["Google News"] if headlines else []) + \
          (["a model summary (check it)"] if summary else [])
    lines.append("Sources: " + ", ".join(src) + ".")
    return "\n".join(lines).strip()
