"""Private control-plane API + static dashboard.

Every route except /api/health and /api/auth/login requires the owner session
cookie. Mutating routes also require the `X-Noesis-Request: 1` header (CSRF
defence on top of SameSite=Strict). The API reads the real database; there is
no mock data anywhere in this layer.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from workforce.contracts import TaskSpec
from workforce.core.db import Database, get_db, loads
from workforce.core.queue import QueueError, TaskQueue
from workforce.core.settings import ROOT
from workforce.core.state import SystemState, audit
from workforce.core.util import new_id, now
from workforce.policies.engine import decide_approval
from workforce.registry import BotRegistry

from .auth import COOKIE, SESSION_TTL, LoginLimiter, check_session, make_session, verify_password

limiter = LoginLimiter()


def db() -> Database:
    return get_db()


def require_owner(request: Request) -> None:
    if not check_session(request.cookies.get(COOKIE)):
        raise HTTPException(401, "login required")
    if request.method not in ("GET", "HEAD") and request.headers.get("x-noesis-request") != "1":
        raise HTTPException(403, "missing X-Noesis-Request header")


def rows(sql: str, args: tuple = (), json_cols: tuple[str, ...] = ()) -> list[dict]:
    out = get_db().all(sql, args)
    for r in out:
        for c in json_cols:
            if c in r:
                r[c.removesuffix("_json")] = loads(r.pop(c))
    return out


@asynccontextmanager
async def lifespan(_app):
    from workforce.factory import bootstrap
    bootstrap()
    yield


app = FastAPI(title="NOESIS Workforce", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
Owner = Depends(require_owner)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                                               "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
    return resp


# ---- auth ---------------------------------------------------------------
class Login(BaseModel):
    password: str = Field(min_length=1, max_length=200)


@app.post("/api/auth/login")
def login(body: Login, request: Request, response: Response):
    key = request.client.host if request.client else "unknown"
    if not limiter.allowed(key):
        raise HTTPException(429, "too many attempts; try again later")
    stored = os.environ.get("NOESIS_ADMIN_PASSWORD_HASH", "")
    if not stored or not verify_password(body.password, stored):
        limiter.fail(key)
        audit(get_db(), "dashboard", "auth.failed", None, {"ip": key})
        raise HTTPException(401, "invalid password")
    limiter.reset(key)
    response.set_cookie(COOKIE, make_session(), max_age=SESSION_TTL, httponly=True, samesite="strict",
                        secure=os.environ.get("NOESIS_COOKIE_SECURE", "true") != "false", path="/")
    audit(get_db(), "dashboard", "auth.login", None, {"ip": key})
    return {"ok": True}


@app.post("/api/auth/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@app.get("/api/auth/me")
def me(request: Request):
    return {"authenticated": check_session(request.cookies.get(COOKIE))}


# ---- health -------------------------------------------------------------
@app.get("/api/health")
def health():
    d = get_db()
    try:
        d.scalar("SELECT 1")
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "db": str(e)}, status_code=503)
    hb = d.one("SELECT value FROM system_state WHERE key='scheduler_heartbeat'")
    sched_age = time.time() - float(json.loads(hb["value"])) if hb else None
    alive = d.scalar("SELECT COUNT(*) FROM bots WHERE enabled=1 AND last_heartbeat > ?", (time.time() - 120,))
    return {"ok": True, "scheduler_heartbeat_age_s": sched_age, "bots_alive": alive}


# ---- overview -----------------------------------------------------------
@app.get("/api/overview", dependencies=[Owner])
def overview():
    d = get_db()
    st = SystemState(d)
    day = now() - 86400
    return {
        "paused": st.paused, "dry_run": st.dry_run,
        "queue": TaskQueue(d).depth(),
        "bots": rows("SELECT id, name, enabled, state, current_task_id, last_heartbeat, last_success, tasks_succeeded, tasks_failed FROM bots ORDER BY id"),
        "pending_approvals": d.scalar("SELECT COUNT(*) FROM approvals WHERE status='pending'"),
        "tasks_24h": {r["status"]: r["n"] for r in d.all("SELECT status, COUNT(*) n FROM tasks WHERE updated_at>? GROUP BY status", (day,))},
        "opportunities_24h": d.scalar("SELECT COUNT(*) FROM opportunities WHERE discovered_at>?", (day,)),
        "upcoming_deadlines": rows("SELECT id, title, deadline, verification_status, application_status FROM opportunities "
                                   "WHERE deadline > ? AND application_status NOT IN ('SUBMITTED','CLOSED') ORDER BY deadline LIMIT 6", (now(),)),
        "spend_today_usd": d.scalar("SELECT COALESCE(SUM(cost_usd),0) FROM model_usage WHERE ts>?", (day,)),
        "integrations": rows("SELECT * FROM integrations ORDER BY name"),
        "recent_activity": rows("SELECT ts, level, bot_id, message FROM exec_log ORDER BY id DESC LIMIT 15"),
    }


# ---- bots ---------------------------------------------------------------
@app.get("/api/bots", dependencies=[Owner])
def bots():
    out = rows("SELECT * FROM bots ORDER BY id", json_cols=("config_json",))
    return out


@app.get("/api/bots/{bot_id}", dependencies=[Owner])
def bot_detail(bot_id: str):
    d = get_db()
    b = d.one("SELECT * FROM bots WHERE id=?", (bot_id,))
    if not b:
        raise HTTPException(404)
    b["config"] = loads(b.pop("config_json"))
    return {
        "bot": b,
        "current_task": d.one("SELECT id, kind, status, started_at FROM tasks WHERE id=?", (b["current_task_id"],)) if b["current_task_id"] else None,
        "recent_tasks": rows("SELECT id, kind, status, attempts, error, created_at, finished_at FROM tasks WHERE bot_id=? ORDER BY updated_at DESC LIMIT 25", (bot_id,)),
        "schedules": rows("SELECT * FROM schedules WHERE bot_id=?", (bot_id,), ("payload_json",)),
        "messages": rows("SELECT id, sender, recipient, topic, type, status, created_at, correlation_id FROM messages WHERE sender=? OR recipient=? "
                         "ORDER BY created_at DESC LIMIT 30", (bot_id, bot_id)),
        "errors": rows("SELECT ts, level, message, task_id FROM exec_log WHERE bot_id=? AND level IN ('error','warning') ORDER BY id DESC LIMIT 20", (bot_id,)),
        "usage": d.one("SELECT COUNT(*) calls, COALESCE(SUM(cost_usd),0) cost, COALESCE(SUM(input_tokens+output_tokens),0) tokens "
                       "FROM model_usage WHERE bot_id=? AND ts>?", (bot_id, now() - 30 * 86400)),
        "actions": rows("SELECT action, status, target, evidence, created_at FROM external_actions WHERE bot_id=? ORDER BY created_at DESC LIMIT 15", (bot_id,)),
    }


@app.post("/api/bots/{bot_id}/{op}", dependencies=[Owner])
def bot_control(bot_id: str, op: str):
    reg = BotRegistry(get_db())
    if not get_db().one("SELECT 1 FROM bots WHERE id=?", (bot_id,)):
        raise HTTPException(404)
    if op == "enable":
        reg.set_enabled(bot_id, True, "dashboard")
    elif op == "disable":
        reg.set_enabled(bot_id, False, "dashboard")
    elif op == "pause":
        reg.set_paused(bot_id, True, "dashboard")
    elif op == "resume":
        reg.set_paused(bot_id, False, "dashboard")
    else:
        raise HTTPException(400, "unknown operation")
    return {"ok": True}


# ---- tasks & messages ------------------------------------------------------
@app.get("/api/tasks", dependencies=[Owner])
def tasks(status: str | None = None, bot: str | None = None, limit: int = 100):
    where, args = [], []
    if status:
        where.append("status=?")
        args.append(status)
    if bot:
        where.append("bot_id=?")
        args.append(bot)
    sql = ("SELECT id, bot_id, kind, status, priority, workflow_id, correlation_id, requested_by, delegation_depth, attempts, max_attempts, "
           "error, created_at, started_at, finished_at, run_after, depends_on_json FROM tasks"
           + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY updated_at DESC LIMIT ?")
    return rows(sql, (*args, min(limit, 500)), ("depends_on_json",))


@app.get("/api/tasks/{task_id}", dependencies=[Owner])
def task_detail(task_id: str):
    r = get_db().one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not r:
        raise HTTPException(404)
    for c in ("payload_json", "result_json", "checkpoint_json", "depends_on_json"):
        r[c.removesuffix("_json")] = loads(r.pop(c))
    return r


@app.post("/api/tasks/{task_id}/cancel", dependencies=[Owner])
def cancel_task(task_id: str):
    return {"cancelled": TaskQueue(get_db()).cancel(task_id, "dashboard")}


@app.post("/api/tasks/{task_id}/retry", dependencies=[Owner])
def retry_task(task_id: str):
    n = get_db().execute("UPDATE tasks SET status='queued', attempts=0, run_after=?, error=NULL, updated_at=? WHERE id=? AND status='dead'",
                         (now(), now(), task_id)).rowcount
    audit(get_db(), "dashboard", "task.retried", None, {"task_id": task_id})
    return {"requeued": bool(n)}


@app.get("/api/messages", dependencies=[Owner])
def messages(bot: str | None = None, correlation: str | None = None, q: str | None = None, limit: int = 100):
    from workforce.messaging.bus import MessageBus
    return MessageBus(get_db()).history(bot, correlation, q, min(limit, 500))


class Objective(BaseModel):
    text: str = Field(min_length=3, max_length=4000)


@app.post("/api/objective", dependencies=[Owner])
def objective(body: Objective):
    tid, _ = TaskQueue(get_db()).enqueue(TaskSpec(bot_id="orchestrator", kind="objective", payload={"text": body.text},
                                                 requested_by="dashboard"))
    return {"task_id": tid}


# ---- domain views -------------------------------------------------------
@app.get("/api/opportunities", dependencies=[Owner])
def opportunities(limit: int = 200):
    return rows("SELECT id, title, organizer, official_url, discovery_source, type, remote_status, deadline, deadline_text, deadline_tz, "
                "relevance_score, relevance_rationale, verification_status, verification_notes_json, eligibility_status, application_status, "
                "eligibility_json, required_materials_json, discovered_at, last_verified, note_path FROM opportunities "
                "ORDER BY discovered_at DESC LIMIT ?", (min(limit, 1000),),
                ("verification_notes_json", "eligibility_json", "required_materials_json"))


@app.get("/api/sources", dependencies=[Owner])
def sources():
    return rows("SELECT id, name, kind, url, access_method, interval_s, enabled, reliability, consecutive_failures, last_checked, "
                "last_success, last_error, next_check, items_seen FROM sources ORDER BY name")


@app.get("/api/applications", dependencies=[Owner])
def applications():
    return rows("SELECT a.*, o.title, o.deadline, o.official_url FROM applications a JOIN opportunities o ON o.id=a.opportunity_id "
                "ORDER BY a.updated_at DESC", (), ("answers_json", "missing_json", "documents_json", "history_json"))


class Confirmation(BaseModel):
    confirmation: str = Field(min_length=2, max_length=300)


@app.post("/api/applications/{app_id}/submitted", dependencies=[Owner])
def app_submitted(app_id: str, body: Confirmation):
    from workforce.agents.applications import record_manual_submission
    try:
        return record_manual_submission(get_db(), app_id, body.confirmation, by="dashboard")
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/applications/{app_id}/submit", dependencies=[Owner])
def app_submit(app_id: str):
    tid, _ = TaskQueue(get_db()).enqueue(TaskSpec(bot_id="applications", kind="submit_application",
                                                 payload={"application_id": app_id}, requested_by="dashboard",
                                                 idempotency_key=f"submit:{app_id}:{int(now() // 60)}"))
    return {"task_id": tid}


@app.get("/api/content", dependencies=[Owner])
def content():
    return rows("SELECT * FROM content_items ORDER BY updated_at DESC LIMIT 200", (),
                ("brief_json", "script_json", "assets_json", "claims_json", "sources_json", "metrics_json", "history_json"))


@app.get("/api/emails", dependencies=[Owner])
def emails():
    return rows("SELECT * FROM emails ORDER BY received_at DESC LIMIT 200", (), ("actions_json",))


@app.post("/api/emails/{email_id}/send", dependencies=[Owner])
def email_send(email_id: str):
    tid, _ = TaskQueue(get_db()).enqueue(TaskSpec(bot_id="communications", kind="send_reply", payload={"email_id": email_id},
                                                 requested_by="dashboard", idempotency_key=f"sendreq:{email_id}"))
    return {"task_id": tid}


@app.get("/api/outreach", dependencies=[Owner])
def outreach():
    return {"contacts": rows("SELECT * FROM contacts ORDER BY updated_at DESC LIMIT 300"),
            "messages": rows("SELECT * FROM outreach_messages ORDER BY created_at DESC LIMIT 300"),
            "suppressed": rows("SELECT * FROM suppression_list ORDER BY created_at DESC")}


class Contact(BaseModel):
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=200)
    name: str | None = Field(None, max_length=120)
    organization: str | None = Field(None, max_length=120)
    role: str | None = Field(None, max_length=120)
    source_url: str | None = Field(None, max_length=500)
    notes: str | None = Field(None, max_length=2000)


@app.post("/api/outreach/contacts", dependencies=[Owner])
def add_contact(c: Contact):
    d = get_db()
    if d.scalar("SELECT 1 FROM contacts WHERE email=?", (c.email.lower(),)):
        raise HTTPException(409, "contact already exists")
    cid = new_id("ct")
    d.execute("INSERT INTO contacts(id, email, name, organization, role, source_url, notes, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
              (cid, c.email.lower(), c.name, c.organization, c.role, c.source_url, c.notes, now(), now()))
    TaskQueue(d).enqueue(TaskSpec(bot_id="outreach", kind="draft_outreach", payload={"contact_id": cid}, requested_by="dashboard"))
    return {"id": cid}


class OptOut(BaseModel):
    email: str
    reason: str = "requested"


@app.post("/api/outreach/opt-out", dependencies=[Owner])
def opt_out(b: OptOut):
    from workforce.agents.outreach import mark_opted_out
    mark_opted_out(get_db(), b.email, b.reason, by="dashboard")
    return {"ok": True}


@app.post("/api/outreach/messages/{mid}/send", dependencies=[Owner])
def outreach_send(mid: str):
    tid, _ = TaskQueue(get_db()).enqueue(TaskSpec(bot_id="outreach", kind="send_outreach", payload={"message_id": mid},
                                                 requested_by="dashboard", idempotency_key=f"osend:{mid}"))
    return {"task_id": tid}


@app.get("/api/knowledge", dependencies=[Owner])
def knowledge():
    return rows("SELECT * FROM knowledge_captures ORDER BY created_at DESC LIMIT 200")


class Url(BaseModel):
    url: str = Field(pattern=r"^https?://", max_length=2000)


@app.post("/api/knowledge/ingest", dependencies=[Owner])
def ingest(u: Url):
    tid, _ = TaskQueue(get_db()).enqueue(TaskSpec(bot_id="knowledge", kind="ingest_url", payload={"url": u.url}, requested_by="dashboard"))
    return {"task_id": tid}


# ---- approvals, logs, integrations, usage, settings ---------------------
@app.get("/api/approvals", dependencies=[Owner])
def approvals(status: str = "pending"):
    return rows("SELECT * FROM approvals WHERE status=? ORDER BY created_at DESC LIMIT 200", (status,), ("payload_json", "reasons_json"))


@app.post("/api/approvals/{aid}/{decision}", dependencies=[Owner])
def decide(aid: str, decision: str):
    if decision not in ("grant", "deny"):
        raise HTTPException(400)
    r = decide_approval(get_db(), aid, decision == "grant", by="dashboard")
    if not r:
        raise HTTPException(404, "no pending approval with that id")
    return r


@app.get("/api/logs", dependencies=[Owner])
def logs(bot: str | None = None, level: str | None = None, limit: int = 200):
    where, args = [], []
    if bot:
        where.append("bot_id=?")
        args.append(bot)
    if level:
        where.append("level=?")
        args.append(level)
    return rows("SELECT * FROM exec_log" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?",
                (*args, min(limit, 1000)), ("data_json",))


@app.get("/api/audit", dependencies=[Owner])
def audit_log(limit: int = 200):
    return rows("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (min(limit, 1000),), ("details_json",))


@app.get("/api/actions", dependencies=[Owner])
def actions(limit: int = 200):
    return rows("SELECT id, bot_id, action, target, status, dry_run, evidence, error, created_at FROM external_actions "
                "ORDER BY created_at DESC LIMIT ?", (min(limit, 1000),))


@app.get("/api/integrations", dependencies=[Owner])
def integrations():
    return rows("SELECT * FROM integrations ORDER BY name")


@app.get("/api/usage", dependencies=[Owner])
def usage(days: int = 30):
    since = now() - days * 86400
    d = get_db()
    return {
        "by_bot": rows("SELECT bot_id, COUNT(*) calls, SUM(cached) cached, SUM(input_tokens) input_tokens, SUM(output_tokens) output_tokens, "
                       "ROUND(SUM(cost_usd), 6) cost_usd, SUM(1-ok) errors FROM model_usage WHERE ts>? GROUP BY bot_id", (since,)),
        "by_day": rows("SELECT date(ts, 'unixepoch', '+330 minutes') day, COUNT(*) calls, ROUND(SUM(cost_usd), 6) cost_usd "
                       "FROM model_usage WHERE ts>? GROUP BY day ORDER BY day", (since,)),
        "by_model": rows("SELECT provider, model, COUNT(*) calls, ROUND(SUM(cost_usd), 6) cost_usd FROM model_usage WHERE ts>? GROUP BY provider, model", (since,)),
        "actions": rows("SELECT action, status, COUNT(*) n FROM external_actions WHERE created_at>? GROUP BY action, status", (since,)),
        "total_cost_usd": d.scalar("SELECT COALESCE(SUM(cost_usd),0) FROM model_usage WHERE ts>?", (since,)),
    }


@app.get("/api/settings", dependencies=[Owner])
def settings():
    from workforce.policies.engine import Policies
    st = SystemState(get_db())
    p = Policies()
    return {"paused": st.paused, "dry_run": st.dry_run,
            "policies": {k: p.get(k) for k in ("global", "publishing", "email", "applications", "outreach")},
            "vault_path_configured": bool(os.environ.get("NOESIS_VAULT_PATH")),
            "schedules": rows("SELECT * FROM schedules ORDER BY bot_id", (), ("payload_json",))}


class Flags(BaseModel):
    paused: bool | None = None
    dry_run: bool | None = None


@app.post("/api/settings", dependencies=[Owner])
def set_flags(f: Flags):
    st = SystemState(get_db())
    if f.paused is not None:
        st.set(SystemState.PAUSED, f.paused, by="dashboard")
    if f.dry_run is not None:
        st.set(SystemState.DRY_RUN, f.dry_run, by="dashboard")
    return {"paused": st.paused, "dry_run": st.dry_run}


@app.get("/api/events", dependencies=[Owner])
async def events(request: Request):
    """Server-sent events: new log lines + queue depth, without page reloads."""
    async def gen():
        d = get_db()
        last = d.scalar("SELECT COALESCE(MAX(id),0) FROM exec_log")
        last_audit = d.scalar("SELECT COALESCE(MAX(id),0) FROM audit_log")
        while not await request.is_disconnected():
            new = await asyncio.to_thread(d.all, "SELECT id, ts, level, bot_id, task_id, message FROM exec_log WHERE id>? ORDER BY id LIMIT 50", (last,))
            aud = await asyncio.to_thread(d.all, "SELECT id, ts, actor, event FROM audit_log WHERE id>? ORDER BY id LIMIT 50", (last_audit,))
            if new:
                last = new[-1]["id"]
            if aud:
                last_audit = aud[-1]["id"]
            payload = {"logs": new, "audit": aud, "queue": TaskQueue(d).depth(),
                       "bots": d.all("SELECT id, state, current_task_id, last_heartbeat FROM bots"),
                       "approvals": d.scalar("SELECT COUNT(*) FROM approvals WHERE status='pending'")}
            yield f"data: {json.dumps(payload, default=str)}\n\n"
            await asyncio.sleep(2)
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---- static dashboard ---------------------------------------------------
DASH = Path(os.environ.get("NOESIS_DASHBOARD_DIR", ROOT / "apps" / "dashboard" / "out"))
if DASH.is_dir():
    app.mount("/_next", StaticFiles(directory=DASH / "_next"), name="next")

    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def spa(path: str):
        if path.startswith("api/"):
            raise HTTPException(404)
        for cand in (DASH / path, DASH / f"{path}.html", DASH / path / "index.html"):
            if cand.is_file() and DASH in cand.resolve().parents:
                return FileResponse(cand)
        return FileResponse(DASH / "index.html")



