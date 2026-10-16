"""Specialist bots: applications, vault safety, content pipeline (real FFmpeg),
communications, outreach, knowledge, orchestrator workflows."""
import json
import shutil
from pathlib import Path

import httpx
import pytest
import yaml

from tests.helpers import ScriptedProvider
from workforce.contracts import TaskSpec
from workforce.core.db import loads
from workforce.core.util import now
from workforce.runtime import BotRuntime


def run(services, bots, rounds=30):
    rts = [BotRuntime(b, services, "it") for b in bots]
    for _ in range(rounds):
        if sum(rt.step() for rt in rts) == 0:
            return
    raise AssertionError("did not settle")


def add_opp(db, **kw):
    from workforce.core.db import dumps
    row = dict(id="opp_x", fingerprint="fp_x", title="Student AI Fellowship", official_url="https://example.org/f",
               discovery_source="manual", type="fellowship", remote_status="remote", deadline=now() + 20 * 86400,
               eligibility_json=dumps(["Open to undergraduate students who are Indian citizens."]),
               required_materials_json=dumps(["resume"]), discovered_at=now(), verification_status="verified",
               summary="A fellowship for students building AI tools.", raw_json=dumps({"raw_text": "apply on the website"}))
    row.update(kw)
    cols = ",".join(row)
    db.execute(f"INSERT INTO opportunities({cols}) VALUES ({','.join('?' * len(row))})", tuple(row.values()))
    return row["id"]


def write_profile(env, facts, answers=None, documents=None):
    (env.config / "profile.yaml").write_text(yaml.safe_dump({
        "name": "Test Owner", "facts": facts, "answers": answers or {}, "documents": documents or {},
        "interests": ["ai"], "preferred_types": ["fellowship"]}))


def test_applications_use_only_verified_profile_data(env, services):
    write_profile(env, {
        "degree": {"value": "B.E. Computer Science (undergraduate)", "verified": True},
        "citizenship": {"value": "Indian", "verified": True},
        "award": {"value": "Won national olympiad", "verified": False},   # unverified: must never appear
    }, answers={"short_bio": {"value": "CS undergraduate building agent tools.", "verified": True}},
       documents={"resume": {"path": "docs/resume.pdf", "verified": True}})
    prov = ScriptedProvider(responses=["I am a CS undergraduate building agent tools and want to grow in AI research."])
    services.router._providers = {"anthropic": prov}
    services.router._explicit = True
    import os
    os.environ["ANTHROPIC_API_KEY"] = "test"
    try:
        oid = add_opp(env.db)
        services.queue.enqueue(TaskSpec(bot_id="applications", kind="evaluate_opportunity", payload={"opportunity_id": oid}))
        run(services, ["applications", "personal_ops"])
    finally:
        del os.environ["ANTHROPIC_API_KEY"]
    app = env.db.one("SELECT * FROM applications")
    assert app["state"] == "REVIEW"
    assert env.db.scalar("SELECT eligibility_status FROM opportunities") == "eligible"
    answers = loads(app["answers_json"])
    assert answers["short_bio"]["status"] == "verified"
    assert answers["why_applying"]["status"] == "draft_requires_your_review"
    assert "olympiad" not in json.dumps(answers).lower()
    assert loads(app["documents_json"]) == [{"material": "resume", "path": "docs/resume.pdf"}]
    # the prompt sent to the model contained only verified facts
    # (ScriptedProvider doesn't keep prompts; check the unverified fact is filtered at source)
    from workforce.agents.applications import verified_facts
    from workforce.sources.registry import load_profile
    assert "award" not in verified_facts(load_profile())


def test_ineligible_application_closes(env, services):
    write_profile(env, {"degree": {"value": "B.E.", "verified": True}, "citizenship": {"value": "Kenyan", "verified": True}})
    oid = add_opp(env.db)
    services.queue.enqueue(TaskSpec(bot_id="applications", kind="evaluate_opportunity", payload={"opportunity_id": oid}))
    run(services, ["applications"])
    assert env.db.scalar("SELECT state FROM applications") == "CLOSED"


def test_web_form_submission_is_manual_and_evidenced(env, services):
    write_profile(env, {"degree": {"value": "B.E. undergraduate", "verified": True}, "citizenship": {"value": "Indian", "verified": True}})
    oid = add_opp(env.db)
    services.queue.enqueue(TaskSpec(bot_id="applications", kind="evaluate_opportunity", payload={"opportunity_id": oid}))
    run(services, ["applications", "personal_ops"])
    app = env.db.one("SELECT * FROM applications")
    services.queue.enqueue(TaskSpec(bot_id="applications", kind="submit_application", payload={"application_id": app["id"]}))
    run(services, ["applications"])
    assert env.db.scalar("SELECT state FROM applications") == "READY_TO_SUBMIT"
    assert env.db.scalar("SELECT COUNT(*) FROM external_actions WHERE action='application.submit'") == 0  # never auto-submitted
    from workforce.agents.applications import record_manual_submission
    rec = record_manual_submission(env.db, app["id"], "CONF-123", by="user")
    assert rec["state"] == "SUBMITTED" and "CONF-123" in rec["evidence"]


def test_obsidian_preserves_user_notes_and_avoids_duplicates(env):
    from integrations.obsidian.vault import Vault
    v = Vault(env.vault)
    user = Path(env.vault) / "09 Opportunities" / "My note.md"
    user.parent.mkdir(parents=True)
    user.write_text("---\ntitle: mine\n---\nhand written")
    r = v.upsert_note("k1", "09 Opportunities/My note.md", {"title": "t"}, "generated", "test")
    assert user.read_text() == "---\ntitle: mine\n---\nhand written"  # untouched
    assert r["path"] == "09 Opportunities/My note (2).md"
    r2 = v.upsert_note("k1", "09 Opportunities/other-name.md", {"title": "t"}, "generated", "test")
    assert r2["path"] == r["path"] and not r2["changed"]               # same key -> same note, no duplicate
    r3 = v.upsert_note("k1", "x.md", {"title": "t"}, "updated body", "test")
    assert r3["changed"] and "updated body" in (Path(env.vault) / r["path"]).read_text()
    assert len(list((Path(env.vault) / "09 Opportunities").glob("*.md"))) == 2
    assert "created" in (Path(env.vault) / "99 System/Workforce/changelog.md").read_text()
    with pytest.raises(Exception):
        v.upsert_note("k2", "../../etc/passwd.md", {}, "x", "test")
    (Path(env.vault) / ".system").mkdir()
    (Path(env.vault) / ".system" / "mcp_token").write_text("secret")
    for hidden in (".system/mcp_token", ".system/note.md", "09 Opportunities/../.obsidian/x.md"):
        with pytest.raises(Exception):
            v.read(hidden)
        with pytest.raises(Exception):
            v.upsert_note("k3", hidden, {}, "x", "test")
    assert (Path(env.vault) / ".system" / "mcp_token").read_text() == "secret"
    fresh = Vault(env.vault)  # index rebuilt from disk after restart
    assert fresh.upsert_note("k1", "y.md", {"title": "t"}, "updated body", "test")["changed"] is False


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_content_pipeline_renders_real_reel_and_gates_publication(env, services):
    services.queue.enqueue(TaskSpec(bot_id="content_strategist", kind="create_brief", payload={
        "topic": "Idempotency keys in agent systems", "platform": "instagram", "category": "technical_learning",
        "sources": [{"title": "Idempotency note", "summary": "Idempotency keys stop duplicate side effects on retries.",
                     "url": "https://example.org/idem"}]}))
    run(services, ["content_strategist", "content_studio", "quality"], rounds=40)
    item = env.db.one("SELECT * FROM content_items")
    asset = loads(item["assets_json"])[0]
    assert Path(asset["path"]).exists() and asset["validated"] and asset["resolution"] == "1080x1920"
    assert Path(asset["subtitles"]).read_text().startswith("1\n00:00:00,000")
    # publishing policy is disabled by default -> approval required, nothing published
    assert item["state"] == "SCHEDULED"
    t = env.db.one("SELECT * FROM tasks WHERE kind='publish_content'")
    assert t["status"] == "waiting_approval"
    assert env.db.one("SELECT status FROM approvals WHERE action='social.publish'")["status"] == "pending"
    assert item["platform_id"] is None and item["published_at"] is None


def test_content_qc_blocks_unsourced_claims(env, services):
    from workforce.core.db import dumps
    env.db.execute("INSERT INTO content_items(id, title, platform, format, category, state, body, claims_json, assets_json, created_at, updated_at) "
                   "VALUES ('c1','t','linkedin','post','educational','QUALITY_CHECK','We guarantee 100% results', ?, '[]', ?, ?)",
                   (dumps([{"text": "x", "source": None}]), now(), now()))
    from workforce.agents.quality import QualityAgent
    from workforce.runtime import BotRuntime
    rt = BotRuntime("quality", services, "t")
    res = QualityAgent().verify_content(rt.ctx(rt.config()), {"content_id": "c1"})
    assert res["status"] == "failed" and not res["facts_verified"] and res["sensitive"]


class FakeMailbox:
    def __init__(self):
        self.drafts, self.sent = [], []

    def configured(self):
        return True

    def list_recent(self):
        return [
            {"id": "m1", "thread_id": "t1", "from": "Recruiter <hr@corp.com>", "subject": "Can we schedule a call next week?",
             "snippet": "Please share your availability", "received_at": now(), "list_unsubscribe": False},
            {"id": "m2", "thread_id": "t2", "from": "news@letter.com", "subject": "Weekly digest", "snippet": "...",
             "received_at": now(), "list_unsubscribe": True},
        ]

    def create_draft(self, to, subject, body, thread_id=None):
        self.drafts.append((to, subject))
        return {"evidence": f"draft-{len(self.drafts)}"}

    def send(self, *a, **k):
        self.sent.append(a)
        return {"evidence": "sent"}


def test_communications_classifies_drafts_and_never_sends_without_policy(env, services):
    mb = FakeMailbox()
    services.extra["mailbox"] = mb
    services.queue.enqueue(TaskSpec(bot_id="communications", kind="check_inbox"))
    run(services, ["communications"])
    e1 = env.db.one("SELECT * FROM emails WHERE id='m1'")
    assert e1["category"] == "meeting_request" and e1["priority"] == "high" and e1["draft"]
    assert "calendar" not in e1["draft"].lower() or "Possible times" not in e1["draft"]  # no invented availability
    assert env.db.scalar("SELECT category FROM emails WHERE id='m2'") == "newsletter"
    assert mb.drafts == [("hr@corp.com", "Re: Can we schedule a call next week?")]
    # second poll: no duplicate drafts
    services.queue.enqueue(TaskSpec(bot_id="communications", kind="check_inbox", idempotency_key="again"))
    run(services, ["communications"])
    assert len(mb.drafts) == 1
    # sending requires approval under the default email policy
    tid, _ = services.queue.enqueue(TaskSpec(bot_id="communications", kind="send_reply", payload={"email_id": "m1"}))
    run(services, ["communications"])
    assert services.queue.get(tid).status == "waiting_approval" and mb.sent == []


def test_slot_suggestions_only_from_real_busy_data():
    from workforce.agents.communications import suggest_slots
    from datetime import datetime
    from zoneinfo import ZoneInfo
    z = ZoneInfo("Asia/Kolkata")
    start = datetime(2026, 10, 19, 9, 0, tzinfo=z).timestamp()  # Monday
    busy = [(datetime(2026, 10, 19, 10, 0, tzinfo=z).timestamp(), datetime(2026, 10, 19, 12, 0, tzinfo=z).timestamp())]
    slots = suggest_slots(busy, start, 1, "Asia/Kolkata", 30, limit=2)
    assert [datetime.fromtimestamp(s, z).hour for s in slots] == [12, 12]


def test_outreach_drafts_honestly_and_honors_opt_out(env, services):
    from workforce.agents.outreach import check_honesty, mark_opted_out
    env.db.execute("INSERT INTO contacts(id, email, name, organization, role, source_url, notes, created_at, updated_at) VALUES "
                   "('c1','lead@ai.co','Asha','AI Co','ML lead','https://ai.co/team','Works on ai agents and llm evals',?,?)", (now(), now()))
    services.queue.enqueue(TaskSpec(bot_id="outreach", kind="draft_outreach", payload={"contact_id": "c1"}))
    run(services, ["outreach"])
    m = env.db.one("SELECT * FROM outreach_messages")
    assert m["status"] == "draft" and "won't follow up" in m["body"] and not check_honesty(m["subject"], m["body"])
    assert check_honesty("Re: our chat", "As promised, Raj referred me")  # deceptive patterns are caught
    services.queue.enqueue(TaskSpec(bot_id="outreach", kind="send_outreach", payload={"message_id": m["id"]}))
    run(services, ["outreach"])
    assert env.db.scalar("SELECT status FROM tasks WHERE kind='send_outreach'") == "waiting_approval"
    mark_opted_out(env.db, "lead@ai.co", "replied no thanks", by="user")
    services.queue.enqueue(TaskSpec(bot_id="outreach", kind="send_outreach", payload={"message_id": m["id"]}, idempotency_key="2"))
    run(services, ["outreach"])
    a = env.db.one("SELECT * FROM external_actions WHERE action='outreach.send'")
    assert a["status"] == "blocked" and "suppression" in a["error"]


def test_knowledge_ingests_article_and_queues_reels(env, services):
    from integrations.obsidian.vault import Vault
    from integrations.web.fetcher import Fetcher
    page = ("<html><head><title>Agents 101</title><meta name='author' content='Jane'></head><body><article>"
            + "".join(f"<p>Paragraph {i} about llm agents, tool calling, and idempotency in distributed systems design.</p>" for i in range(8))
            + "</article></body></html>")
    transport = httpx.MockTransport(lambda req: httpx.Response(404) if req.url.path == "/robots.txt" else httpx.Response(200, text=page))
    services.extra["fetcher"] = Fetcher(transport=transport, min_host_interval_s=0)
    services.extra["vault"] = Vault(env.vault)
    services.queue.enqueue(TaskSpec(bot_id="knowledge", kind="ingest_url", payload={"url": "https://blog.example.com/agents?utm_source=x"}))
    services.queue.enqueue(TaskSpec(bot_id="knowledge", kind="ingest_url", payload={"url": "https://www.instagram.com/reel/ABC/"}))
    run(services, ["knowledge"])
    caps = env.db.all("SELECT * FROM knowledge_captures ORDER BY created_at")
    assert len(caps) == 2 and all(c["status"] == "processed" for c in caps)
    art = (Path(env.vault) / caps[0]["note_path"]).read_text()
    assert caps[0]["note_path"].startswith("02 Knowledge/") and 'source_type: "article"' in art and "[[" in art
    assert caps[1]["note_path"].startswith("01 Inbox/")
    services.queue.enqueue(TaskSpec(bot_id="knowledge", kind="ingest_url", payload={"url": "https://blog.example.com/agents"}, idempotency_key="dup"))
    run(services, ["knowledge"])
    assert env.db.scalar("SELECT COUNT(*) FROM knowledge_captures") == 2
    assert env.db.one("SELECT result_json FROM tasks WHERE idempotency_key='dup'")["result_json"].find("duplicate") > 0


def test_orchestrator_builds_dependency_graph(env, services):
    from integrations.obsidian.vault import Vault
    services.extra["vault"] = Vault(env.vault)
    services.queue.enqueue(TaskSpec(bot_id="orchestrator", kind="objective", payload={"text": "Write a LinkedIn post about agent idempotency"},
                                    requested_by="telegram"))
    run(services, ["orchestrator", "knowledge", "content_strategist", "content_studio", "quality"], rounds=40)
    wf = env.db.one("SELECT * FROM workflows")
    assert wf["name"] == "content_from_topic"
    steps = env.db.all("SELECT kind, status, depends_on_json FROM tasks WHERE workflow_id=? ORDER BY created_at", (wf["id"],))
    assert [s["kind"] for s in steps][:2] == ["find_context", "create_brief"]
    assert loads(steps[1]["depends_on_json"])  # brief waited for context
    assert env.db.scalar("SELECT COUNT(*) FROM content_items") == 1


def test_bot_isolation_of_memory(env, services):
    a = BotRuntime("echo_a", services, "t")
    b = BotRuntime("echo_b", services, "t")
    a.ctx(a.config()).memory_set("secret", "only-a")
    assert b.ctx(b.config()).memory_get("secret") is None


def test_instagram_reel_uploads_file_directly_without_public_url(tmp_path):
    import httpx
    from integrations.social.publishers import InstagramPublisher
    video = tmp_path / "r.mp4"
    video.write_bytes(b"\x00" * 1234)
    calls, polls = [], iter(["IN_PROGRESS", "FINISHED"])

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.host, req.url.path, dict(req.headers)))
        if req.url.path.endswith("/media") and req.url.params.get("upload_type") == "resumable":
            return httpx.Response(200, json={"id": "c1", "uri": "https://rupload.facebook.com/ig-api-upload/v21.0/c1"})
        if req.url.host == "rupload.facebook.com":
            assert req.headers["file_size"] == "1234" and req.headers["offset"] == "0" and len(req.content) == 1234
            return httpx.Response(200, json={"success": True})
        if req.url.path.endswith("/c1"):
            return httpx.Response(200, json={"status_code": next(polls)})
        if req.url.path.endswith("/media_publish"):
            return httpx.Response(200, json={"id": "m9"})
        return httpx.Response(404)

    pub = InstagramPublisher(token="t", ig_user_id="u1", transport=httpx.MockTransport(handler))
    out = pub.publish_reel_file(video, "caption", sleep=lambda s: None)
    assert out["platform_id"] == "m9"
    assert [c[1] for c in calls] == ["graph.facebook.com", "rupload.facebook.com", "graph.facebook.com",
                                     "graph.facebook.com", "graph.facebook.com"]
    assert calls[1][3]["authorization"] == "OAuth t"

    def failing(req):
        if req.url.path.endswith("/media"):
            return httpx.Response(200, json={"id": "c2"})
        if req.url.host == "rupload.facebook.com":
            return httpx.Response(200, json={"success": True})
        return httpx.Response(200, json={"status_code": "ERROR", "status": "bad codec"})
    pub = InstagramPublisher(token="t", ig_user_id="u1", transport=httpx.MockTransport(failing))
    with pytest.raises(Exception, match="could not process"):
        pub.publish_reel_file(video, "c", sleep=lambda s: None)


def test_linkedin_video_upload_parts_finalize_and_post(tmp_path):
    import httpx
    from integrations.social.publishers import LinkedInPublisher
    video = tmp_path / "v.mp4"
    video.write_bytes(bytes(range(256)) * 40)          # 10240 bytes, two parts
    seen = {"parts": [], "finalize": None, "post": None}
    status = iter(["PROCESSING", "AVAILABLE"])

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers.get("linkedin-version", "202609") == "202609" or req.method == "PUT"
        if "initializeUpload" in str(req.url):
            return httpx.Response(200, json={"value": {"video": "urn:li:video:V1", "uploadToken": "tok", "uploadInstructions": [
                {"uploadUrl": "https://up.example/1", "firstByte": 0, "lastByte": 4999},
                {"uploadUrl": "https://up.example/2", "firstByte": 5000, "lastByte": 10239}]}})
        if req.method == "PUT":
            seen["parts"].append(len(req.content))
            return httpx.Response(200, headers={"etag": f'"e{len(seen["parts"])}"'})
        if "finalizeUpload" in str(req.url):
            import json
            seen["finalize"] = json.loads(req.content)
            return httpx.Response(200)
        if req.url.path.startswith("/rest/videos/"):
            return httpx.Response(200, json={"status": next(status)})
        if req.url.path == "/rest/posts":
            import json
            seen["post"] = json.loads(req.content)
            return httpx.Response(201, headers={"x-restli-id": "urn:li:share:7"})
        return httpx.Response(404)

    pub = LinkedInPublisher(token="t", author_urn="urn:li:person:me", transport=httpx.MockTransport(handler))
    out = pub.publish_video_file(video, "text", title="T", sleep=lambda s: None)
    assert out["platform_id"] == "urn:li:share:7"
    assert seen["parts"] == [5000, 5240]
    assert seen["finalize"]["finalizeUploadRequest"]["uploadedPartIds"] == ["e1", "e2"]
    assert seen["post"]["content"]["media"]["id"] == "urn:li:video:V1"
    assert pub.publish_text("hi")["platform_id"] == "urn:li:share:7"
