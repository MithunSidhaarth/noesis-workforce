"""Brands (cue.notch / msthinks style multi-account posting through Postiz),
weekly project-idea scout, email applications and the source keyword filter.
All outside services are fakes or httpx.MockTransport; nothing here proves
that Postiz, GitHub or Gmail accept a real call."""
import json

import httpx
import yaml

from tests.integration.test_specialists import run
from workforce.contracts import TaskSpec
from workforce.core.db import dumps, loads
from workforce.core.util import now

BRANDS = {"brands": [
    {"id": "cuenotch", "name": "cue.notch", "voice": "punchy build-log", "channels": {
        "instagram": {"postiz_integration": "ig-cue"}, "linkedin": {"postiz_integration": "li-cue"}}},
    {"id": "msthinks", "name": "msthinks", "voice": "reflective essays", "topics": ["research", "paper"], "channels": {
        "instagram": {"postiz_integration": "ig-ms"}, "linkedin": {"postiz_integration": "li-ms"}}},
]}


def test_postiz_publisher_uploads_media_and_posts_to_the_right_channel():
    from integrations.social.publishers import PostizPublisher
    calls = []

    def handler(req: httpx.Request):
        calls.append((req.method, req.url.path, req.headers.get("authorization")))
        if req.url.path.endswith("/integrations"):
            return httpx.Response(200, json=[{"id": "ig-cue", "identifier": "instagram-standalone", "name": "cue.notch", "disabled": False}])
        if req.url.path.endswith("/upload"):
            assert b"REELBYTES" in req.content
            return httpx.Response(200, json={"id": "m1", "path": "https://uploads.postiz.com/m1.mp4"})
        if req.url.path.endswith("/posts"):
            body = json.loads(req.content)
            post = body["posts"][0]
            assert body["type"] == "now" and post["integration"]["id"] == "ig-cue"
            assert post["settings"]["__type"] == "instagram-standalone" and post["settings"]["post_type"] == "post"
            assert post["value"][0]["image"] == [{"id": "m1", "path": "https://uploads.postiz.com/m1.mp4"}]
            return httpx.Response(200, json=[{"postId": "p42", "integration": "ig-cue"}])
        return httpx.Response(404)

    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".mp4") as f:
        f.write(b"REELBYTES")
        f.flush()
        pub = PostizPublisher("ig-cue", "instagram", api_key="k", base_url="https://postiz.test/public/v1",
                              transport=httpx.MockTransport(handler))
        res = pub.publish_reel_file(f.name, "hook\ncaption #ai")
    assert res["platform_id"] == "postiz:p42" and "accepted by Postiz" in res["evidence"]
    assert [c[1].rsplit("/", 1)[-1] for c in calls] == ["integrations", "upload", "posts"]
    assert all(c[2] == "k" for c in calls)  # raw key, no Bearer prefix
    # Instagram never gets a text-only post; unknown channel is a clear credential error
    import pytest
    from workforce.policies.engine import IntegrationError, IntegrationRevoked
    with pytest.raises(IntegrationError):
        pub.publish_text("text only")
    other = PostizPublisher("gone", "linkedin", api_key="k", base_url="https://postiz.test/public/v1",
                            transport=httpx.MockTransport(handler))
    with pytest.raises(IntegrationRevoked):
        other.publish_text("hello")


def test_work_log_becomes_one_post_per_brand_per_platform_in_each_voice(env, services):
    (env.config / "brands.yaml").write_text(yaml.safe_dump(BRANDS))
    from workforce.owner_actions import OwnerActionError, log_work
    log_work(env.db, "cue.notch shipped a voice agent demo with Whisper and Llama, 300ms latency", by="t")
    run(services, ["content_strategist"], rounds=20)
    rows = env.db.all("SELECT brand, platform, format, brief_json FROM content_items ORDER BY platform")
    assert [(r["brand"], r["platform"], r["format"]) for r in rows] == [("cuenotch", "instagram", "reel"), ("cuenotch", "linkedin", "post")]
    assert "punchy build-log" in loads(rows[0]["brief_json"])["voice"]
    # no brand named: topics route research work to msthinks only
    log_work(env.db, "read a paper on KV cache compression and reproduced the results", by="t")
    run(services, ["content_strategist"], rounds=20)
    assert {r["brand"] for r in env.db.all("SELECT brand FROM content_items WHERE title LIKE 'read a paper%'")} == {"msthinks"}
    import pytest
    with pytest.raises(OwnerActionError):
        log_work(env.db, "msthinks hi", by="t")


def test_template_drafts_differ_per_platform(env, services):
    (env.config / "brands.yaml").write_text(yaml.safe_dump(BRANDS))
    services.queue.enqueue(TaskSpec(bot_id="content_strategist", kind="log_work",
                                    payload={"text": "built a RAG bot for my college notes using pgvector", "brand": "cuenotch"}))
    run(services, ["content_strategist", "content_studio"], rounds=30)
    bodies = {r["platform"]: r["body"] for r in env.db.all("SELECT platform, body FROM content_items WHERE body IS NOT NULL")}
    assert "linkedin" in bodies and "instagram" in bodies and bodies["linkedin"] != bodies["instagram"]
    assert "→" in bodies["instagram"] and "•" in bodies["linkedin"]


def test_publish_routes_to_the_brand_channel_and_caps_are_per_account(env):
    (env.config / "brands.yaml").write_text(yaml.safe_dump(BRANDS))
    p = env.policies / "publishing.yaml"
    pol = yaml.safe_load(p.read_text())
    pol.update(enabled=True, accounts_allowlist=["postiz:li-cue", "postiz:li-ms"], max_posts_per_day=1)
    p.write_text(yaml.safe_dump(pol))
    from tests.helpers import FakeTelegram
    from workforce.model_router.router import ModelRouter
    from workforce.runtime import Services
    services = Services(env.db, router=ModelRouter(env.db, providers={}), extra={"telegram": FakeTelegram()})

    class Pub:
        def __init__(self, cid):
            self.account_id, self.sent = f"postiz:{cid}", []

        def configured(self):
            return True

        def publish_text(self, text):
            self.sent.append(text)
            return {"platform_id": f"postiz:{len(self.sent)}", "evidence": "fake"}

    pubs = {"linkedin:cuenotch": Pub("li-cue"), "linkedin:msthinks": Pub("li-ms")}
    services.extra["publishers"] = pubs
    for i, brand in enumerate(["cuenotch", "msthinks", "cuenotch"]):
        env.db.execute("INSERT INTO content_items(id, title, platform, format, category, state, body, brand, brief_json, assets_json, "
                       "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       (f"c{i}", f"t{i}", "linkedin", "post", "project_update", "SCHEDULED", f"body {i}", brand,
                        dumps({"qc": {"licenses_ok": True, "facts_verified": True}}), "[]", now(), now()))
        services.queue.enqueue(TaskSpec(bot_id="content_studio", kind="publish_content", payload={"content_id": f"c{i}"}))
        run(services, ["content_studio"], rounds=10)
    assert pubs["linkedin:cuenotch"].sent == ["body 0"] and pubs["linkedin:msthinks"].sent == ["body 1"]
    # third post is cue.notch's second today: held by the per-account cap, not published
    assert env.db.one("SELECT state FROM content_items WHERE id='c2'")["state"] == "SCHEDULED"


def test_idea_scout_writes_vault_note_and_reports_failed_sources(env, services):
    from integrations.obsidian.vault import Vault
    services.extra["vault"] = Vault(str(env.vault))
    sig = [{"source": "GitHub", "title": "acme/agent-kit", "url": "https://github.com/acme/agent-kit", "summary": "LLM agents toolkit",
            "score": 900, "tags": ["agents"]},
           {"source": "Show HN", "title": "Show HN: Tiny OCR", "url": "https://x.test/ocr", "summary": "120 points", "score": 120, "tags": []}]
    services.extra["trend_collector"] = lambda cfg: (sig, ["arXiv: ConnectError boom"])
    services.queue.enqueue(TaskSpec(bot_id="knowledge", kind="idea_scout", payload={}))
    run(services, ["knowledge"], rounds=10)
    t = env.db.one("SELECT status, result_json FROM tasks WHERE kind='idea_scout'")
    res = loads(t["result_json"])
    assert t["status"] == "succeeded" and res["signals"] == 2 and res["method"] == "list"
    note = next((env.vault / "02 Knowledge" / "Project Ideas").glob("*.md")).read_text()
    assert "acme/agent-kit" in note and "arXiv: ConnectError" in note and "GEMINI_API_KEY" in note


def test_trend_signals_parse_official_apis():
    from integrations.trends.signals import collect

    def handler(req: httpx.Request):
        h = req.url.host
        if h == "api.github.com":
            return httpx.Response(200, json={"items": [{"full_name": "a/b", "html_url": "https://github.com/a/b", "description": "d",
                                                        "stargazers_count": 5, "topics": ["llm"], "language": "Python"}]})
        if h == "huggingface.co":
            return httpx.Response(200, json=[{"id": "org/model", "trendingScore": 9, "pipeline_tag": "text-generation"},
                                             {"id": "x/NSFW-gen", "trendingScore": 99, "tags": ["not-for-all-audiences"]}])
        if h == "hn.algolia.com":
            return httpx.Response(200, json={"hits": [{"objectID": "1", "title": "Show HN: x", "points": 80, "num_comments": 3}]})
        return httpx.Response(503)

    items, errors = collect({"github_topics": []}, transport=httpx.MockTransport(handler))
    assert {i["source"] for i in items} == {"GitHub", "Hugging Face models", "Hugging Face spaces", "Show HN"}
    assert len(errors) == 1 and errors[0].startswith("arXiv")
    assert not any("NSFW" in i["title"] for i in items)


def test_email_application_uses_only_verified_answers_and_guards_attachments(tmp_path):
    from integrations.applications.email_submit import compose, make_email_submitter
    from workforce.policies.engine import IntegrationError
    app = {"answers_json": dumps({"short_bio": {"text": "I build voice agents.", "status": "verified"},
                                  "why_applying": {"text": "MODEL GUESS", "status": "draft_requires_your_review"}}),
           "documents_json": dumps([{"path": "documents/resume.pdf"}])}
    opp = {"title": "AI Intern", "organizer": "Acme", "raw_json": dumps({"raw_text": "Apply by emailing jobs@acme.test"}),
           "summary": "", "official_url": "https://acme.test/careers"}
    subject, body = compose(app, opp, {"name": "Mithun", "email": "m@x.test"})
    assert "I build voice agents." in body and "MODEL GUESS" not in body and subject.startswith("Application: AI Intern")

    class MB:
        def __init__(self):
            self.sent = []

        def send(self, to, subject, body, attachments=None):
            self.sent.append((to, [p.name for p in attachments]))
            return {"message_id": "g1", "evidence": "gmail id g1"}

    data = tmp_path / "data"
    (data / "documents").mkdir(parents=True)
    (data / "documents" / "resume.pdf").write_bytes(b"%PDF")
    mb = MB()
    submit = make_email_submitter(mb, lambda: {"name": "Mithun"}, str(data))
    res = submit(app, opp)
    assert res["confirmation_id"] == "g1" and mb.sent[0] == ("jobs@acme.test", ["resume.pdf"])
    import pytest
    escape = {**app, "documents_json": dumps([{"path": "../../etc/passwd"}])}
    with pytest.raises(IntegrationError):
        submit(escape, opp)


def test_source_keyword_filter():
    from workforce.sources.parsing import keep_item
    item = {"title": "PyCon India 2026", "body": "Bengaluru, Nov", "tags": []}
    assert keep_item(item, {}) and keep_item(item, {"include_any": ["bengaluru"]})
    assert not keep_item(item, {"include_any": ["berlin"]})


def test_one_approval_is_enough_to_send_an_email_application(env, services):
    from tests.integration.test_specialists import add_opp, write_profile
    from workforce.owner_actions import confirm_opportunity
    write_profile(env, {"degree": {"value": "B.E. Computer Science (undergraduate)", "verified": True},
                        "citizenship": {"value": "Indian", "verified": True}},
                  answers={"short_bio": {"value": "CS undergraduate building agent tools.", "verified": True},
                           "why_applying": {"value": "I want to ship AI tools for Indian students.", "verified": True}},
                  documents={"resume": {"path": "documents/resume.pdf", "verified": True}})
    sent = []
    services.extra["application_submitters"] = {"email": lambda app, opp: sent.append(opp["id"]) or
                                                {"confirmation_id": "g1", "evidence": "gmail id g1"}}
    raw = dumps({"raw_text": "Apply by emailing your resume to jobs@acme.test"})
    mine = add_opp(env.db, raw_json=raw, verification_status="unverified")
    other = add_opp(env.db, id="opp_y", fingerprint="fp_y", raw_json=raw)       # verified by QC, but you never approved it
    confirm_opportunity(env.db, mine, by="telegram:owner")
    services.queue.enqueue(TaskSpec(bot_id="applications", kind="evaluate_opportunity", payload={"opportunity_id": other}))
    run(services, ["applications"], rounds=20)
    assert sent == [mine]
    assert env.db.scalar("SELECT state FROM applications WHERE opportunity_id=?", (mine,)) == "SUBMITTED"
    assert env.db.scalar("SELECT state FROM applications WHERE opportunity_id=?", (other,)) == "REVIEW"
