"""Phase D (custom Telegram agents, the built-in Assistant) and the v2 extras: phone calls,
calendar sync, referrals, debriefs, offers, drive radar, dossier. Fakes only, no network."""
import json

import httpx
import pytest

from tests.helpers import ScriptedProvider
from tests.integration.test_pipeline_v2 import add_opp
from tests.integration.test_specialists import run, write_profile
from workforce.contracts import TaskSpec
from workforce.core.db import loads
from workforce.core.util import now

TOKEN = "123456789:AAH" + "x" * 32
TOKEN2 = "987654321:BBH" + "y" * 32
OWNER = 4242


@pytest.fixture
def key(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("NOESIS_SECRET_KEY", Fernet.generate_key().decode())


def fake_me(username="scout_bot"):
    return lambda tok: {"is_bot": True, "username": username, "id": int(tok.split(":")[0]), "first_name": "Scout"}


class FakeAgentBots:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send(self, agent_id, text, buttons=None):
        self.sent.append((agent_id, text))
        return {"evidence": f"fake-agent-message-{len(self.sent)}"}


class FakeClient:
    def __init__(self, updates=None, audio=b"OggS fake"):
        self.updates, self.audio = list(updates or []), audio
        self.sent: list[str] = []
        self.typed = 0

    def get_updates(self, offset, timeout=25):
        out, self.updates = self.updates, []
        return out

    def send(self, chat_id, text, buttons=None):
        self.sent.append(text)
        return {"message_id": len(self.sent)}

    def typing(self, chat_id):
        self.typed += 1

    def download(self, file_id, max_bytes=0):
        return self.audio


class FakePhone:
    provider = "twilio"

    def __init__(self):
        self.calls: list[str] = []

    def call(self, message):
        self.calls.append(message)
        return {"call_id": f"CA{len(self.calls)}", "evidence": f"twilio call CA{len(self.calls)} to +91•••••••789"}

    def masked_owner(self):
        return "+91•••••••789"


def use_model(services, responses):
    prov = ScriptedProvider(responses=responses)
    services.router._providers = {"gemini": prov}
    services.router._explicit = True
    return prov


def make_agent(env, **kw):
    from workforce.custom_agents import create_agent
    args = dict(name="Scout", task="Find me Bengaluru ML internships every morning.", token=TOKEN, emoji="🔭",
                template="scout", tools=["search_opportunities", "create_reminder"], schedule=None, me=fake_me())
    args.update(kw)
    return create_agent(env.db, **args)


def msg(text=None, sender=OWNER, chat_type="private", update_id=1, **extra):
    m = {"from": {"id": sender}, "chat": {"id": sender, "type": chat_type}, **extra}
    if text is not None:
        m["text"] = text
    return {"update_id": update_id, "message": m}


def set_policy(env, services, name, **values):
    """Policies are loaded once per process (bots can't change them); a test edits the file and reloads."""
    import yaml
    from workforce.policies.engine import Policies
    p = env.policies / f"{name}.yaml"
    data = yaml.safe_load(p.read_text())
    data.update(values)
    p.write_text(yaml.safe_dump(data))
    services.gateway.engine.p = Policies()


# ---- tokens and CRUD ----------------------------------------------------------------
def test_tokens_are_encrypted_masked_and_never_returned(env, key):
    a = make_agent(env)
    row = env.db.one("SELECT * FROM custom_agents WHERE id=?", (a["id"],))
    assert TOKEN not in json.dumps(a) and "telegram_token_enc" not in a and a["has_token"]
    assert row["telegram_token_enc"] and TOKEN not in row["telegram_token_enc"]
    assert row["token_hint"] == "123456789:••••" and a["telegram_username"] == "scout_bot"
    from workforce.secrets import decrypt
    assert decrypt(row["telegram_token_enc"]) == TOKEN
    from workforce.core.util import redact
    assert TOKEN not in redact(f"error calling with {TOKEN} now")


def test_create_rejects_bad_input(env, key, monkeypatch):
    from workforce.custom_agents import AgentError, create_agent, delete_agent, ensure_builtin
    make_agent(env)
    with pytest.raises(AgentError, match="already one of your agents"):
        make_agent(env, token=TOKEN2)
    with pytest.raises(AgentError, match="Unknown tools"):
        make_agent(env, token=TOKEN2, tools=["send_whatsapp"], me=fake_me("other"))
    with pytest.raises(AgentError, match="BotFather token"):
        make_agent(env, token="not-a-token", me=fake_me("other"))
    with pytest.raises(AgentError, match="15m"):
        make_agent(env, token=TOKEN2, schedule="every 5m", me=fake_me("other"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN2)
    with pytest.raises(AgentError, match="main NOESIS bot"):
        create_agent(env.db, name="Xy", task="a long enough task", token=TOKEN2, me=fake_me("other"))
    ensure_builtin(env.db)
    with pytest.raises(AgentError, match="turned off"):
        delete_agent(env.db, "assistant")


def test_secret_key_required(env, monkeypatch):
    from workforce.secrets import SecretKeyMissing
    monkeypatch.delenv("NOESIS_SECRET_KEY", raising=False)
    with pytest.raises(SecretKeyMissing):
        make_agent(env)


def test_update_schedule_and_list(env, key):
    from workforce.custom_agents import list_agents, parse_schedule, update_agent
    a = make_agent(env)
    u = update_agent(env.db, a["id"], schedule="at 08:30 on mon,thu", tools=["today_shortlist"], enabled=False)
    assert u["schedule"] == "at 08:30 on mon,thu" and u["next_run"] > now() and u["tools"] == ["today_shortlist"]
    assert not u["enabled"]
    assert parse_schedule("every 6h")["every_s"] == 21600 and parse_schedule("") is None
    names = [x["id"] for x in list_agents(env.db)]
    assert names[0] == "assistant" and a["id"] in names


# ---- the poller: owner only, per bot ------------------------------------------------------
def test_poller_accepts_only_the_owner_in_private(env, key):
    from services.agents.main import AgentPoller
    a = make_agent(env)
    row = env.db.one("SELECT * FROM custom_agents WHERE id=?", (a["id"],))
    p = AgentPoller(env.db, OWNER, data_dir=str(env.tmp / "data"))
    client = FakeClient([msg("hi", sender=999, update_id=5), msg("hi", chat_type="group", update_id=6),
                         msg("/start", update_id=7), msg("any ML internships?", update_id=8)])
    assert p.poll_once(row, client) == 4
    assert len(client.sent) == 1 and "Scout here" in client.sent[0] and "My job" in client.sent[0]
    tasks = env.db.all("SELECT kind, payload_json FROM tasks WHERE bot_id='custom_agents'")
    assert [t["kind"] for t in tasks] == ["chat"] and loads(tasks[0]["payload_json"])["text"] == "any ML internships?"
    assert env.db.scalar("SELECT COUNT(*) FROM audit_log WHERE event='telegram.unauthorized'") == 2
    assert env.db.scalar("SELECT updates_offset FROM custom_agents WHERE id=?", (a["id"],)) == 9
    # redelivered update: idempotent
    p.handle_update(row, msg("any ML internships?", update_id=8))
    assert env.db.scalar("SELECT COUNT(*) FROM tasks WHERE bot_id='custom_agents'") == 1


def test_poller_saves_voice_notes_for_the_worker(env, key):
    from services.agents.main import AgentPoller
    a = make_agent(env)
    row = env.db.one("SELECT * FROM custom_agents WHERE id=?", (a["id"],))
    p = AgentPoller(env.db, OWNER, data_dir=str(env.tmp / "data"))
    client = FakeClient()
    assert p.handle_update(row, msg(update_id=11, voice={"file_id": "f1", "duration": 20}), client) is None
    payload = loads(env.db.scalar("SELECT payload_json FROM tasks WHERE bot_id='custom_agents'"))
    assert payload["voice_path"].endswith("11.ogg") and client.typed == 1
    assert "over 5 minutes" in p.handle_update(row, msg(update_id=12, voice={"file_id": "f2", "duration": 900}), client)


def test_supervisor_reload_starts_and_stops_threads(env, key, monkeypatch):
    from services.agents import main as m
    from workforce.custom_agents import update_agent
    started = []
    monkeypatch.setattr(m.Supervisor, "_run", lambda self, agent, stop: started.append(agent["id"]) or stop.wait(5))
    a = make_agent(env)
    sup = m.Supervisor(env.db, m.AgentPoller(env.db, OWNER), client_factory=lambda *x: FakeClient())
    assert sup.reload()["started"] == 1 and sup.reload()["started"] == 0
    update_agent(env.db, a["id"], enabled=False)
    st = sup.reload()
    assert st["stopped"] == 1 and st["running"] == 0 and started == [a["id"]]


# ---- the engine ---------------------------------------------------------------------------------
def test_agent_uses_allowed_tools_and_replies_in_its_own_bot(env, key, services):
    a = make_agent(env)
    add_opp(env.db, title="ML Intern, Bengaluru", type="internship", organizer="Acme", verification_status="verified",
            relevance_score=0.8, stage="found")
    bots = FakeAgentBots()
    services.extra["custom_bots"] = bots
    prov = use_model(services, [
        json.dumps({"reply": "Looking…", "actions": [{"tool": "search_opportunities", "args": {"query": "ml"}},
                                                     {"tool": "draft_email", "args": {"to": "x@y.com"}},
                                                     {"tool": "call_me", "args": {"message": "hi"}}]}),
        json.dumps({"reply": "Found one: ML Intern at Acme.", "actions": []}),
    ])
    services.queue.enqueue(TaskSpec(bot_id="custom_agents", kind="chat", payload={"agent_id": a["id"], "text": "ML internships?"}))
    run(services, ["custom_agents"])
    assert bots.sent == [(a["id"], "Found one: ML Intern at Acme.")]
    assert prov.calls == 2
    refused = env.db.all("SELECT details_json FROM audit_log WHERE event='custom_agent.tool_refused'")
    assert sorted(loads(r["details_json"])["tool"] for r in refused) == ["call_me", "draft_email"]
    turns = env.db.all("SELECT role, text FROM custom_agent_messages WHERE agent_id=? ORDER BY id", (a["id"],))
    assert [t["role"] for t in turns] == ["owner", "agent"]
    act = env.db.one("SELECT * FROM external_actions WHERE action='telegram.custom_agent_reply'")
    assert act["status"] == "succeeded" and act["target"] == "owner"


def test_write_tool_runs_and_reports_without_a_second_call(env, key, services):
    a = make_agent(env)
    services.extra["custom_bots"] = bots = FakeAgentBots()
    prov = use_model(services, [json.dumps({"reply": "Done.", "actions": [
        {"tool": "create_reminder", "args": {"when": "in 2h", "what": "apply to Acme"}}]})])
    services.queue.enqueue(TaskSpec(bot_id="custom_agents", kind="chat", payload={"agent_id": a["id"], "text": "remind me"}))
    run(services, ["custom_agents"])
    assert prov.calls == 1 and "reminder set" in bots.sent[0][1]
    assert env.db.scalar("SELECT title FROM reminders") == "apply to Acme"


def test_disabled_agent_does_nothing(env, key, services):
    from workforce.custom_agents import update_agent
    a = make_agent(env)
    update_agent(env.db, a["id"], enabled=False)
    services.extra["custom_bots"] = bots = FakeAgentBots()
    use_model(services, [json.dumps({"reply": "hi", "actions": []})])
    services.queue.enqueue(TaskSpec(bot_id="custom_agents", kind="chat", payload={"agent_id": a["id"], "text": "hi"}))
    run(services, ["custom_agents"])
    assert bots.sent == []


def test_unmapped_text_goes_to_builtin_assistant_without_loops(env, services):
    use_model(services, ["none", json.dumps({"reply": "Sure, noted.", "actions": [
        {"tool": "ask_main_agent", "args": {"text": "do the weird thing"}}]}), "none"])
    services.queue.enqueue(TaskSpec(bot_id="orchestrator", kind="objective", payload={"text": "what should I eat?"}))
    run(services, ["orchestrator", "custom_agents"])
    tg = services.extra["telegram"]
    assert any(t.startswith("🧑‍💼 Sure, noted.") for t in tg.sent)
    # the handed-over request did not bounce back to the assistant
    assert env.db.scalar("SELECT COUNT(*) FROM tasks WHERE bot_id='custom_agents'") == 1
    assert any("couldn't act on that request" in t for t in tg.sent)


def test_scheduled_runs_fire_and_advance(env, key, services):
    from workforce.scheduler import Scheduler
    a = make_agent(env, schedule="every 6h")
    env.db.execute("UPDATE custom_agents SET next_run=? WHERE id=?", (now() - 10, a["id"]))
    fired = Scheduler(env.db).custom_agent_runs(now())
    assert len(fired) == 1
    assert env.db.scalar("SELECT next_run FROM custom_agents WHERE id=?", (a["id"],)) > now()
    assert Scheduler(env.db).custom_agent_runs(now()) == []
    services.extra["custom_bots"] = bots = FakeAgentBots()
    use_model(services, ["NOTHING"])
    run(services, ["custom_agents"])
    assert bots.sent == []


def test_voice_note_is_transcribed_then_deleted(env, key, services, monkeypatch):
    from workforce import stt
    a = make_agent(env)
    services.extra["custom_bots"] = bots = FakeAgentBots()
    d = env.tmp / "data" / "voice" / a["id"]
    d.mkdir(parents=True)
    f = d / "3.ogg"
    f.write_bytes(b"OggS")
    monkeypatch.setattr(stt, "transcribe", lambda p: "binary search on rotated array")
    prov = use_model(services, [json.dumps({"reply": "8/10, good.", "actions": []})])
    services.queue.enqueue(TaskSpec(bot_id="custom_agents", kind="chat", payload={"agent_id": a["id"], "voice_path": str(f)}))
    run(services, ["custom_agents"])
    assert bots.sent[0][1] == "8/10, good." and not f.exists() and prov.calls == 1
    assert "(voice note) binary search" in env.db.scalar("SELECT text FROM custom_agent_messages WHERE role='owner'")


def test_stt_request_shape(monkeypatch, tmp_path):
    from workforce import stt
    seen = {}

    def handler(req):
        seen["auth"] = req.headers["authorization"]
        seen["body"] = req.content
        return httpx.Response(200, json={"text": "hello there"})
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    f = tmp_path / "a.ogg"
    f.write_bytes(b"OggS")
    assert stt.transcribe(f, transport=httpx.MockTransport(handler)) == "hello there"
    assert seen["auth"] == "Bearer gsk_test" and b"whisper-large-v3-turbo" in seen["body"]


# ---- phone calls -----------------------------------------------------------------------------
def test_calls_are_off_by_default_and_fall_back_to_telegram(env, services):
    services.extra["phone"] = phone = FakePhone()
    services.queue.enqueue(TaskSpec(bot_id="personal_ops", kind="test_call", payload={}))
    run(services, ["personal_ops"])
    assert phone.calls == []
    assert any("No test call" in t and "phone calls are off" in t for t in services.extra["telegram"].sent)


def test_calls_follow_policy_caps_and_quiet_hours(env, services):
    set_policy(env, services, "phone", enabled=True, quiet_hours="off", max_calls_per_day=1, min_gap_minutes=0)
    services.extra["phone"] = phone = FakePhone()
    for _ in range(2):
        services.queue.enqueue(TaskSpec(bot_id="personal_ops", kind="test_call", payload={}))
        run(services, ["personal_ops"])
    assert len(phone.calls) == 1 and "test call from NOESIS" in phone.calls[0]
    assert env.db.scalar("SELECT COUNT(*) FROM phone_calls WHERE status='placed'") == 1
    assert any("daily call cap" in t for t in services.extra["telegram"].sent)
    set_policy(env, services, "phone", enabled=True, quiet_hours="00:00-23:59", max_calls_per_day=5, min_gap_minutes=0)
    services.queue.enqueue(TaskSpec(bot_id="personal_ops", kind="test_call", payload={}))
    run(services, ["personal_ops"])
    assert len(phone.calls) == 1


def test_interview_reminder_rings_the_phone(env, services):
    set_policy(env, services, "phone", enabled=True, quiet_hours="off", min_gap_minutes=0)
    services.extra["phone"] = phone = FakePhone()
    oid = add_opp(env.db, title="SDE Intern", type="internship", organizer="Acme", stage="interview_1")
    env.db.execute("INSERT INTO reminders(id, title, due_at, source, ref_id, dedupe_key, created_at) VALUES "
                   "('rem_1','Interview 1 for SDE Intern in 1 hour',?, 'telegram:owner', ?, ?, ?)",
                   (now() - 5, oid, f"stage:{oid}:interview_1:{int(now())}:1", now()))
    services.queue.enqueue(TaskSpec(bot_id="personal_ops", kind="send_due_reminders", payload={}))
    run(services, ["personal_ops"])
    assert len(phone.calls) == 1 and "Interview 1 for SDE Intern" in phone.calls[0]


def test_twilio_client_dials_only_the_owner(monkeypatch):
    from integrations.phone.client import PhoneClient
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        seen["body"] = req.content.decode()
        return httpx.Response(201, json={"sid": "CA123"})
    c = PhoneClient("twilio", "AC1", "tok", "+14155550100", "+919876543210", transport=httpx.MockTransport(handler))
    out = c.call("Interview with <Acme> & co in 1 hour")
    assert out["call_id"] == "CA123" and "+91" in out["evidence"] and "9876543210" not in out["evidence"]
    assert "Accounts/AC1/Calls.json" in seen["url"]
    assert "To=%2B919876543210" in seen["body"] and "%26lt%3BAcme%26gt%3B" in seen["body"]
    from workforce.policies.engine import IntegrationRevoked
    with pytest.raises(IntegrationRevoked):
        PhoneClient("twilio", "AC1", "tok", "+14155550100", "98765")


# ---- calendar sync ------------------------------------------------------------------------------
class FakeCalendar:
    def __init__(self):
        self.events = []

    def create_event(self, summary, start_iso, end_iso, attendees=None, send_updates=False):
        assert not attendees and not send_updates
        self.events.append(summary)
        return {"event_id": f"ev{len(self.events)}", "evidence": f"calendar event ev{len(self.events)}"}


def test_calendar_sync_off_by_default_then_once_per_event(env, services):
    services.extra["calendar"] = cal = FakeCalendar()
    add_opp(env.db, title="SDE Intern", type="internship", organizer="Acme", stage="preparing", deadline=now() + 5 * 86400)
    oid = add_opp(env.db, title="ML Intern", type="internship", organizer="Beta", stage="online_assessment")
    env.db.execute("UPDATE opportunities SET next_event_at=? WHERE id=?", (now() + 86400, oid))
    services.queue.enqueue(TaskSpec(bot_id="personal_ops", kind="calendar_sync", payload={}))
    run(services, ["personal_ops"])
    assert cal.events == []
    set_policy(env, services, "calendar", sync_enabled=True)
    for i in range(2):
        services.queue.enqueue(TaskSpec(bot_id="personal_ops", kind="calendar_sync", payload={}, idempotency_key=f"c{i}"))
        run(services, ["personal_ops"])
    assert sorted(cal.events) == sorted(["⏰ Deadline: SDE Intern (Acme)", "🎯 Online assessment: Beta"])


# ---- referrals, debriefs, offers, drives, dossier ----------------------------------------------------
def test_referrals_are_links_and_a_note_from_verified_facts(env):
    from workforce.referrals import plan, render
    write_profile(env, {"college": {"value": "BMS College of Engineering", "verified": True},
                        "graduation_year": {"value": "2027", "verified": True},
                        "skills": {"value": "Python, PyTorch, React, SQL", "verified": True},
                        "cgpa": {"value": "9.9", "verified": False}})
    p = plan(env.db, "Razorpay")
    assert all(link["url"].startswith(("https://www.linkedin.com/search/", "https://www.google.com/search")) for link in p["links"])
    assert "BMS+College" in p["links"][0]["url"]
    assert len(p["note"]) <= 300 and "BMS College of Engineering" in p["note"] and "9.9" not in p["note"]
    assert "I never open LinkedIn" in render(p)


def test_debrief_bank_and_prep(env):
    from workforce.debrief import bank, debrief_from_text, topic_of
    from workforce.owner_actions import ref
    oid = add_opp(env.db, title="SDE Intern", type="internship", organizer="Acme", stage="interview_1")
    r = debrief_from_text(env.db, f"{ref(oid)} reverse a linked list; design a URL shortener; tell me about yourself")
    assert r["company"] == "Acme" and r["logged"] == 3 and r["stage"] == "interview_1"
    assert topic_of("Design a URL shortener") == "system design"
    assert {q["topic"] for q in bank(env.db, "acme")} == {"dsa", "system design", "behavioural"}
    from workforce.prep import company_questions
    assert len(company_questions(env.db, "Acme")) == 3


def test_offers_parse_and_rank(env):
    from workforce.offers import add_offer, compare, parse_offer
    from workforce.owner_actions import OwnerActionError
    add_offer(env.db, parse_offer("Razorpay | 50k | 6 | Bengaluru | hybrid | likely | 4"))
    add_offer(env.db, parse_offer("TinyCo | 15000 | 3 | Pune | onsite | no | 2"))
    rows = compare(env.db)
    assert rows[0]["company"] == "Razorpay" and rows[0]["stipend_month_inr"] == 50000 and rows[0]["score"] > rows[1]["score"]
    with pytest.raises(OwnerActionError):
        parse_offer("X | 1 | 1 | Y | spaceship")


def test_drive_radar_alerts_once_per_window(env, services):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from workforce.drives import load_drives, phase
    ds = {d["id"]: d for d in load_drives()}
    assert {"google_step", "amazon_wow", "flipkart_grid", "jpmc_code_for_good"} <= set(ds)
    month = datetime.now(ZoneInfo("Asia/Kolkata")).month
    (env.config / "drives.yaml").write_text(
        f"drives:\n  - {{id: t1, name: Test Drive, organizer: T, opens: [{month}], url: 'https://example.org/d', watch: false}}\n")
    assert phase(load_drives()[0]) == "open"
    for i in range(2):
        services.queue.enqueue(TaskSpec(bot_id="opportunity_scout", kind="drive_radar", payload={}, idempotency_key=f"d{i}"))
        run(services, ["opportunity_scout"])
    assert sum("Test Drive is usually open" in t for t in services.extra["telegram"].sent) == 1


def test_dossier_from_own_data_offline(env, services):
    from workforce.debrief import log_debrief
    add_opp(env.db, title="SDE Intern", type="internship", organizer="Acme", stage="applied")
    log_debrief(env.db, "Acme", "interview_1", ["Reverse a linked list"])
    services.extra.pop("fetcher", None)
    services.queue.enqueue(TaskSpec(bot_id="applications", kind="company_dossier", payload={"company": "Acme"}))
    run(services, ["applications"])
    text = next(t for t in services.extra["telegram"].sent if t.startswith("🏢 Acme"))
    assert "SDE Intern" in text and "Reverse a linked list" in text and "Sources: your NOESIS data." in text


def test_telegram_extras_commands(env, key):
    from services.telegram.bot import CommandHandler
    h = CommandHandler(env.db, owner_id=OWNER)
    send = lambda t, i=1: h.handle(msg(t, update_id=i))  # noqa: E731
    assert "Referrals at Zeta" in send("/referrals Zeta")
    assert "Added Asha" in send("/referral add Zeta | Asha | https://www.linkedin.com/in/asha")
    assert "Logged 2 questions" in send("/debrief Zeta two sum; LRU cache design")
    assert "Zeta: 2 questions" in send("/questions")
    assert "saved" in send("/offer Zeta | 40000 | 6 | Bengaluru | remote | yes | 5")
    assert "Zeta: " in send("/offers")
    assert "Google STEP" in send("/drives")
    assert "Assistant (main bot)" in send("/agents")
    assert "Placing a test call" in send("/call test")
