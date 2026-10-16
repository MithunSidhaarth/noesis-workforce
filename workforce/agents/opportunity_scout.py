"""Opportunity Scout: discover -> normalize -> dedupe -> verify (Quality) ->
score -> hand to Applications -> notify -> NOESIS note."""
from __future__ import annotations

import difflib
from datetime import datetime
from zoneinfo import ZoneInfo

from ..contracts import MessageType
from ..core.db import dumps, loads
from ..core.queue import Task
from ..core.settings import get_settings
from ..core.util import iso, new_id, now
from ..sources import parsing
from ..sources.registry import load_profile
from .base import Agent, AgentContext, PermanentError, register, untrusted

LLM_FIELDS = ("organizer", "location", "eligibility", "required_materials", "estimated_effort")


@register("opportunity_scout")
class OpportunityScout(Agent):
    accepts = {"check_source", "find_opportunities"}

    def handlers(self):
        return {
            "sweep_sources": self.sweep_sources,
            "check_source": self.check_source,
            "find_opportunities": self.sweep_sources,
            "process_verification": self.process_verification,
        }

    # ---- discovery -------------------------------------------------------
    def sweep_sources(self, ctx: AgentContext, task: Task) -> dict:
        t = now()
        due = ctx.db.all("SELECT id, next_check FROM sources WHERE enabled=1 AND (next_check IS NULL OR next_check<=?)", (t,))
        queued = []
        for s in due:
            slot = int(s["next_check"] or t)
            tid, created = ctx.enqueue_self("check_source", {"source_id": s["id"]}, idempotency_key=f"src:{s['id']}:{slot}")
            if created:
                queued.append(s["id"])
        return {"sources_due": len(due), "queued": queued}

    def check_source(self, ctx: AgentContext, task: Task) -> dict:
        src = ctx.db.one("SELECT * FROM sources WHERE id=?", (task.payload["source_id"],))
        if not src:
            raise PermanentError(f"unknown source {task.payload['source_id']}")
        fetcher = ctx.services.get("fetcher")
        t = now()
        try:
            if src["kind"] == "fixture":
                from pathlib import Path
                content, etag, lm, not_modified, base = Path(src["url"]).read_bytes(), None, None, False, "https://fixture.local/"
            else:
                if fetcher is None:
                    raise PermanentError("no fetcher configured")
                r = fetcher.get(src["url"], src["etag"], src["last_modified"])
                content, etag, lm, not_modified, base = r.content, r.etag, r.last_modified, r.not_modified, r.url
            parser_cfg = loads(src["parser_json"], {})
            items = []
            if not not_modified:
                kind = src["kind"] if src["kind"] != "fixture" else parser_cfg.get("format", "rss")
                if kind == "rss":
                    items = parsing.parse_feed(content, base)
                elif kind == "html":
                    items = parsing.parse_html(content, base, parser_cfg)
                elif kind == "json":
                    items = parsing.parse_json(content, base, parser_cfg)
        except Exception as e:
            fails = src["consecutive_failures"] + 1
            ctx.db.execute("UPDATE sources SET last_checked=?, last_error=?, consecutive_failures=?, reliability=?, next_check=? WHERE id=?",
                           (t, str(e)[:500], fails, "failing" if fails >= 3 else "degraded",
                            t + min(src["interval_s"] * (2 ** min(fails, 4)), 7 * 86400), src["id"]))
            if fails == 3:
                ctx.notify_owner(f"⚠️ Source '{src['name']}' has failed 3 times in a row: {str(e)[:200]}", key=f"srcfail:{src['id']}:{int(t // 86400)}")
            # The source's own backoff (next_check) is the retry mechanism; retrying the task
            # immediately would hammer the host and count one outage as several failures.
            raise PermanentError(f"source fetch failed (next attempt per source backoff): {e}") from e

        new_ids = []
        limit = ctx.bot.settings.get("max_items_per_source", 50)
        for item in items[:limit]:
            opp = parsing.normalize_item(item, {**dict(src), **parser_cfg}, get_settings().timezone)
            if not opp:
                continue
            oid = self._store(ctx, opp, src)
            if oid:
                new_ids.append(oid)
        ctx.db.execute(
            "UPDATE sources SET last_checked=?, last_success=?, last_error=NULL, consecutive_failures=0, reliability='ok', "
            "etag=COALESCE(?, etag), last_modified=COALESCE(?, last_modified), next_check=?, items_seen=items_seen+? WHERE id=?",
            (t, t, etag, lm, t + src["interval_s"], len(items), src["id"]))
        for oid in new_ids:
            ctx.send(MessageType.VERIFICATION_REQUEST, {"kind": "opportunity", "opportunity_id": oid},
                     recipient="quality", dedupe_key=f"verify:{oid}", response_type=MessageType.VERIFICATION_RESULT)
        return {"source": src["id"], "items": len(items), "new": len(new_ids), "not_modified": not_modified}

    def _is_duplicate(self, ctx: AgentContext, opp: dict, fp: str) -> bool:
        if ctx.db.scalar("SELECT 1 FROM opportunities WHERE fingerprint=?", (fp,)):
            return True
        norm = parsing.normalize_title(opp["title"])
        # compare across types: the same event is often labelled differently by different sources
        recent = ctx.db.all("SELECT title FROM opportunities WHERE discovered_at > ?", (now() - 120 * 86400,))
        return any(difflib.SequenceMatcher(None, norm, parsing.normalize_title(r["title"])).ratio() > 0.92 for r in recent)

    def _store(self, ctx: AgentContext, opp: dict, src: dict) -> str | None:
        fp = parsing.fingerprint(opp)
        if self._is_duplicate(ctx, opp, fp):
            return None
        if ctx.has_model("extract"):
            opp = self._enrich(ctx, opp)
        oid = new_id("opp")
        try:
            ctx.db.execute(
                "INSERT INTO opportunities(id, fingerprint, title, organizer, official_url, discovery_source, discovery_url, type, location, "
                "remote_status, deadline, deadline_text, deadline_tz, eligibility_json, required_materials_json, estimated_effort, summary, "
                "discovered_at, raw_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (oid, fp, opp["title"], opp.get("organizer"), opp.get("official_url"), src["id"], opp.get("discovery_url"),
                 opp["type"], opp.get("location"), opp["remote_status"], opp.get("deadline"), opp.get("deadline_text"),
                 opp.get("deadline_tz"), dumps(opp.get("eligibility", [])), dumps(opp.get("required_materials", [])),
                 opp.get("estimated_effort"), opp.get("summary"), now(), dumps({"raw_text": opp.get("raw_text", "")[:8000],
                                                       "candidate_links": opp.get("candidate_links", [])})))
        except Exception as e:
            if "UNIQUE" in str(e):
                return None
            raise
        return oid

    def _enrich(self, ctx: AgentContext, opp: dict) -> dict:
        """LLM extraction, accepted only where the value is grounded in the source text."""
        prompt = ("Extract fields from this opportunity listing as JSON with keys organizer, location, eligibility (list of "
                  "strings copied verbatim), required_materials (list), estimated_effort (low|medium|high). Use null when "
                  "the text does not state it. Do not infer.\n" + untrusted(opp.get("raw_text", ""), 6000))
        try:
            data = ctx.llm("extract", prompt, max_tokens=600).json()
        except Exception as e:  # model optional: keep deterministic result
            ctx.log(f"extraction model unavailable, using deterministic fields: {e}", level="debug")
            return opp
        raw = (opp.get("raw_text") or "").lower()
        out = dict(opp)
        for k in LLM_FIELDS:
            v = data.get(k)
            if v in (None, "", []):
                continue
            if k == "estimated_effort":
                if v in ("low", "medium", "high"):
                    out[k] = v
            elif isinstance(v, list):
                grounded = [x for x in v if isinstance(x, str) and x.lower()[:40] in raw]
                if grounded:
                    out[k] = grounded
            elif isinstance(v, str) and v.lower() in raw and not out.get(k):
                out[k] = v
        return out

    # ---- verification results -------------------------------------------
    def on_verification_result(self, ctx: AgentContext, msg: dict) -> None:
        oid = msg["payload"].get("opportunity_id")
        if not oid:
            return
        ctx.enqueue_self("process_verification", {"opportunity_id": oid, "result": msg["payload"]},
                         idempotency_key=f"procver:{msg['id']}", correlation_id=msg.get("correlation_id"))

    def process_verification(self, ctx: AgentContext, task: Task) -> dict:
        oid = task.payload["opportunity_id"]
        res = task.payload["result"]
        opp = ctx.db.one("SELECT * FROM opportunities WHERE id=?", (oid,))
        if not opp:
            raise PermanentError(f"opportunity {oid} vanished")
        status = res.get("status", "needs_review")
        score, rationale = score_opportunity(opp, load_profile())
        ctx.db.execute("UPDATE opportunities SET verification_status=?, verification_notes_json=?, last_verified=?, relevance_score=?, "
                       "relevance_rationale=?, application_status=? WHERE id=?",
                       (status, dumps(res.get("notes", [])), now(), score, rationale,
                        "VERIFIED" if status == "verified" else opp["application_status"], oid))
        opp = ctx.db.one("SELECT * FROM opportunities WHERE id=?", (oid,))
        note = self._write_note(ctx, opp)
        if status == "failed":
            return {"opportunity_id": oid, "status": status, "note": note}
        if status == "verified":
            ctx.delegate("applications", "evaluate_opportunity", {"opportunity_id": oid}, dedupe_key=f"eval:{oid}")
            ctx.send(MessageType.RESEARCH_FINDING, {"opportunity_id": oid, "title": opp["title"], "score": score},
                     topic="opportunity.verified", dedupe_key=f"found:{oid}")
        notified = False
        if score >= ctx.bot.settings.get("notify_min_score", 0.7) and not opp["notified"]:
            from ..owner_actions import opportunity_card, schedule_deadline_reminders
            text, buttons = opportunity_card(opp, short=True)
            # only a verified match closing within 72h interrupts you; the rest waits for the morning brief
            urgent = status == "verified" and bool(opp["deadline"]) and opp["deadline"] - now() < 72 * 3600
            ctx.notify_owner(text, key=f"opp:{oid}", urgent=urgent, buttons=buttons)
            notified = True   # stored: delivered now or listed in the next brief
            ctx.db.execute("UPDATE opportunities SET notified=1 WHERE id=?", (oid,))
            schedule_deadline_reminders(ctx.db, opp, ctx.bot.id)
        return {"opportunity_id": oid, "status": status, "score": score, "notified": notified, "note": note}

    def _write_note(self, ctx: AgentContext, opp: dict) -> str | None:
        vault = ctx.services.get("vault")
        if vault is None or "vault.write" not in ctx.bot.tools:
            return None
        rel = f"09 Opportunities/{datetime.fromtimestamp(opp['discovered_at'], ZoneInfo(get_settings().timezone)):%Y-%m} {_slug(opp['title'])}.md"
        fm, body = opportunity_note(opp)
        res = ctx.act("vault.write", f"vault:opp:{opp['id']}:{opp['verification_status']}",
                      lambda r: {**vault.upsert_note(f"opp:{opp['id']}", rel, fm, body, ctx.bot.id), "evidence": rel},
                      target=rel, params={"path": rel}, summary=f"NOESIS note for {opp['title'][:60]}", wait_on_approval=False)
        path = (res.result or {}).get("path") if res.status == "succeeded" else None
        if path:
            ctx.db.execute("UPDATE opportunities SET note_path=? WHERE id=?", (path, opp["id"]))
        return path


def _slug(t: str) -> str:
    from integrations.obsidian.vault import slugify
    return slugify(t, 60)


def score_opportunity(opp: dict, profile: dict) -> tuple[float, str]:
    """Deterministic relevance score in [0, 1] with a readable rationale."""
    text = f"{opp['title']} {opp.get('summary') or ''} {' '.join(loads(opp.get('eligibility_json'), []))}".lower()
    reasons, score = [], 0.0
    if opp["type"] in profile.get("preferred_types", []):
        score += 0.3
        reasons.append(f"type {opp['type']} is preferred")
    hits = [i for i in profile.get("interests", []) if i.lower() in text]
    if hits:
        score += min(0.4, 0.12 * len(hits))
        reasons.append(f"matches interests: {', '.join(hits[:5])}")
    locs = [loc for loc in profile.get("preferred_locations", []) if loc.lower() in text]
    if locs or opp.get("remote_status") in ("remote", "hybrid"):
        score += 0.15
        reasons.append("location fits" if locs else f"{opp['remote_status']} participation")
    if opp.get("deadline"):
        days = (opp["deadline"] - now()) / 86400
        if days < 0:
            score = 0.0
            reasons.append("deadline has passed")
        elif days <= 30:
            score += 0.15
            reasons.append(f"deadline in {int(days)} days")
    else:
        reasons.append("deadline unknown")
    bad = [k for k in profile.get("exclude_keywords", []) if k.lower() in text]
    if bad:
        score *= 0.3
        reasons.append(f"contains excluded terms: {', '.join(bad)}")
    if opp.get("verification_status") == "failed":
        score *= 0.2
    return round(min(score, 1.0), 2), "; ".join(reasons)


def opportunity_note(opp: dict) -> tuple[dict, str]:
    tz = get_settings().timezone
    elig = loads(opp.get("eligibility_json"), [])
    mats = loads(opp.get("required_materials_json"), [])
    notes = loads(opp.get("verification_notes_json"), [])
    fm = {
        "title": opp["title"], "type": "opportunity", "opportunity_type": opp["type"], "organizer": opp.get("organizer"),
        "official_url": opp.get("official_url"), "discovery_source": opp["discovery_source"],
        "deadline": iso(opp.get("deadline"), tz), "deadline_tz": opp.get("deadline_tz"),
        "remote_status": opp.get("remote_status"), "verification_status": opp["verification_status"],
        "application_status": opp["application_status"], "relevance_score": opp.get("relevance_score"),
        "discovered_at": iso(opp["discovered_at"], tz), "last_verified": iso(opp.get("last_verified"), tz),
        "tags": ["opportunity", opp["type"]],
    }
    unknown = "_Unknown — not stated in the source._"
    body = [f"# {opp['title']}", "",
            f"**Type:** {opp['type']} · **Status:** `{opp['verification_status']}` / `{opp['application_status']}`",
            f"**Official URL:** {opp.get('official_url') or unknown}",
            f"**Deadline:** {(fm['deadline'] + ' (' + str(opp.get('deadline_text')) + ')') if opp.get('deadline') else unknown}",
            f"**Organizer:** {opp.get('organizer') or unknown}",
            f"**Mode:** {opp.get('remote_status')}", "",
            "## Summary", opp.get("summary") or unknown, "",
            "## Eligibility (as stated)", *([f"- {e}" for e in elig] or [unknown]), "",
            "## Required materials", *([f"- {m}" for m in mats] or [unknown]), "",
            "## Relevance", f"{opp.get('relevance_score')} — {opp.get('relevance_rationale') or ''}", "",
            "## Verification notes", *([f"- {n}" for n in notes] or ["- none"]), "",
            "_Generated by the NOESIS Workforce Opportunity Scout. Database is the source of truth for status._"]
    return fm, "\n".join(body)
