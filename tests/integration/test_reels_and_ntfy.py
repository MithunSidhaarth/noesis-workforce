"""ntfy push notifications, Instagram collaborators on Zernio, Cue topic routing
and ready-made reels queued one per slot. ntfy and Zernio are httpx.MockTransport
fakes; nothing here proves either service accepts a real call."""
import json
import shutil
import subprocess

import httpx
import pytest
import yaml

from tests.integration.test_specialists import run
from workforce.core.db import loads
from workforce.core.util import now

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def _ntfy(handler_log, status=200):
    def handler(req: httpx.Request):
        handler_log.append((req.url, json.loads(req.content), req.headers.get("authorization")))
        if status != 200:
            return httpx.Response(status, text="no")
        return httpx.Response(200, json={"id": "abc123", "event": "message"})
    return httpx.MockTransport(handler)


def test_ntfy_pushes_only_the_headline_with_tag_and_priority():
    from integrations.ntfy.client import NtfyClient
    from workforce.policies.engine import IntegrationRevoked
    log = []
    push = NtfyClient("methxd", transport=_ntfy(log), full_text=False)
    res = push.push("📬 2 emails need you\n\nFrom recruiter@corp.com: Interview on Friday", urgent=True)
    url, body, auth = log[0]
    assert str(url) == "https://ntfy.sh" and auth is None
    assert body["topic"] == "methxd" and body["title"] == "2 emails need you" and body["tags"] == ["mailbox_with_mail"]
    assert body["priority"] == 4 and "recruiter@corp.com" not in json.dumps(body)   # private detail stays in Telegram
    assert res["message_id"] == "abc123"
    full = NtfyClient("methxd", transport=_ntfy(log), full_text=True, token="tk")
    full.push("💡 Ideas\n\nthree new ones")
    assert log[-1][1]["message"] == "three new ones" and log[-1][2] == "Bearer tk" and log[-1][1]["priority"] == 3
    with pytest.raises(IntegrationRevoked):
        NtfyClient("bad topic!")
    with pytest.raises(IntegrationRevoked):
        NtfyClient("methxd", transport=_ntfy([], status=403)).push("x")


def test_notifications_go_to_telegram_and_ntfy(env, services):
    from integrations.ntfy.client import NtfyClient
    from workforce.agents.base import AgentContext
    log = []
    services.extra["ntfy"] = NtfyClient("methxd", transport=_ntfy(log))
    ctx = AgentContext(services.registry.get("personal_ops"), env.db, services.queue, services.bus, services.gateway,
                       services.router, "t", services=services.extra)
    ctx.notify_owner("🔔 Reminder: submit Zyoris\n\nCloses Sat 19 Oct.", key="n1", urgent=True)
    assert services.extra["telegram"].sent == ["🔔 Reminder: submit Zyoris\n\nCloses Sat 19 Oct."]
    assert log and log[0][1]["title"] == "Reminder: submit Zyoris"
    assert env.db.scalar("SELECT status FROM notifications WHERE key='n1'") == "sent"
    # ntfy alone (no Telegram) still delivers
    services.extra.pop("telegram")
    ctx.notify_owner("⚠️ Gmail needs you to sign in again", key="n2", urgent=True)
    assert len(log) == 2 and env.db.scalar("SELECT status FROM notifications WHERE key='n2'") == "sent"


def test_zernio_invites_instagram_collaborators_on_reels():
    from integrations.social.publishers import ZernioPublisher
    bodies = []

    def handler(req: httpx.Request):
        if req.url.path.endswith("/media/presign"):
            return httpx.Response(200, json={"uploadUrl": "https://store.test/up", "publicUrl": "https://cdn.test/r.mp4"})
        if req.url.host == "store.test":
            return httpx.Response(200)
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json={"post": {"_id": "p1", "platforms": [{"platform": "instagram", "status": "published"}]}})

    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".mp4") as f:
        f.write(b"x")
        f.flush()
        ig = ZernioPublisher("acc1", "instagram", api_key="k", base_url="https://z.test/api/v1",
                             transport=httpx.MockTransport(handler), collaborators=["@cue.notch"])
        ig.publish_reel_file(f.name, "caption")
    assert bodies[0]["platforms"][0]["platformSpecificData"] == {"collaborators": ["cue.notch"]}
    li = ZernioPublisher("acc2", "linkedin", api_key="k", collaborators=["cue.notch"])
    assert li.collaborators == []


def test_cue_work_goes_to_the_cue_brand_on_instagram_and_still_to_linkedin():
    from workforce.brands import brands_for
    brands = [{"id": "msthinks", "topics": [], "channels": {"instagram": {}}},
              {"id": "me", "topics": [], "channels": {"linkedin": {}}},
              {"id": "cue", "topics": ["cue", "tock"], "channels": {"instagram": {}}}]
    pick = lambda t: sorted((b["id"], p) for b in brands_for(t, brands) for p in b["channels"])  # noqa: E731
    assert pick("gave Tock a wink animation in Cue") == [("cue", "instagram"), ("me", "linkedin")]
    assert pick("a barbecue rescue story about attention") == [("me", "linkedin"), ("msthinks", "instagram")]


def _video(path, w, h, secs=4):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=0x101418:s={w}x{h}:d={secs}",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)


@needs_ffmpeg
def test_ready_reels_queue_one_per_slot_pass_qc_and_publish_with_collab(env, services):
    from workforce.reels import queue_reels, upcoming
    folder = env.config.parent / "reels"
    folder.mkdir()
    _video(folder / "a.mp4", 1080, 1920)
    _video(folder / "b.mp4", 1920, 1080)       # landscape: QC must stop it
    (folder / "manifest.yaml").write_text(yaml.safe_dump({"brand": "cue", "series": "cue-launch", "reels": [
        {"file": "a.mp4", "title": "Approve your agent from the notch", "caption": "Your agent asks. Tock answers.\nLink in bio. #windows"},
        {"file": "b.mp4", "caption": "wrong shape"},
        {"file": "a.mp4", "caption": "already on Instagram", "posted": True}]}))
    (env.config / "brands.yaml").write_text(yaml.safe_dump({"brands": [
        {"id": "cue", "name": "Cue", "topics": ["cue"], "channels": {"instagram": {"zernio_account": "acc1", "collaborators": ["cue.notch"]}}}]}))
    p = env.policies / "publishing.yaml"
    pol = yaml.safe_load(p.read_text())
    pol.update(enabled=True, accounts_allowlist=["zernio:acc1"])
    p.write_text(yaml.safe_dump(pol))
    from tests.helpers import FakeTelegram
    from workforce.model_router.router import ModelRouter
    from workforce.runtime import Services
    services = Services(env.db, router=ModelRouter(env.db, providers={}), extra={"telegram": FakeTelegram()})

    out = queue_reels(env.db, folder, by="t", every_hours=3, window="00:00-23:59")
    assert len(out) == 2 and out[0]["at"] > now()
    assert out[1]["at"] - out[0]["at"] in (3 * 3600, None) or out[1]["at"] - out[0]["at"] > 3 * 3600
    assert queue_reels(env.db, folder, by="t", window="00:00-23:59")[0]["new"] is False            # re-running doesn't double-queue
    assert [r["title"] for r in upcoming(env.db)][0] == "Approve your agent from the notch"
    run(services, ["content_studio", "quality"], rounds=10)
    assert env.db.scalar("SELECT COUNT(*) FROM content_items") == 0         # nothing before its slot

    class Pub:
        account_id, sent = "zernio:acc1", []

        def configured(self):
            return True

        def publish_reel_file(self, path, caption, **_):
            self.sent.append((path, caption))
            return {"platform_id": "zernio:p1", "evidence": "fake"}

    services.extra["publishers"] = {"instagram:cue": Pub()}
    env.db.execute("UPDATE tasks SET run_after=? WHERE kind='post_ready_reel'", (now() - 1,))
    run(services, ["content_studio", "quality"], rounds=40)
    rows = {loads(r["assets_json"])[0]["resolution"]: r for r in env.db.all("SELECT * FROM content_items")}
    assert rows["1080x1920"]["state"] == "PUBLISHED" and rows["1080x1920"]["brand"] == "cue"
    assert Pub.sent[0][1].startswith("Your agent asks") and "/media/ready/" in Pub.sent[0][0]
    assert rows["1920x1080"]["state"] == "BLOCKED"


def test_reel_slots_are_three_hours_apart_inside_the_posting_window(env):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from workforce.reels import slots
    tz = ZoneInfo("Asia/Kolkata")
    start = datetime(2026, 10, 16, 19, 50, tzinfo=tz)
    got = [datetime.fromtimestamp(t, tz).strftime("%d %H:%M") for t in slots(4, 3, "09:00-23:00", start=start)]
    assert got == ["16 20:00", "16 23:00", "17 09:00", "17 12:00"]


@needs_ffmpeg
def test_refill_makes_new_reels_with_unused_scripts_only(env, services):
    (env.config / "brands.yaml").write_text(yaml.safe_dump({"brands": [
        {"id": "cue", "name": "Cue", "topics": ["cue"], "reels": {"every_hours": 3, "window": "00:00-23:59", "keep_queued": 2},
         "channels": {"instagram": {"zernio_account": "acc1"}}}]}))
    bank = env.config.parent / "scripts.yaml"
    sc = lambda hook: {"hook": hook, "cta": "Cue.", "caption": f"{hook}\nLink in bio. #windows",  # noqa: E731
                       "beats": [{"text": "one", "clip": "hover", "secs": 1.5}, {"text": "two", "clip": "done", "secs": 1.5}]}
    bank.write_text(yaml.safe_dump({"scripts": [sc("Meet Tock"), sc("Meet Tock!"), sc("Approve from the notch")]}))
    from workforce.contracts import TaskSpec
    for n in range(3):
        services.queue.enqueue(TaskSpec(bot_id="content_studio", kind="refill_reels", payload={"brand": "cue", "scripts": str(bank)},
                                        idempotency_key=f"refill{n}"))
        run(services, ["content_studio"], rounds=10)
    rows = env.db.all("SELECT run_after, payload_json FROM tasks WHERE kind='post_ready_reel' ORDER BY run_after")
    hooks = [loads(r["payload_json"])["hook"] for r in rows]
    assert hooks == ["Meet Tock", "Approve from the notch"]            # "Meet Tock!" counts as the same hook
    assert rows[1]["run_after"] - rows[0]["run_after"] == 3 * 3600
    assert all(loads(r["payload_json"])["path"].endswith(".mp4") for r in rows)
    # queue is full: nothing more is made; when it drains and the bank is empty, Mithun is told once
    env.db.execute("UPDATE tasks SET status='succeeded' WHERE kind='post_ready_reel'")
    services.queue.enqueue(TaskSpec(bot_id="content_studio", kind="refill_reels", payload={"brand": "cue", "scripts": str(bank)},
                                    idempotency_key="refill-last"))
    run(services, ["content_studio"], rounds=10)
    assert env.db.scalar("SELECT COUNT(*) FROM tasks WHERE kind='post_ready_reel'") == 2
    assert env.db.scalar("SELECT COUNT(*) FROM notifications WHERE key LIKE 'reels-empty:cue:%'") == 1


def test_template_drafts_never_publish_without_approval(env):
    from workforce.core.db import dumps
    p = env.policies / "publishing.yaml"
    pol = yaml.safe_load(p.read_text())
    pol.update(enabled=True, accounts_allowlist=["zernio:li"])
    p.write_text(yaml.safe_dump(pol))
    from tests.helpers import FakeTelegram
    from workforce.contracts import TaskSpec
    from workforce.model_router.router import ModelRouter
    from workforce.runtime import Services
    services = Services(env.db, router=ModelRouter(env.db, providers={}), extra={"telegram": FakeTelegram()})

    class Pub:
        account_id, sent = "zernio:li", []

        def configured(self):
            return True

        def publish_text(self, text):
            self.sent.append(text)
            return {"platform_id": "x", "evidence": "fake"}

    services.extra["publishers"] = {"linkedin": Pub()}
    for cid, method in (("tpl", "template"), ("mdl", "model")):
        env.db.execute("INSERT INTO content_items(id, title, platform, format, category, state, body, brief_json, script_json, "
                       "assets_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       (cid, cid, "linkedin", "post", "project_update", "SCHEDULED", f"body {cid}",
                        dumps({"qc": {"licenses_ok": True, "facts_verified": True}}), dumps({"method": method}), "[]", now(), now()))
        services.queue.enqueue(TaskSpec(bot_id="content_studio", kind="publish_content", payload={"content_id": cid}))
    run(services, ["content_studio"], rounds=10)
    assert Pub.sent == ["body mdl"]
    assert env.db.scalar("SELECT COUNT(*) FROM approvals WHERE status='pending' AND action='social.publish'") == 1
