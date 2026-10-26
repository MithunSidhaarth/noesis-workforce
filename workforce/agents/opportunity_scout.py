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
    accepts = {"check_source", "find_opportunities", "ingest_link"}

    def handlers(self):
        return {
            "sweep_sources": self.sweep_sources,
            "check_source": self.check_source,
            "find_opportunities": self.sweep_sources,
            "process_verification": self.process_verification,
            "ingest_link": self.ingest_link,
            "drive_radar": self.drive_radar,
        }

    def drive_radar(self, ctx: AgentContext, task: Task) -> dict:
        from ..drives import radar
        return radar(ctx)

    # ---- a link the owner sent (Internshala, Unstop, Wellfound, a careers page...) ----
    def ingest_link(self, ctx: AgentContext, task: Task) -> dict:
        """One page, fetched once and politely (robots.txt honoured), or the text the owner
        pasted. LinkedIn is never fetched: the owner pastes the description instead."""
        from ..owner_actions import clip, ref
        url = parsing.normalize_url(task.payload.get("url"))
        pasted = str(task.payload.get("text") or "")[:20000]
        if not url:
            raise PermanentError("not a valid link")
        title, body = "", pasted
        if not pasted:
            fetcher = ctx.services.get("fetcher")
            if fetcher is None:
                raise PermanentError("no fetcher configured")
            try:
                page = fetcher.get(url)
            except Exception as e:  # noqa: BLE001  (robots.txt refusal, 403, timeout: tell the owner, don't retry around it)
                ctx.notify_owner(f"⚠️ I couldn't open that link\n\n{url}\n{str(e)[:200]}\n\nPaste the posting's text instead: "
                                 "/add <link> <the description>", key=f"linkfail:{task.id}", force_now=True)
                return {"url": url, "error": str(e)[:200]}
            title, body = page_title_and_text(page.text)
        item = {"title": clip(title or pasted.split("\n", 1)[0], 200), "url": url, "body": body[:12000], "links": [],
                "tags": []}
        fields = self._extract_link_fields(ctx, item) if ctx.has_model("extract") else {}
        if fields.get("title"):
            item["title"] = fields["title"]
        if fields.get("organizer"):
            item["author"] = fields["organizer"]
        if fields.get("location"):
            item["location"] = fields["location"]
        if fields.get("deadline"):
            item["structured_deadline"] = fields["deadline"]
        src = ctx.db.one("SELECT * FROM sources WHERE id='owner_links'") or {"id": "owner_links", "default_type": "internship"}
        opp = parsing.normalize_item(item, {**dict(src), "author_is_organizer": True, "default_type": "internship"},
                                     get_settings().timezone)
        if not opp:
            ctx.notify_owner(f"🤔 I couldn't find a posting on that page\n\n{url}\nPaste its text: /add <link> <the description>",
                             key=f"linkempty:{task.id}", force_now=True)
            return {"url": url, "stored": False}
        if fields.get("stipend") and fields["stipend"].lower()[:12] in (body or "").lower():
            opp["stipend"] = fields["stipend"]
        oid = self._store(ctx, opp, src)
        if oid is None:
            existing = ctx.db.one("SELECT id, title FROM opportunities WHERE fingerprint=?", (parsing.fingerprint(opp),))
            ctx.notify_owner(f"📌 Already tracking that one\n\n{clip(existing['title'] if existing else opp['title'], 120)}"
                             + (f"\nref {ref(existing['id'])}" if existing else ""), key=f"linkdup:{task.id}", force_now=True)
            return {"url": url, "duplicate": True}
        from ..pipeline import set_stage
        set_stage(ctx.db, oid, "shortlisted", by="owner", note="you sent the link")
        ctx.send(MessageType.VERIFICATION_REQUEST, {"kind": "opportunity", "opportunity_id": oid},
                 recipient="quality", dedupe_key=f"verify:{oid}", response_type=MessageType.VERIFICATION_RESULT)
        ctx.notify_owner(f"📌 Added to your shortlist\n\n{clip(opp['title'], 150)}\n{opp.get('organizer') or ''}\n"
                         f"I'm checking it and scoring the fit now. Send /opp {ref(oid)} for details.\nref {ref(oid)}",
                         key=f"linkadded:{oid}", force_now=True)
        return {"url": url, "opportunity_id": oid}

    def _extract_link_fields(self, ctx: AgentContext, item: dict) -> dict:
        prompt = ("This is one job or internship posting page. Return JSON with keys title (the role), organizer (the "
                  "company), location, deadline (as written, or null), stipend (as written, or null). Copy values from the "
                  "text; use null when it is not stated.\n" + untrusted(f"{item['title']}\n{item['body']}", 8000))
        try:
            data = ctx.llm("extract", prompt, max_tokens=300).json()
        except Exception as e:  # noqa: BLE001
            ctx.log(f"link extraction unavailable: {e}", level="debug")
            return {}
        raw = f"{item['title']} {item['body']}".lower()
        out = {}
        for k in ("title", "organizer", "location", "deadline", "stipend"):
            v = data.get(k) if isinstance(data, dict) else None
            if isinstance(v, str) and v.strip() and (k == "title" or v.strip().lower()[:30] in raw):
                out[k] = v.strip()[:200]
        return out

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
                pcfg = loads(src["parser_json"], {})
                r = fetcher.get(src["url"], src["etag"], src["last_modified"], max_bytes=pcfg.get("max_bytes"))
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
                items = [i for i in items if parsing.keep_item(i, parser_cfg)]
        except Exception as e:
            fails = src["consecutive_failures"] + 1
            ctx.db.execute("UPDATE sources SET last_checked=?, last_error=?, consecutive_failures=?, reliability=?, next_check=? WHERE id=?",
                           (t, str(e)[:500], fails, "failing" if fails >= 3 else "degraded",
                            t + min(src["interval_s"] * (2 ** min(fails, 4)), 7 * 86400), src["id"]))
            if fails == 3:
                ctx.notify_owner(f"⚠️ An opportunity source keeps failing\n\n“{src['name']}” has failed 3 times in a row. "
                                 f"Last error: {str(e)[:200]}\nI'll keep retrying it, less often. If it stays broken, "
                                 "disable it in the dashboard.", key=f"srcfail:{src['id']}:{int(t // 86400)}")
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
            ctx.db.execute("UPDATE opportunities SET last_seen=? WHERE fingerprint=?", (now(), fp))
            return True
        norm = parsing.normalize_title(opp["title"])
        # compare across types: the same event is often labelled differently by different sources.
        # Job titles repeat across companies ("Software Engineering Intern"), so when the organiser
        # is known only listings from the same organiser (or with none recorded) can be duplicates.
        org = (opp.get("organizer") or "").strip().lower()
        recent = ctx.db.all("SELECT id, title, organizer FROM opportunities WHERE discovered_at > ?", (now() - 180 * 86400,))
        for r in recent:
            if org and r["organizer"] and r["organizer"].strip().lower() != org:
                continue
            if difflib.SequenceMatcher(None, norm, parsing.normalize_title(r["title"])).ratio() > 0.92:
                if org and r["organizer"]:
                    # the same company posting the same role under a new link: a repost (ghost-listing signal)
                    ctx.db.execute("UPDATE opportunities SET reposts=reposts+1, last_seen=? WHERE id=?", (now(), r["id"]))
                return True
        return False

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
                "discovered_at, raw_json, stipend, published_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (oid, fp, opp["title"], opp.get("organizer"), opp.get("official_url"), src["id"], opp.get("discovery_url"),
                 opp["type"], opp.get("location"), opp["remote_status"], opp.get("deadline"), opp.get("deadline_text"),
                 opp.get("deadline_tz"), dumps(opp.get("eligibility", [])), dumps(opp.get("required_materials", [])),
                 opp.get("estimated_effort"), opp.get("summary"), now(), dumps({"raw_text": opp.get("raw_text", "")[:8000],
                                                       "candidate_links": opp.get("candidate_links", [])}),
                 opp.get("stipend"), opp.get("published")))
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
        profile = load_profile()
        score, rationale = score_opportunity(opp, profile)
        verdict = self._ai_fit(ctx, opp, profile) if status != "failed" and ctx.has_model("classify") else None
        if verdict:
            score, rationale = combine_scores(score, rationale, verdict)
            ctx.db.execute("UPDATE opportunities SET ai_fit=?, ai_reasons_json=?, ai_red_flags_json=?, ai_eligible=? WHERE id=?",
                           (verdict["fit"], dumps(verdict["reasons"]), dumps(verdict["red_flags"]), verdict["eligible"], oid))
        ctx.db.execute("UPDATE opportunities SET verification_status=?, verification_notes_json=?, last_verified=?, relevance_score=?, "
                       "relevance_rationale=?, application_status=? WHERE id=?",
                       (status, dumps(res.get("notes", [])), now(), score, rationale,
                        "VERIFIED" if status == "verified" else opp["application_status"], oid))
        from ..ghosts import assess_ghost_risk
        risk, why = assess_ghost_risk(ctx.db, oid)
        if risk >= 0.3:   # still shown, ranked lower, with the reason
            score = round(score * (1 - 0.5 * risk), 2)
            ctx.db.execute("UPDATE opportunities SET relevance_score=?, relevance_rationale=? WHERE id=?",
                           (score, f"{rationale}; possible ghost listing: {', '.join(why)}", oid))
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

    def _ai_fit(self, ctx: AgentContext, opp: dict, profile: dict) -> dict | None:
        """Model verdict on fit for this student. Data in, JSON out; never trusted beyond its shape."""
        prompt = ("Judge how well this listing fits the student below. Reply with JSON only: "
                  '{"fit": 0..1, "reasons": [up to 3 short strings], "red_flags": [up to 3 short strings], '
                  '"eligible_for_india_student": true | false | "unknown"}. eligible_for_india_student is false when the '
                  "listing requires US/EU work authorisation or residency, a completed degree (graduates only), or a PhD; "
                  '"unknown" when it does not say.\nSTUDENT: ' + dumps(profile_summary(profile)) + "\nLISTING:\n"
                  + untrusted(f"{opp['title']}\nCompany: {opp.get('organizer') or 'unknown'}\nLocation: {opp.get('location') or 'unknown'}"
                              f"\n{opp.get('summary') or ''}\n{loads(opp.get('raw_json'), {}).get('raw_text', '')[:4000]}", 6000))
        try:
            data = ctx.llm("classify", prompt, max_tokens=300).json()
        except Exception as e:  # noqa: BLE001  (model optional: the deterministic score stands)
            ctx.log(f"fit scoring unavailable: {e}", level="debug")
            return None
        return clean_verdict(data)

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


def page_title_and_text(html_text: str) -> tuple[str, str]:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html_text or "", "html.parser")
    for t in soup(["script", "style", "noscript", "nav", "footer", "header", "svg"]):
        t.decompose()
    og = soup.find("meta", attrs={"property": "og:title"})
    h1 = soup.find("h1")
    title = (og.get("content") if og and og.get("content") else None) or (h1.get_text(" ", strip=True) if h1 else None) \
        or (soup.title.get_text(" ", strip=True) if soup.title else "")
    return parsing.clean_text(title)[:200], parsing.clean_text(soup.get_text(" "))[:15000]


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


PROFILE_KEYS = ("degree", "institution", "college", "graduation_year", "student_status", "citizenship", "location", "cgpa",
                "skills", "target_roles", "target_season", "available_from", "duration_months", "stipend_min")


def profile_summary(profile: dict) -> dict:
    """What the fit model may see: verified, non-sensitive facts plus interests."""
    facts = {k: v.get("value") for k, v in (profile.get("facts") or {}).items()
             if isinstance(v, dict) and v.get("verified") and not v.get("sensitive") and v.get("value") not in (None, "")
             and k in PROFILE_KEYS}
    return {"facts": facts, "interests": profile.get("interests", [])[:15],
            "preferred_locations": profile.get("preferred_locations", [])[:8]}


def clean_verdict(data) -> dict | None:
    if not isinstance(data, dict):
        return None
    try:
        fit = max(0.0, min(1.0, float(data.get("fit"))))
    except (TypeError, ValueError):
        return None
    el = data.get("eligible_for_india_student", "unknown")
    el = "true" if el is True or str(el).lower() == "true" else "false" if el is False or str(el).lower() == "false" else "unknown"
    lst = lambda k: [str(x)[:140] for x in (data.get(k) or []) if isinstance(x, (str, int, float))][:3]  # noqa: E731
    return {"fit": round(fit, 2), "reasons": lst("reasons"), "red_flags": lst("red_flags"), "eligible": el}


def combine_scores(det: float, rationale: str, v: dict) -> tuple[float, str]:
    """0.4 x deterministic + 0.6 x model; not eligible (US-only, graduates-only, PhD-only) caps it at 0.2."""
    score = round(0.4 * det + 0.6 * v["fit"], 2)
    parts = [rationale, f"AI fit {int(v['fit'] * 100)}%"]
    if v["reasons"]:
        parts.append("why: " + ", ".join(v["reasons"]))
    if v["red_flags"]:
        parts.append("watch out: " + ", ".join(v["red_flags"]))
    if v["eligible"] == "false":
        score = min(score, 0.2)
        parts.append("probably not open to a student in India")
    return score, "; ".join(p for p in parts if p)


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
