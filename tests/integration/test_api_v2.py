"""v2 API: same auth + CSRF rules, agents CRUD never returns tokens, pipeline drag, resume
upload + one-page PDF, extras. No network (board tests use a fake fetcher)."""
import json

import pytest

from tests.integration.test_api import H, client, login  # noqa: F401  (fixture)
from tests.integration.test_pipeline_v2 import add_opp

TOKEN = "123456789:AAH" + "x" * 32


@pytest.fixture
def owner(client, monkeypatch):  # noqa: F811
    from cryptography.fernet import Fernet
    monkeypatch.setenv("NOESIS_SECRET_KEY", Fernet.generate_key().decode())
    login(client)
    return client


def test_v2_routes_need_login_and_csrf(client):  # noqa: F811
    for path in ("/api/home", "/api/internships", "/api/pipeline", "/api/agents", "/api/boards", "/api/offers", "/api/resume"):
        assert client.get(path).status_code == 401, path
    login(client)
    assert client.post("/api/offers", json={"company": "X"}).status_code == 403
    assert client.post("/api/offers", json={"company": "X"}, headers=H).status_code == 200


def test_agents_crud_never_exposes_the_token(owner, monkeypatch):
    import workforce.custom_agents as ca
    monkeypatch.setattr(ca, "validate_token", lambda tok, me=None: {"username": "my_scout_bot", "id": 1, "name": "S"})
    r = owner.post("/api/agents", headers=H, json={"name": "Scout", "token": TOKEN, "task": "Find Bengaluru ML internships daily",
                                                   "tools": ["search_opportunities"], "schedule": "at 08:30", "template": "scout"})
    assert r.status_code == 200, r.text
    a = r.json()
    assert TOKEN not in r.text and a["token_hint"] == "123456789:••••" and a["next_run"]
    lst = owner.get("/api/agents")
    assert TOKEN not in lst.text and "telegram_token_enc" not in lst.text
    assert {"scout", "apply", "assistant", "coach", "inbox"} <= set(lst.json()["templates"])
    r = owner.patch(f"/api/agents/{a['id']}", headers=H, json={"tools": ["nuke_everything"]})
    assert r.status_code == 400 and "Unknown tools" in r.json()["detail"]
    assert owner.patch(f"/api/agents/{a['id']}", headers=H, json={"enabled": False}).json()["enabled"] == 0
    assert owner.delete("/api/agents/assistant", headers=H).status_code == 400
    assert owner.delete(f"/api/agents/{a['id']}", headers=H).status_code == 200


def test_pipeline_drag_and_home(owner, env):
    oid = add_opp(env.db, title="SDE Intern", type="internship", organizer="Acme", stage="applied")
    r = owner.post(f"/api/internships/{oid}/stage", headers=H, json={"stage": "oa", "event_at": 4102444800})
    assert r.status_code == 200 and r.json()["stage"] == "online_assessment"
    cols = {c["stage"]: c["items"] for c in owner.get("/api/pipeline").json()["columns"]}
    assert [o["id"] for o in cols["online_assessment"]] == [oid]
    assert owner.post(f"/api/internships/{oid}/stage", headers=H, json={"stage": "banana"}).status_code == 400
    home = owner.get("/api/home").json()
    assert home["upcoming"][0]["id"] == oid and "streak" in home
    detail = owner.get(f"/api/internships/{oid}").json()
    assert [h["stage"] for h in detail["history"]][-1] == "online_assessment"


def test_resume_upload_and_one_page_pdf(owner):
    text = "\n".join(["Jane Doe", "Built a FastAPI service in Python serving 10k requests a day for the college fest.",
                      "Trained a PyTorch model to classify crop disease with 92% accuracy on 8,000 images.",
                      "Led a team of four to build a React dashboard for hostel complaints used by 600 students."] * 2)
    r = owner.post("/api/resume", headers=H, files={"file": ("resume.md", text.encode(), "text/markdown")})
    assert r.status_code == 200 and r.json()["version"] == "v1"
    pdf = owner.get("/api/resume/pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    bad = owner.post("/api/resume", headers=H, files={"file": ("x.docx", b"zz", "application/octet-stream")})
    assert bad.status_code == 400


def test_board_add_requires_a_working_test(owner, monkeypatch):
    import services.api.v2 as v2
    from workforce.sources import boards
    monkeypatch.setattr(boards, "test_board", lambda f, b: {"ok": False, "error": "HTTP 404", "items": 0, "total": 0, "sample": []})
    monkeypatch.setattr(v2, "_fetcher", lambda: None)
    r = owner.post("/api/boards", headers=H, json={"company": "Nope", "ats": "lever", "slug": "nope"})
    assert r.status_code == 400 and "didn't answer" in r.json()["detail"]
    monkeypatch.setattr(boards, "test_board", lambda f, b: {"ok": True, "items": 3, "total": 40, "sample": [], "url": "u"})
    r = owner.post("/api/boards", headers=H, json={"company": "Meesho", "ats": "lever", "slug": "meesho-new"})
    assert r.status_code == 200 and r.json()["id"] == "board_lever_meesho_new" and r.json()["last_test_items"] == 3
    assert owner.delete("/api/boards/board_lever_meesho_new", headers=H).json()["deleted"] is True
    assert owner.post("/api/boards", headers=H, json={"company": "X", "ats": "workday", "slug": "x"}).status_code == 422


def test_extras_endpoints(owner, env):
    assert owner.post("/api/referrals", headers=H, json={"company": "Zeta", "name": "Asha"}).status_code == 200
    plan = owner.get("/api/referrals", params={"q": "Zeta"}).json()["plan"]
    assert plan["company"] == "Zeta" and plan["existing"][0]["name"] == "Asha"
    assert owner.post("/api/debrief", headers=H, json={"company": "Zeta", "questions": ["two sum", "design a cache"]}).json()["logged"] == 2
    assert len(owner.get("/api/questions", params={"company": "zeta"}).json()["questions"]) == 2
    assert owner.post("/api/offers", headers=H, json={"company": "Zeta", "stipend_month_inr": 40000, "ppo": "likely"}).status_code == 200
    assert owner.get("/api/offers").json()["offers"][0]["score"] > 0
    assert any(d["id"] == "google_step" for d in owner.get("/api/drives").json())
    ph = owner.get("/api/phone").json()
    assert ph["policy"]["enabled"] is False and ph["calendar"]["sync_enabled"] is False
    assert json.dumps(owner.get("/api/settings").json()["policies"]["phone"])
