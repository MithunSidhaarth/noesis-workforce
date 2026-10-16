"""Content Strategist and Content Studio.

Pipeline: IDEA -> RESEARCH -> BRIEF -> SCRIPT -> ASSET_CREATION -> RENDER ->
QUALITY_CHECK -> SCHEDULED -> PUBLISHED -> PERFORMANCE_REVIEW (or BLOCKED)

Creative rules live in prompts/content/ (version-controlled, separate from the
agents' base instructions). Publishing always goes through the publishing
policy; it starts disabled, so posts queue for your approval.
"""
from __future__ import annotations

from pathlib import Path

from ..contracts import MessageType
from ..core.db import dumps, loads
from ..core.queue import Task
from ..core.settings import get_settings
from ..core.util import new_id, now, sha
from .base import Agent, AgentContext, PermanentError, register, untrusted

STATES = ["IDEA", "RESEARCH", "BRIEF", "SCRIPT", "ASSET_CREATION", "RENDER", "QUALITY_CHECK", "SCHEDULED", "PUBLISHED",
          "PERFORMANCE_REVIEW", "BLOCKED"]


def guidelines() -> tuple[str, str]:
    p = Path(get_settings().prompts_dir) / "content" / "guidelines.md"
    if not p.exists():
        return "", "none"
    txt = p.read_text(encoding="utf-8")
    return txt, sha(txt)[:10]


PLATFORM_STYLE = {
    "instagram": ("Instagram reel. First line is a scroll-stopping hook (under 10 words). Caption under 120 words, "
                  "short lines, one clear takeaway, end with 3-5 specific hashtags. Scenes: punchy on-screen text, 3-6 scenes, under 45s."),
    "linkedin": ("LinkedIn post, first person. Hook line, then a short story of what I built or learned, the problem, what worked, "
                 "one lesson others can use. 120-250 words, short paragraphs, at most 3 hashtags, no emoji spam, no engagement bait."),
}


def set_state(ctx: AgentContext, cid: str, state: str, note: str = "", **fields) -> None:
    row = ctx.db.one("SELECT history_json, state FROM content_items WHERE id=?", (cid,))
    hist = loads(row["history_json"], []) + [{"ts": now(), "from": row["state"], "to": state, "note": note, "by": ctx.bot.id}]
    sets = ", ".join(f"{k}=?" for k in fields)
    ctx.db.execute(f"UPDATE content_items SET state=?, history_json=?, updated_at=?{', ' + sets if sets else ''} WHERE id=?",
                   (state, dumps(hist), now(), *fields.values(), cid))


@register("content_strategist")
class ContentStrategist(Agent):
    accepts = {"create_brief"}

    def handlers(self):
        return {"create_brief": self.create_brief, "plan_week": self.plan_week, "log_work": self.log_work}

    def _fan_out(self, ctx: AgentContext, topic: str, sources: list, category: str, key: str, brand_id: str | None = None) -> list[str]:
        """One brief per brand per platform it posts on; without brands, one LinkedIn brief as before."""
        from ..brands import brands_for, load_brands
        brands = load_brands()
        if brand_id:
            brands = [b for b in brands if b["id"] == brand_id]
            if not brands:
                raise PermanentError(f"unknown brand {brand_id!r} (config/brands.yaml)")
        else:
            brands = brands_for(f"{topic} {' '.join(str(x.get('summary') or '') for x in sources)}", brands)
        targets = [(b["id"], pl) for b in brands for pl in b["channels"]] or [(None, "linkedin")]
        created = []
        for bid, pl in targets:
            tid, new = ctx.enqueue_self("create_brief", {"topic": topic, "platform": pl, "brand": bid, "category": category,
                                                         "sources": sources}, idempotency_key=f"{key}:{bid}:{pl}")
            if new:
                created.append(tid)
        return created

    def log_work(self, ctx: AgentContext, task: Task) -> dict:
        """You tell it what you worked on (/work); it becomes posts for each brand and platform."""
        text = str(task.payload.get("text") or "").strip()[:2000]
        if len(text) < 10:
            raise PermanentError("tell me a bit more about the work (at least a sentence)")
        topic = text.split("\n")[0][:120]
        created = self._fan_out(ctx, topic, [{"title": "Work log (from you)", "summary": text, "owner_provided": True}],
                                task.payload.get("category") or "project_update", f"work:{sha(text)[:16]}", task.payload.get("brand"))
        return {"briefs": len(created)}

    def plan_week(self, ctx: AgentContext, task: Task) -> dict:
        """Ideas from what you actually learned and did this week (no invented topics)."""
        week = now() - 7 * 86400
        captures = ctx.db.all("SELECT id, title, url, summary FROM knowledge_captures WHERE status='processed' AND created_at>=? "
                              "ORDER BY created_at DESC LIMIT 3", (week,))
        created = []
        for c in captures:
            created += self._fan_out(ctx, c["title"], [{"url": c["url"], "title": c["title"], "summary": c["summary"]}],
                                     "technical_learning", f"idea:{c['id']}")
        return {"ideas": len(created), "from_captures": len(captures)}

    def create_brief(self, ctx: AgentContext, task: Task) -> dict:
        p = task.payload
        topic = str(p.get("topic") or "").strip()[:200]
        if not topic:
            raise PermanentError("brief needs a topic")
        platform = p.get("platform") if p.get("platform") in ("linkedin", "instagram") else "linkedin"
        sources = list(p.get("sources") or [])
        for dep in ctx.queue.dependency_results(task).values():
            for r in (dep.get("result") or {}).get("results", []):
                sources.append({"title": r["title"], "path": r["path"], "url": r.get("source_url")})
        from ..brands import get_brand, voice_text
        brand = get_brand(p.get("brand"))
        g, gv = guidelines()
        cid = new_id("cnt")
        brief = {"topic": topic, "platform": platform, "brand": brand["id"] if brand else None, "voice": voice_text(brand),
                 "audience": p.get("audience") or (brand or {}).get("audience") or "students and early-career engineers",
                 "angle": p.get("angle") or f"What I learned about {topic}", "sources": sources[:6],
                 "format": "reel" if platform == "instagram" else "post", "guidelines_version": gv,
                 "cta": p.get("cta", "Ask a question or share what you'd add."),
                 "notes": [] if g else ["No creative guidelines imported yet (prompts/content/guidelines.md); drafts will be plain."]}
        ctx.db.execute("INSERT INTO content_items(id, title, platform, format, category, state, brief_json, sources_json, brand, "
                       "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (cid, topic, platform, brief["format"], p.get("category", "technical_learning"), "IDEA", dumps(brief),
                        dumps(sources[:6]), brief["brand"], now(), now()))
        set_state(ctx, cid, "RESEARCH", f"{len(sources)} source(s)")
        set_state(ctx, cid, "BRIEF", "brief written")
        ctx.delegate("content_studio", "draft_content", {"content_id": cid}, dedupe_key=f"draft:{cid}")
        return {"content_id": cid, "brief": brief}


@register("content_studio")
class ContentStudio(Agent):
    accepts = {"draft_content", "render_reel", "publish_content", "post_ready_reel", "refill_reels"}

    def handlers(self):
        return {"draft_content": self.draft, "render_reel": self.render, "publish_content": self.publish,
                "post_ready_reel": self.post_ready_reel, "refill_reels": self.refill_reels}

    def refill_reels(self, ctx: AgentContext, task: Task) -> dict:
        """Keep a brand's reel queue topped up: when fewer than `keep_queued` reels are
        waiting, write a new script nobody has used (starter bank first, then the
        model), render it from the brand's footage and queue it in the next slot."""
        from integrations.video.cue_reel import ASSETS, check_script, clips, render_cue_reel
        from integrations.video.render import available

        from .. import reels as rl
        from ..brands import get_brand
        bid = task.payload.get("brand") or "cue"
        brand = get_brand(bid)
        if not brand:
            raise PermanentError(f"unknown brand {bid!r} (config/brands.yaml)")
        cfg = {"every_hours": 3, "window": "09:00-23:00", "keep_queued": 2, **(brand.get("reels") or {})}
        waiting = rl.queued_count(ctx.db, bid)
        if waiting >= int(cfg["keep_queued"]):
            return {"brand": bid, "waiting": waiting, "made": 0}
        if not available():
            raise PermanentError("ffmpeg is not installed on this server")
        used = rl.used_hooks(ctx.db, bid)
        script, method = None, "starter"
        bank = Path(task.payload.get("scripts") or ASSETS / "cue" / "scripts.yaml")
        if bank.is_file():
            import yaml
            for sc in (yaml.safe_load(bank.read_text(encoding="utf-8")) or {}).get("scripts") or []:
                if rl.norm(sc.get("hook", "")) not in used and not check_script(sc):
                    script = sc
                    break
        if script is None and ctx.has_model("write"):
            method = "model"
            prompt = (f"Write ONE new Instagram reel script for {brand['name']}.\nABOUT: {brand.get('about')}\n"
                      f"AUDIENCE: {brand.get('audience')}\nVOICE: {brand.get('voice')}\n"
                      f"FOOTAGE you can use (clip names, each is a real screen recording): {', '.join(clips())}. "
                      "Pick clips that actually show what the line says.\n"
                      "Only claim features named in ABOUT. Never say download, free, available now or launch.\n"
                      "It must be about a different angle from every hook already used:\n- "
                      + "\n- ".join(sorted(used)[-40:] or ["(none yet)"]) +
                      "\nReply JSON: {\"hook\": str (max 7 words), \"beats\": [{\"text\": str (max 50 chars), \"clip\": str, "
                      "\"secs\": number 2.5-4}] (3 beats), \"cta\": str (max 6 words), \"caption\": str (hook line, 2-3 short lines, "
                      "'Link in bio.', then 3-5 hashtags; under 600 chars)}")
            for _ in range(2):
                try:
                    d = ctx.llm("write", prompt, max_tokens=900).json()
                except Exception as e:  # noqa: BLE001
                    ctx.log(f"reel script failed: {e}", level="warning")
                    continue
                if not check_script(d) and rl.norm(d.get("hook", "")) not in used and str(d.get("caption") or "").strip():
                    script = d
                    break
        if script is None:
            ctx.notify_owner(f"🎬 I'm out of new {brand['name']} reel ideas\n\nEvery starter script has been used and no "
                             "writing model is set up, so no new reel was made. Add scripts to assets/cue/scripts.yaml "
                             "or set GEMINI_API_KEY.", key=f"reels-empty:{bid}:{int(now() // 86400)}")
            return {"brand": bid, "waiting": waiting, "made": 0, "reason": "no unused script"}
        out = Path(get_settings().data_dir) / "media" / "ready" / f"{bid}-{sha(rl.norm(script['hook']))[:10]}.mp4"
        info = render_cue_reel(script, out)
        at = rl.next_slot(ctx.db, bid, float(cfg["every_hours"]), str(cfg["window"]))
        tid, _ = ctx.enqueue_self("post_ready_reel", {
            "path": info["path"], "caption": str(script["caption"]).strip(), "title": script["hook"], "hook": script["hook"],
            "brand": bid, "series": f"{bid}-auto", "license": "generated-by-noesis (Cue footage, Motion-as-code SFX)",
            "script": {k: script.get(k) for k in ("hook", "beats", "cta")}}, idempotency_key=f"auto-reel:{bid}:{rl.norm(script['hook'])}",
            run_after=at)
        return {"brand": bid, "waiting": waiting + 1, "made": 1, "method": method, "hook": script["hook"], "at": at, "task": tid}

    def post_ready_reel(self, ctx: AgentContext, task: Task) -> dict:
        """A finished reel you made (or I rendered outside NOESIS) with its caption.
        Queued with run_after for its slot; it goes through QC and the publishing
        policy like everything else, so it only posts when publishing is enabled."""
        from integrations.video.render import available, validate_file

        from ..brands import get_brand
        p = task.payload
        path = Path(str(p.get("path") or ""))
        caption = str(p.get("caption") or "").strip()
        if not path.is_file():
            raise PermanentError(f"reel file not found: {path}")
        if not caption:
            raise PermanentError("a ready reel needs a caption")
        brand = get_brand(p.get("brand"))
        if p.get("brand") and not brand:
            raise PermanentError(f"unknown brand {p.get('brand')!r} (config/brands.yaml)")
        if not available():
            raise PermanentError("ffmpeg/ffprobe is not installed, so the reel can't be checked")
        info = validate_file(path)
        cid = new_id("cnt")
        title = str(p.get("title") or caption.split("\n")[0])[:120]
        asset = {"kind": "video", "path": str(path), "license": p.get("license") or "owner-provided",
                 "validated": info["validated"], "problems": info["problems"], "duration": info["duration"],
                 "resolution": f"{info['width']}x{info['height']}"}
        brief = {"topic": title, "platform": "instagram", "brand": brand["id"] if brand else None, "format": "reel",
                 "ready_made": True, "series": p.get("series"), "hook": p.get("hook") or title, "script": p.get("script")}
        ctx.db.execute("INSERT INTO content_items(id, title, platform, format, category, state, brief_json, body, assets_json, "
                       "content_hash, brand, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (cid, title, "instagram", "reel", p.get("category") or "project_update", "IDEA", dumps(brief), caption,
                        dumps([asset]), sha(caption.lower())[:32], brief["brand"], now(), now()))
        set_state(ctx, cid, "RENDER", f"ready-made reel {path.name}")
        self._request_qc(ctx, cid)
        return {"content_id": cid, "asset": asset}

    def draft(self, ctx: AgentContext, task: Task) -> dict:
        item = ctx.db.one("SELECT * FROM content_items WHERE id=?", (task.payload["content_id"],))
        if not item:
            raise PermanentError("content item not found")
        if item["state"] not in ("BRIEF", "SCRIPT"):
            return {"content_id": item["id"], "state": item["state"], "skipped": True}
        brief = loads(item["brief_json"], {})
        g, gv = guidelines()
        sources = brief.get("sources", [])
        method = "template"
        if ctx.has_model("write"):
            src_txt = "\n".join(f"[{i}] {s.get('title')}: {s.get('summary') or ''} ({s.get('url') or s.get('path')})" for i, s in enumerate(sources))
            prompt = (f"Write a {brief['platform']} {brief['format']} about: {brief['topic']}\nAngle: {brief['angle']}\n"
                      f"Audience: {brief['audience']}\nCTA: {brief['cta']}\nFORMAT: {PLATFORM_STYLE.get(brief['platform'], '')}\n"
                      + (f"WHOSE ACCOUNT (write in this voice):\n{brief['voice']}\n" if brief.get("voice") else "")
                      + "Use only what the sources say about my work; never invent results, numbers or names. "
                      "Every factual claim must cite a source index. Reply JSON: {\"body\": str, \"claims\": [{\"text\": str, \"source\": int|null}], "
                      "\"scenes\": [{\"text\": str, \"narration\": str, \"duration\": number}]} (scenes only for reels, 3-6 scenes, total < 60s).\n"
                      f"GUIDELINES:\n{g or '(none provided: keep it plain, first person, no hype)'}\nSOURCES:\n" + untrusted(src_txt, 6000))
            try:
                d = ctx.llm("write", prompt, max_tokens=1500).json()
                body, scenes = str(d.get("body", "")), d.get("scenes") or []
                claims = [{"text": c.get("text"), "source": sources[c["source"]] if isinstance(c.get("source"), int) and c["source"] < len(sources) else None}
                          for c in d.get("claims", [])]
                method = "model"
            except Exception as e:  # noqa: BLE001
                ctx.log(f"model draft failed, using template: {e}", level="warning")
        if method == "template":
            pts = [s.get("summary") or s.get("title") for s in sources if s.get("summary") or s.get("title")][:3]
            from ..brands import get_brand, link_line
            cta = link_line(get_brand(item["brand"]), brief["platform"])
            links = [s["url"] for s in sources[:3] if s.get("url")]
            if brief["platform"] == "instagram":
                body = f"{brief['angle']}\n\n" + "\n".join(f"→ {p[:120]}" for p in pts) + f"\n\n{brief['cta']}" + (f"\n{cta}" if cta else "")
            else:
                body = (f"{brief['angle']}.\n\n" + "\n".join(f"• {p[:220]}" for p in pts) + f"\n\n{brief['cta']}" + (f"\n{cta}" if cta else "")
                        + ("\n\nSources: " + ", ".join(links) if links else ""))
            scenes = [{"text": brief["angle"], "narration": brief["angle"], "duration": 3}] + \
                     [{"text": p[:90], "narration": p[:160], "duration": 4} for p in pts] + \
                     [{"text": brief["cta"][:90], "narration": brief["cta"], "duration": 3}]
            claims = []
        set_state(ctx, item["id"], "SCRIPT", f"drafted by {method}", body=body,
                  script_json=dumps({"scenes": scenes, "method": method, "guidelines_version": gv}),
                  claims_json=dumps(claims), content_hash=sha(body.strip().lower())[:32])
        if item["format"] == "reel":
            ctx.enqueue_self("render_reel", {"content_id": item["id"]}, idempotency_key=f"render:{item['id']}")
        else:
            self._request_qc(ctx, item["id"])
        return {"content_id": item["id"], "method": method, "chars": len(body)}

    def render(self, ctx: AgentContext, task: Task) -> dict:
        from integrations.video.render import available, render_reel
        item = ctx.db.one("SELECT * FROM content_items WHERE id=?", (task.payload["content_id"],))
        scenes = loads(item["script_json"], {}).get("scenes", [])
        if not scenes:
            raise PermanentError("no scenes to render")
        if not available():
            raise PermanentError("ffmpeg is not installed on this server")
        set_state(ctx, item["id"], "ASSET_CREATION", "generated backgrounds + text (license: generated)")
        out = Path(get_settings().data_dir) / "media" / f"{item['id']}.mp4"
        info = render_reel(scenes, out, budget_s=ctx.time_left())
        asset = {"kind": "video", "path": info["path"], "subtitles": info["subtitles"], "license": "generated-by-noesis",
                 "validated": info["validated"], "problems": info["problems"], "duration": info["duration"],
                 "resolution": f"{info['width']}x{info['height']}"}
        set_state(ctx, item["id"], "RENDER", "rendered", assets_json=dumps([asset]))
        self._request_qc(ctx, item["id"])
        return {"content_id": item["id"], "asset": asset}

    def _request_qc(self, ctx: AgentContext, cid: str) -> None:
        set_state(ctx, cid, "QUALITY_CHECK", "sent to Quality agent")
        ctx.send(MessageType.VERIFICATION_REQUEST, {"kind": "content", "content_id": cid}, recipient="quality",
                 dedupe_key=f"qc:{cid}:{int(now())}", response_type=MessageType.VERIFICATION_RESULT)

    def on_verification_result(self, ctx: AgentContext, msg: dict) -> None:
        p = msg["payload"]
        cid = p.get("content_id")
        item = ctx.db.one("SELECT * FROM content_items WHERE id=?", (cid,)) if cid else None
        if not item or item["state"] != "QUALITY_CHECK":
            return
        if p["status"] == "failed":
            set_state(ctx, cid, "BLOCKED", "; ".join(p.get("notes", []))[:500])
            ctx.notify_owner(f"✋ A post was held back by quality checks\n\n“{item['title'][:80]}”\n"
                             + "\n".join(f"• {n}" for n in p.get("notes", [])[:5])
                             + "\n\nNothing was published. Review it on the dashboard Content page.", key=f"qcblock:{cid}")
            return
        md = loads(item["brief_json"], {})
        md["qc"] = p
        set_state(ctx, cid, "SCHEDULED", f"QC {p['status']}", brief_json=dumps(md), scheduled_for=now())
        ctx.enqueue_self("publish_content", {"content_id": cid}, idempotency_key=f"publish:{cid}", correlation_id=msg.get("correlation_id"))
        ctx.send(MessageType.RESEARCH_FINDING, {"content_id": cid, "title": item["title"]}, topic="content.ready", dedupe_key=f"cready:{cid}")

    def publish(self, ctx: AgentContext, task: Task) -> dict:
        item = ctx.db.one("SELECT * FROM content_items WHERE id=?", (task.payload["content_id"],))
        if item["state"] == "PUBLISHED":
            return {"content_id": item["id"], "duplicate": True}
        if task.payload.get("approval_denied"):
            set_state(ctx, item["id"], "BLOCKED", "publication denied by owner")
            return {"content_id": item["id"], "state": "BLOCKED"}
        qc = loads(item["brief_json"], {}).get("qc", {})
        assets = loads(item["assets_json"], [])
        publishers = ctx.services.get("publishers", {})
        pub = (publishers.get(f"{item['platform']}:{item['brand']}") if item["brand"] else None) or publishers.get(item["platform"])
        account = getattr(pub, "account_id", None) or f"{item['platform']}:default"
        attrs = {"category": item["category"], "media_valid": all(a.get("validated", True) for a in assets),
                 "not_duplicate": True, "licenses_ok": qc.get("licenses_ok", False), "has_claims": bool(loads(item["claims_json"], [])),
                 "facts_verified": qc.get("facts_verified", False),
                 "flags": (["sensitive_disclosure"] if qc.get("sensitive") else [])
                 + (["template_draft"] if loads(item["script_json"], {}).get("method") == "template" else [])}

        def executor(req):
            if pub is None or not pub.configured():
                from ..policies.engine import IntegrationRevoked
                e = IntegrationRevoked(f"{item['platform']} publishing is not configured")
                e.integration = item["platform"]
                raise e
            if item["format"] == "reel":
                video = next((a["path"] for a in assets if a.get("kind") == "video" and a.get("path")), None)
                wait = max(30.0, (ctx.time_left() or 600) - 60)
                if video and hasattr(pub, "publish_reel_file"):
                    return pub.publish_reel_file(video, item["body"] or item["title"], wait_s=wait)
                if video and hasattr(pub, "publish_video_file"):
                    return pub.publish_video_file(video, item["body"] or item["title"], title=item["title"], wait_s=wait)
                raise PermanentError(f"no rendered video to publish, or {item['platform']} does not take video")
            return pub.publish_text(item["body"])

        res = ctx.act("social.publish", f"publish:{item['id']}", executor, target=account,
                      params={"platform": item["platform"], "chars": len(item["body"] or "")}, attributes=attrs,
                      summary=f"Publish {item['platform']} {item['format']}: {item['title'][:80]}")
        if res.status == "succeeded":
            from ..core.state import set_integration_status
            set_integration_status(ctx.db, item["platform"], "ok", "last publish succeeded")
            set_state(ctx, item["id"], "PUBLISHED", res.evidence or "", published_at=now(),
                      platform_id=(res.result or {}).get("platform_id"), publication_url=(res.result or {}).get("url"))
        elif res.status == "dry_run":
            ctx.log("dry-run: content would have been published", content_id=item["id"])
        return {"content_id": item["id"], "action": res.status, "reasons": res.reasons, "evidence": res.evidence}
