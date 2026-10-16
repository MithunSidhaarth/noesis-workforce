"""NOESIS Knowledge Agent.

* Articles/pages: fetched (public, robots-respecting), summarized, written to
  02 Knowledge/<Domain>/ with the same frontmatter schema as noesis.py notes.
* Instagram / TikTok / YouTube links: these need a logged-in or media pipeline
  that already exists on your PC (noesis.py + faster-whisper). The agent writes
  a capture note to 01 Inbox instead, for that pipeline to process.
* Other bots can ask for context (INFORMATION_REQUEST / find_context); answers
  come from vault search, read-only.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from ..contracts import MessageType
from ..core.queue import Task
from ..core.settings import get_settings
from ..core.util import new_id, now, sha
from ..sources.parsing import domain, normalize_url
from .base import Agent, AgentContext, PermanentError, register, untrusted

SOCIAL_MEDIA = ("instagram.com", "tiktok.com", "youtube.com", "youtu.be", "x.com", "twitter.com", "facebook.com")
STOP = set("the a an and or of to in on for with is are was were be been this that it as at by from you your we our they "
           "their not but can will about into more most also have has had which what when how why who".split())
DOMAIN_MAP = {"ai": "AI", "machine learning": "AI", "llm": "AI", "agents": "AI", "programming": "Programming",
              "python": "Programming", "software": "Programming", "security": "Cybersecurity", "math": "Mathematics",
              "finance": "Finance", "science": "Science", "business": "Business", "startup": "Business", "career": "Business"}


def extract_article(html_bytes: bytes) -> dict:
    soup = BeautifulSoup(html_bytes, "html.parser")
    meta = lambda *names: next((m.get("content") for n in names for m in soup.find_all("meta", attrs={"property": n}) +
                                soup.find_all("meta", attrs={"name": n}) if m.get("content")), None)
    title = meta("og:title", "twitter:title") or (soup.title.get_text(strip=True) if soup.title else "")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form", "noscript"]):
        tag.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    paras = [p.get_text(" ", strip=True) for p in root.find_all(["p", "li", "h2", "h3"])]
    text = "\n".join(p for p in paras if len(p) > 30)
    return {"title": title.strip()[:200], "description": meta("og:description", "description"),
            "author": meta("author", "article:author"), "published": meta("article:published_time"),
            "site": meta("og:site_name"), "text": text[:60000]}


def keywords(text: str, n: int = 6) -> list[str]:
    words = [w for w in re.findall(r"[a-zA-Z][a-zA-Z\-]{3,}", text.lower()) if w not in STOP]
    return [w for w, _ in Counter(words).most_common(n)]


@register("knowledge")
class KnowledgeAgent(Agent):
    accepts = {"ingest_url", "find_context"}

    def handlers(self):
        return {"ingest_url": self.ingest_url, "find_context": self.find_context}

    def _vault(self, ctx: AgentContext):
        v = ctx.services.get("vault")
        if v is None:
            raise PermanentError("NOESIS vault is not configured on this server (NOESIS_VAULT_PATH)")
        return v

    def find_context(self, ctx: AgentContext, task: Task) -> dict:
        q = str(task.payload.get("query", ""))[:300]
        vault = ctx.services.get("vault")
        hits = vault.search(q, limit=5) if vault else []
        return {"query": q, "results": hits, "vault_available": vault is not None}

    def on_information_request(self, ctx: AgentContext, msg: dict) -> None:
        vault = ctx.services.get("vault")
        q = str(msg["payload"].get("query", ""))[:300]
        ctx.bus.reply(msg, ctx.bot.id, MessageType.INFORMATION_RESPONSE,
                      {"query": q, "results": vault.search(q, limit=5) if vault else [], "vault_available": vault is not None})

    def ingest_url(self, ctx: AgentContext, task: Task) -> dict:
        url = normalize_url(task.payload.get("url"))
        if not url:
            raise PermanentError("a valid http(s) URL is required")
        h = sha(url)
        existing = ctx.db.one("SELECT * FROM knowledge_captures WHERE url_hash=?", (h,))
        if existing and existing["status"] == "processed":
            return {"duplicate": True, "note": existing["note_path"]}
        cid = existing["id"] if existing else new_id("kn")
        if not existing:
            ctx.db.execute("INSERT INTO knowledge_captures(id, url, url_hash, status, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                           (cid, url, h, "captured", now(), now()))
        vault = self._vault(ctx)
        tz = get_settings().timezone
        stamp = datetime.now(ZoneInfo(tz))
        if any(domain(url).endswith(d) for d in SOCIAL_MEDIA):
            rel = f"01 Inbox/{stamp:%Y-%m-%d %H%M} capture-{h[:8]}.md"
            fm = {"id": cid, "title": f"Capture: {url}", "source_type": "reel" if "instagram" in url else "video",
                  "source_url": url, "captured_at": stamp.replace(microsecond=0).isoformat(), "processing_status": "inbox"}
            body = (f"# Capture\n\n{url}\n\nCaptured by the NOESIS Workforce. Social video needs your local NOESIS "
                    "pipeline (transcription), so it is queued here for `noesis.py` to process.")
            res = ctx.act("vault.write", f"vault:capture:{h}", lambda r: {**vault.upsert_note(f"capture:{h}", rel, fm, body, ctx.bot.id), "evidence": rel},
                          target=rel, wait_on_approval=False)
            path = (res.result or {}).get("path") or rel
            ctx.db.execute("UPDATE knowledge_captures SET status='processed', note_path=?, title=?, updated_at=? WHERE id=?",
                           (path, fm["title"], now(), cid))
            return {"capture": cid, "note": path, "mode": "inbox_for_local_pipeline"}

        fetcher = ctx.services.get("fetcher")
        if fetcher is None:
            raise PermanentError("no fetcher configured")
        page = fetcher.get(url)
        art = extract_article(page.content)
        if len(art["text"]) < 200:
            raise PermanentError("page has too little readable text to summarize (may need JS or login)")
        analysis = self._analyze(ctx, art)
        topics = analysis["topics"] or ["Other"]
        dom = next((DOMAIN_MAP[t.lower()] for t in topics if t.lower() in DOMAIN_MAP), "Other")
        slug = re.sub(r"[^a-z0-9]+", "-", "-".join(analysis["concepts"][:3]).lower()).strip("-") or "article"
        rel = f"02 Knowledge/{dom}/{stamp:%Y-%m-%d %H%M} {slug[:60]}.md"
        fm = {"id": cid, "title": art["title"] or url, "source_type": "article", "source_url": url,
              "creator": art["author"] or art["site"], "captured_at": stamp.replace(microsecond=0).isoformat(),
              "processed_at": stamp.replace(microsecond=0).isoformat(), "difficulty": None, "processing_status": "processed",
              "review_status": "unreviewed", "review_due_at": None, "review_count": None, "topics": topics,
              "concepts": analysis["concepts"], "summarized_by": analysis["method"]}
        body = "\n".join([
            f"# {fm['title']}", "", f"**Source:** [{url}]({url})  ", f"**Creator:** {fm['creator'] or 'unknown'}  ",
            f"**Captured:** {fm['captured_at']}  ", "**Status:** `processed` / review: `unreviewed`", "",
            "## TL;DR", "", analysis["tldr"], "", "## Key Concepts", "",
            *[f"- [[{c}]]" for c in analysis["concepts"]], "", "## Key Points", "",
            *[f"- {p}" for p in analysis["key_points"]], "", "## Claims To Verify", "",
            *([f"- {c}" for c in analysis["claims"]] or ["- none flagged"]), ""])
        res = ctx.act("vault.write", f"vault:kn:{h}", lambda r: {**vault.upsert_note(f"kn:{h}", rel, fm, body, ctx.bot.id), "evidence": rel},
                      target=rel, wait_on_approval=False)
        path = (res.result or {}).get("path") or rel
        ctx.db.execute("UPDATE knowledge_captures SET status='processed', note_path=?, title=?, summary=?, updated_at=? WHERE id=?",
                       (path, fm["title"], analysis["tldr"][:1000], now(), cid))
        return {"capture": cid, "note": path, "method": analysis["method"], "write": res.status}

    def _analyze(self, ctx: AgentContext, art: dict) -> dict:
        if ctx.has_model("summarize"):
            prompt = ("Analyze this article for a personal knowledge vault. Reply JSON with keys tldr (2-3 sentences), "
                      "key_points (5-8 strings), concepts (3-6 short noun phrases), topics (1-3 broad areas), "
                      "claims (factual claims worth verifying). Only use what the text says.\n" + untrusted(art["text"], 15000))
            try:
                d = ctx.llm("summarize", prompt, max_tokens=1200).json()
                return {"tldr": str(d.get("tldr", ""))[:1500], "key_points": [str(x)[:400] for x in d.get("key_points", [])][:10],
                        "concepts": [str(x)[:60] for x in d.get("concepts", [])][:6], "topics": [str(x)[:40] for x in d.get("topics", [])][:3],
                        "claims": [str(x)[:300] for x in d.get("claims", [])][:8], "method": "model"}
            except Exception as e:  # noqa: BLE001
                ctx.log(f"model summary failed, using extractive summary: {e}", level="warning")
        paras = [p for p in art["text"].split("\n") if len(p) > 60]
        kws = keywords(art["text"])
        return {"tldr": art["description"] or (paras[0][:400] if paras else ""),
                "key_points": [p[:300] for p in paras[1:7]], "concepts": [k.title() for k in kws[:5]],
                "topics": [k for k in kws if k in DOMAIN_MAP][:2], "claims": [], "method": "extractive (no model configured)"}
