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
        return {"create_brief": self.create_brief, "plan_week": self.plan_week}

    def plan_week(self, ctx: AgentContext, task: Task) -> dict:
        """Ideas from what you actually learned and did this week (no invented topics)."""
        week = now() - 7 * 86400
        captures = ctx.db.all("SELECT id, title, url, summary FROM knowledge_captures WHERE status='processed' AND created_at>=? "
                              "ORDER BY created_at DESC LIMIT 3", (week,))
        created = []
        for c in captures:
            tid, new = ctx.enqueue_self("create_brief", {"topic": c["title"], "platform": "linkedin", "category": "technical_learning",
                                                         "sources": [{"url": c["url"], "title": c["title"], "summary": c["summary"]}]},
                                        idempotency_key=f"idea:{c['id']}")
            if new:
                created.append(tid)
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
        g, gv = guidelines()
        cid = new_id("cnt")
        brief = {"topic": topic, "platform": platform, "audience": p.get("audience", "students and early-career engineers"),
                 "angle": p.get("angle") or f"What I learned about {topic}", "sources": sources[:6],
                 "format": "reel" if platform == "instagram" else "post", "guidelines_version": gv,
                 "cta": p.get("cta", "Ask a question or share what you'd add."),
                 "notes": [] if g else ["No creative guidelines imported yet (prompts/content/guidelines.md); drafts will be plain."]}
        ctx.db.execute("INSERT INTO content_items(id, title, platform, format, category, state, brief_json, sources_json, created_at, updated_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (cid, topic, platform, brief["format"], p.get("category", "technical_learning"), "IDEA", dumps(brief),
                        dumps(sources[:6]), now(), now()))
        set_state(ctx, cid, "RESEARCH", f"{len(sources)} source(s)")
        set_state(ctx, cid, "BRIEF", "brief written")
        ctx.delegate("content_studio", "draft_content", {"content_id": cid}, dedupe_key=f"draft:{cid}")
        return {"content_id": cid, "brief": brief}


@register("content_studio")
class ContentStudio(Agent):
    accepts = {"draft_content", "render_reel", "publish_content"}

    def handlers(self):
        return {"draft_content": self.draft, "render_reel": self.render, "publish_content": self.publish}

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
                      f"Audience: {brief['audience']}\nCTA: {brief['cta']}\n"
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
            body = (f"{brief['angle']}.\n\n" + "\n".join(f"• {p[:220]}" for p in pts) + f"\n\n{brief['cta']}"
                    + ("\n\nSources: " + ", ".join(s.get("url") or s.get("title") for s in sources[:3]) if sources else ""))
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
            ctx.notify_owner(f"✋ Content blocked by QC: {item['title'][:80]}\n" + "\n".join(p.get("notes", [])[:5]), key=f"qcblock:{cid}")
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
        pub = publishers.get(item["platform"])
        account = getattr(pub, "account_id", None) or f"{item['platform']}:default"
        attrs = {"category": item["category"], "media_valid": all(a.get("validated", True) for a in assets),
                 "not_duplicate": True, "licenses_ok": qc.get("licenses_ok", False), "has_claims": bool(loads(item["claims_json"], [])),
                 "facts_verified": qc.get("facts_verified", False),
                 "flags": ["sensitive_disclosure"] if qc.get("sensitive") else []}

        def executor(req):
            if pub is None or not pub.configured():
                from ..policies.engine import IntegrationRevoked
                e = IntegrationRevoked(f"{item['platform']} publishing is not configured")
                e.integration = item["platform"]
                raise e
            if item["format"] == "reel":
                raise PermanentError("reel publishing needs a public video URL host; not configured")
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
