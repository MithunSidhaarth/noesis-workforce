"""Control-plane API: auth, CSRF header, real DB state, approvals, settings."""
import pytest

from services.api.auth import LoginLimiter, hash_password, make_session, check_session

H = {"X-Noesis-Request": "1"}


@pytest.fixture
def client(env, monkeypatch):
    monkeypatch.setenv("NOESIS_ADMIN_PASSWORD_HASH", hash_password("correct horse", iterations=1000))
    monkeypatch.setenv("NOESIS_SESSION_SECRET", "x" * 40)
    monkeypatch.setenv("NOESIS_COOKIE_SECURE", "false")
    from fastapi.testclient import TestClient
    import services.api.app as api
    monkeypatch.setattr(api, "limiter", LoginLimiter(max_attempts=3, window_s=60))
    with TestClient(api.app) as c:
        yield c


def login(c):
    r = c.post("/api/auth/login", json={"password": "correct horse"})
    assert r.status_code == 200, r.text
    return r


def test_health_is_public_and_reads_db(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_everything_else_requires_login(client):
    for path in ("/api/overview", "/api/bots", "/api/tasks", "/api/approvals", "/api/settings", "/api/logs"):
        assert client.get(path).status_code == 401, path
    assert client.post("/api/settings", json={"paused": True}, headers=H).status_code == 401


def test_wrong_password_rate_limited(client):
    for _ in range(3):
        assert client.post("/api/auth/login", json={"password": "nope"}).status_code == 401
    # even the right password is refused once the limit is hit
    assert client.post("/api/auth/login", json={"password": "correct horse"}).status_code == 429


def test_session_cookie_flags_and_tamper(client):
    r = login(client)
    sc = r.headers["set-cookie"].lower()
    assert "httponly" in sc and "samesite=strict" in sc
    tok = make_session()
    assert check_session(tok)
    assert not check_session(tok[:-1] + ("0" if tok[-1] != "0" else "1"))


def test_mutations_need_csrf_header(client):
    login(client)
    assert client.post("/api/settings", json={"paused": True}).status_code == 403
    r = client.post("/api/settings", json={"paused": True}, headers=H)
    assert r.status_code == 200 and r.json()["paused"] is True
    assert client.get("/api/settings").json()["paused"] is True
    # audited
    audit = client.get("/api/audit").json()
    assert any(a["actor"] == "dashboard" for a in audit)


def test_overview_reflects_real_db(client, env):
    from workforce.contracts import TaskSpec
    from workforce.core.queue import TaskQueue
    login(client)
    TaskQueue(env.db).enqueue(TaskSpec(bot_id="echo_a", kind="echo", payload={}))
    ov = client.get("/api/overview").json()
    assert ov["queue"].get("queued", 0) >= 1
    ids = {b["id"] for b in ov["bots"]}
    assert {"opportunity_scout", "echo_a"} <= ids


def test_security_headers(client):
    r = client.get("/api/health")
    assert r.headers.get("x-frame-options") == "DENY"
    assert "default-src" in r.headers.get("content-security-policy", "")


def test_objective_and_task_cancel(client):
    login(client)
    r = client.post("/api/objective", json={"text": "find AI hackathons this month"}, headers=H)
    assert r.status_code == 200, r.text
    tid = r.json()["task_id"]
    assert client.get(f"/api/tasks/{tid}").json()["bot_id"]
    assert client.post(f"/api/tasks/{tid}/cancel", headers=H).status_code == 200
    assert client.get(f"/api/tasks/{tid}").json()["status"] == "cancelled"


def test_approval_grant_via_api(client, env, services):
    """An approval created by the gateway can only be granted by the owner session."""
    from workforce.contracts import TaskSpec
    from workforce.core.queue import TaskQueue
    from workforce.runtime import BotRuntime
    login(client)
    tid, _ = TaskQueue(env.db).enqueue(TaskSpec(bot_id="echo_a", kind="send_email", payload={"key": "api1", "to": "a@b.c"}))
    BotRuntime("echo_a", services, "t").step()
    assert TaskQueue(env.db).get(tid).status == "waiting_approval"
    pend = client.get("/api/approvals").json()
    assert len(pend) == 1
    aid = pend[0]["id"]
    assert client.post(f"/api/approvals/{aid}/grant").status_code == 403   # no CSRF header
    assert client.post(f"/api/approvals/{aid}/grant", headers=H).status_code == 200
    assert TaskQueue(env.db).get(tid).status == "queued"
    assert client.post(f"/api/approvals/{aid}/grant", headers=H).status_code == 404  # not pending any more


def test_bot_disable_enable(client):
    login(client)
    assert client.post("/api/bots/echo_b/disable", headers=H).status_code == 200
    assert [b for b in client.get("/api/bots").json() if b["id"] == "echo_b"][0]["enabled"] in (0, False)
    assert client.post("/api/bots/echo_b/enable", headers=H).status_code == 200
    assert client.post("/api/bots/echo_b/explode", headers=H).status_code == 400


def test_events_stream_requires_auth(client):
    assert client.get("/api/events").status_code == 401
