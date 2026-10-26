"""Quality & Verification agent. Deterministic checks first; a model is only
used (when configured) to judge content tone/claims and never overrides a
failed deterministic check."""
from __future__ import annotations

import difflib
import re

from ..contracts import MessageType
from ..core.db import loads
from ..core.queue import Task
from ..core.util import now
from ..sources import parsing
from .base import Agent, AgentContext, PermanentError, register

AGGREGATOR_HINT = "listing comes from an aggregator; official organizer page not confirmed"
SENSITIVE_PATTERNS = [r"\bguarantee[sd]?\b", r"\b100%\b", r"\bcure[sd]?\b", r"\blegal advice\b", r"\binvest(ment)? returns?\b",
                      r"\bpaid partnership\b", r"#ad\b", r"\bsponsored\b"]


@register("quality")
class QualityAgent(Agent):
    accepts = {"verify"}

    def handlers(self):
        return {"verify": self.verify}

    def on_verification_request(self, ctx: AgentContext, msg: dict) -> None:
        ctx.enqueue_self("verify", {**msg["payload"], "_request_msg": msg["id"]}, idempotency_key=f"verify:{msg['id']}",
                         correlation_id=msg.get("correlation_id"))

    def verify(self, ctx: AgentContext, task: Task) -> dict:
        kind = task.payload.get("kind")
        fn = {"opportunity": self.verify_opportunity, "content": self.verify_content}.get(kind)
        if fn is None:
            raise PermanentError(f"unknown verification kind {kind}")
        result = fn(ctx, task.payload)
        req_id = task.payload.get("_request_msg")
        if req_id:
            original = ctx.db.one("SELECT * FROM messages WHERE id=?", (req_id,))
            if original:
                ctx.bus.reply(original, ctx.bot.id, MessageType.VERIFICATION_RESULT, result)
        return result

    # ---- opportunities ---------------------------------------------------
    def verify_opportunity(self, ctx: AgentContext, p: dict) -> dict:
        opp = ctx.db.one("SELECT * FROM opportunities WHERE id=?", (p["opportunity_id"],))
        if not opp:
            raise PermanentError("opportunity not found")
        src = ctx.db.one("SELECT * FROM sources WHERE id=?", (opp["discovery_source"],)) or {}
        notes, failed, review = [], False, False
        url = opp["official_url"]
        if not url:
            notes.append("no application/official URL in the listing")
            review = True
        elif not url.startswith("https://"):
            notes.append("URL is not HTTPS")
            review = True
        else:
            official = loads(src.get("official_domains_json"), [])
            d = parsing.domain(url)
            if official and any(d == o or d.endswith("." + o) for o in official):
                notes.append(f"URL is on an official domain ({d})")
            else:
                notes.append(AGGREGATOR_HINT + f" ({d})")
                review = True
                cands = loads(opp.get("raw_json"), {}).get("candidate_links", [])
                if cands:
                    notes.append(f"possible official page (unconfirmed): {cands[0]}")
            if ctx.bot.settings.get("check_url_reachable") and ctx.services.get("fetcher") and src.get("kind") != "fixture":
                ok, detail = ctx.services["fetcher"].head_ok(url)
                notes.append(f"reachability: {detail}")
                if not ok:
                    failed = True
        rolling = bool(loads(src.get("parser_json"), {}).get("rolling")) and url and "aggregator" not in " ".join(notes)
        if opp["deadline"] is None and rolling:
            notes.append("rolling posting on the company's official job board (no deadline stated; apply early)")
        elif opp["deadline"] is None:
            notes.append("deadline not stated in the source; left unknown")
            review = True
        elif opp["deadline"] < now():
            notes.append("deadline has already passed")
            failed = True
        elif opp["deadline"] > now() + 2 * 365 * 86400:
            notes.append("deadline more than 2 years away; likely a parsing error")
            review = True
        else:
            notes.append(f"deadline taken from source text: \"{opp['deadline_text']}\"")
        # duplicate of an already verified opportunity?
        norm = parsing.normalize_title(opp["title"])
        org = (opp.get("organizer") or "").strip().lower()
        for r in ctx.db.all("SELECT id, title, organizer FROM opportunities WHERE id!=? AND verification_status='verified'", (opp["id"],)):
            if org and r["organizer"] and r["organizer"].strip().lower() != org:
                continue
            if difflib.SequenceMatcher(None, norm, parsing.normalize_title(r["title"])).ratio() > 0.92:
                notes.append(f"possible duplicate of {r['id']}")
                review = True
                break
        status = "failed" if failed else ("needs_review" if review else "verified")
        return {"kind": "opportunity", "opportunity_id": opp["id"], "status": status, "notes": notes}

    # ---- content ----------------------------------------------------------
    def verify_content(self, ctx: AgentContext, p: dict) -> dict:
        item = ctx.db.one("SELECT * FROM content_items WHERE id=?", (p["content_id"],))
        if not item:
            raise PermanentError("content item not found")
        body = item["body"] or ""
        notes, problems, flags = [], [], []
        limits = {"linkedin": 3000, "instagram": 2200}
        if not body.strip():
            problems.append("empty body")
        if len(body) > limits.get(item["platform"], 3000):
            problems.append(f"body exceeds {item['platform']} limit ({len(body)} chars)")
        claims = loads(item["claims_json"], [])
        sources = loads(item["sources_json"], [])
        unsupported = [c for c in claims if not c.get("source")]
        if unsupported:
            problems.append(f"{len(unsupported)} claim(s) without a source")
        for pat in SENSITIVE_PATTERNS:
            if re.search(pat, body, re.IGNORECASE):
                flags.append(f"sensitive wording: {pat}")
        assets = loads(item["assets_json"], [])
        unlicensed = [a for a in assets if not a.get("license")]
        if unlicensed:
            problems.append(f"{len(unlicensed)} asset(s) without license info")
        media_bad = [a for a in assets if a.get("kind") == "video" and not a.get("validated")]
        if media_bad:
            problems.append("video not validated")
        if item["content_hash"]:
            dup = ctx.db.one("SELECT id FROM content_items WHERE content_hash=? AND id!=? AND state IN ('PUBLISHED','SCHEDULED')",
                             (item["content_hash"], item["id"]))
            if dup:
                problems.append(f"duplicate of {dup['id']}")
        notes.append(f"{len(claims)} claim(s), {len(sources)} source(s), {len(assets)} asset(s)")
        status = "failed" if problems else ("needs_review" if flags else "verified")
        return {"kind": "content", "content_id": item["id"], "status": status, "notes": notes + problems + flags,
                "facts_verified": not unsupported, "licenses_ok": not unlicensed, "media_valid": not media_bad,
                "sensitive": bool(flags)}
