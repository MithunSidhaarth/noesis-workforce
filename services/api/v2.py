"""v2 API: the internship engine, pipeline board, company boards, custom agents, resume,
and the career extras. Mounted by app.py under the same owner session + CSRF header
rules as every other route. Each mutation reuses the owner_actions / module functions
the Telegram commands use, so the dashboard and Telegram behave the same.
"""
from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from workforce.contracts import TaskSpec
from workforce.core.db import get_db, loads
from workforce.core.queue import BotDisabled, TaskQueue
from workforce.core.util import now

router = APIRouter()


def _call(fn, *args, **kw):
    """Owner-facing errors (bad ref, bad stage, bad token) become 400s with their message."""
    from workforce.custom_agents import AgentError
    from workforce.owner_actions import OwnerActionError
    from workforce.pipeline import StageError
    from workforce.resume import ResumeError
    from workforce.secrets import SecretKeyMissing
    from workforce.sources.boards import BoardError
    try:
        return fn(*args, **kw)
    except (OwnerActionError, StageError, AgentError, BoardError, ResumeError, SecretKeyMissing) as e:
        raise HTTPException(400, str(e)) from e


def _enqueue(bot: str, kind: str, payload: dict, key: str) -> str:
    try:
        tid, _ = TaskQueue(get_db()).enqueue(TaskSpec(bot_id=bot, kind=kind, payload=payload, requested_by="dashboard",
                                                      idempotency_key=key))
    except BotDisabled as e:
        raise HTTPException(409, f"{bot} is switched off (Bots page)") from e
    return tid


# ---- home ------------------------------------------------------------------------------
@router.get("/api/home")
def home():
    from workforce.habits import applied_per_day, streak, todays_shortlist
    from workforce.pipeline import funnel, pending_proposals
    d = get_db()
    week = now() - 7 * 86400
    return {
        "streak": streak(d), "today": _opps(todays_shortlist(d)), "funnel_7d": funnel(d, week), "funnel_all": funnel(d),
        "applied_per_day": applied_per_day(d, 28), "proposals": pending_proposals(d),
        "upcoming": d.all("SELECT id, title, organizer, stage, next_event_at FROM opportunities WHERE next_event_at>? "
                          "ORDER BY next_event_at LIMIT 8", (now(),)),
        "deadlines": d.all("SELECT id, title, organizer, stage, deadline FROM opportunities WHERE deadline>? AND stage IN "
                           "('shortlisted','preparing') ORDER BY deadline LIMIT 8", (now(),)),
        "new_24h": d.scalar("SELECT COUNT(*) FROM opportunities WHERE discovered_at>?", (now() - 86400,)),
    }


def _opps(rs: list[dict]) -> list[dict]:
    for r in rs:
        for c in ("ai_reasons_json", "ai_red_flags_json", "ghost_reasons_json", "eligibility_json", "verification_notes_json",
                  "required_materials_json"):
            if c in r:
                r[c.removesuffix("_json")] = loads(r.pop(c), [])
        r.pop("raw_json", None)
    return rs


# ---- internships -------------------------------------------------------------------------
@router.get("/api/internships")
def internships(q: str = "", stage: str = "", location: str = "", min_fit: float = 0.0, include_closed: bool = False,
                limit: int = 300):
    sql = "SELECT * FROM opportunities WHERE 1=1"
    args: list = []
    if not include_closed:
        sql += " AND stage NOT IN ('skipped','rejected','withdrawn','ghosted') AND (deadline IS NULL OR deadline>?)"
        args.append(now())
    for w in q.lower().split()[:5]:
        sql += " AND lower(title || ' ' || COALESCE(organizer,'') || ' ' || COALESCE(summary,'')) LIKE ?"
        args.append(f"%{w}%")
    if stage:
        sql += " AND stage=?"
        args.append(stage)
    if location:
        sql += " AND lower(COALESCE(location,'') || ' ' || COALESCE(remote_status,'')) LIKE ?"
        args.append(f"%{location.lower()}%")
    if min_fit:
        sql += " AND COALESCE(relevance_score,0)>=?"
        args.append(min_fit)
    sql += " ORDER BY COALESCE(relevance_score,0) * (1 - 0.5 * COALESCE(ghost_risk,0)) DESC, deadline LIMIT ?"
    return _opps(get_db().all(sql, (*args, min(limit, 1000))))


@router.get("/api/internships/{opp_id}")
def internship(opp_id: str):
    from workforce.owner_actions import find_opportunity
    from workforce.pipeline import history
    d = get_db()
    o = _call(find_opportunity, d, opp_id)
    app = d.one("SELECT id, state, drafts_json, missing_json, resume_version FROM applications WHERE opportunity_id=?", (o["id"],))
    if app:
        app["drafts"] = loads(app.pop("drafts_json"), {})
        app["missing"] = loads(app.pop("missing_json"), [])
    return {**_opps([dict(o)])[0], "history": history(d, o["id"]), "application": app,
            "referrals": d.all("SELECT * FROM referrals WHERE lower(company)=lower(?)", (o.get("organizer") or "",))}


class Link(BaseModel):
    url: str = Field(min_length=8, max_length=1000)
    text: str = Field(default="", max_length=20000)


@router.post("/api/internships/link")
def add_link(b: Link):
    from workforce.owner_actions import add_link as add
    return _call(add, get_db(), f"{b.url.strip()} {b.text.strip()}".strip(), by="dashboard")


class Stage(BaseModel):
    stage: str = Field(min_length=2, max_length=30)
    event_at: float | None = None
    note: str = Field(default="", max_length=300)


@router.post("/api/internships/{opp_id}/stage")
def set_stage(opp_id: str, b: Stage):
    """The Kanban drag. An OA / interview with a time also gets reminders and a prep plan."""
    from workforce.owner_actions import find_opportunity, schedule_stage_followups
    from workforce.pipeline import set_stage as move
    d = get_db()
    o = _call(find_opportunity, d, opp_id)
    out = _call(move, d, o["id"], b.stage, by="dashboard", note=b.note, event_at=b.event_at)
    if out["stage"] in ("online_assessment", "interview_1", "interview_2", "hr"):
        schedule_stage_followups(d, out, "dashboard")
    return out


@router.post("/api/internships/{opp_id}/tailor")
def tailor(opp_id: str):
    from workforce.owner_actions import find_opportunity
    o = _call(find_opportunity, get_db(), opp_id)
    return {"task_id": _enqueue("applications", "tailor_resume", {"opportunity_id": o["id"]}, f"tailor:{o['id']}:{int(now() // 60)}")}


@router.get("/api/pipeline")
def pipeline():
    from workforce.pipeline import BOARD, LABELS, board
    cols = board(get_db())
    return {"columns": [{"stage": s, "label": LABELS[s], "items": cols.get(s, [])} for s in BOARD]}


class Decision(BaseModel):
    accept: bool


@router.post("/api/proposals/{prop_id}")
def proposal(prop_id: str, b: Decision):
    from workforce.pipeline import decide_proposal
    return _call(decide_proposal, get_db(), prop_id, b.accept, by="dashboard")


@router.get("/api/insights")
def insights():
    from workforce.insights import weekly_insights
    from workforce.pipeline import funnel, response_rates
    d = get_db()
    return {"funnel": funnel(d), "by_source": response_rates(d, "source"), "by_company": response_rates(d, "company"),
            "insights": weekly_insights(d)}


# ---- company boards ------------------------------------------------------------------------------
@router.get("/api/boards")
def boards():
    return get_db().all("SELECT b.*, s.last_checked, s.last_success, s.last_error, s.items_seen, "
                        "(SELECT COUNT(*) FROM opportunities o WHERE o.discovery_source=b.id) found "
                        "FROM company_boards b LEFT JOIN sources s ON s.id=b.id ORDER BY b.company")


class Board(BaseModel):
    company: str = Field(min_length=2, max_length=80)
    ats: str = Field(pattern=r"^(greenhouse|lever|ashby)$")
    slug: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._-]+$")
    domain: str | None = Field(default=None, max_length=120)


def _board(b: Board):
    from workforce.sources.registry import CompanyBoard
    try:
        return CompanyBoard(company=b.company, ats=b.ats, slug=b.slug, domain=b.domain)
    except ValueError as e:
        raise HTTPException(400, f"That board doesn't look right: {e}") from e


def _fetcher():
    from integrations.web.fetcher import Fetcher
    return Fetcher(min_host_interval_s=1.0)


@router.post("/api/boards/test")
def board_test(b: Board):
    from workforce.sources.boards import test_board
    return test_board(_fetcher(), _board(b))


@router.post("/api/boards")
def board_add(b: Board):
    from workforce.sources.boards import add_board, record_test, test_board
    res = test_board(_fetcher(), _board(b))
    if not res["ok"]:
        raise HTTPException(400, f"That board didn't answer: {res['error']}. Check the slug with Test first.")
    row = _call(add_board, get_db(), b.company, b.ats, b.slug, b.domain, by="dashboard")
    record_test(get_db(), row["id"], res["items"])
    return {**get_db().one("SELECT * FROM company_boards WHERE id=?", (row["id"],)), "test": res}


class Probe(BaseModel):
    company: str = Field(min_length=2, max_length=80)
    domain: str | None = Field(default=None, max_length=120)


@router.post("/api/boards/probe")
def board_probe(b: Probe):
    from workforce.sources.boards import probe
    return {"found": probe(_fetcher(), b.company, b.domain)}


@router.delete("/api/boards/{board_id}")
def board_remove(board_id: str):
    from workforce.sources.boards import remove_board
    return _call(remove_board, get_db(), board_id, by="dashboard")


# ---- custom agents --------------------------------------------------------------------------------
@router.get("/api/agents")
def agents():
    from workforce.custom_agents import TEMPLATES, TOOLS, list_agents
    return {"agents": list_agents(get_db()), "tools": TOOLS, "templates": TEMPLATES}


class NewAgent(BaseModel):
    name: str = Field(min_length=2, max_length=60)
    emoji: str = Field(default="🤖", max_length=8)
    template: str | None = Field(default=None, max_length=20)
    token: str = Field(min_length=20, max_length=120)
    task: str = Field(min_length=10, max_length=4000)
    tools: list[str] = Field(default_factory=list, max_length=20)
    schedule: str | None = Field(default=None, max_length=40)
    model_tier: str = Field(default="standard", pattern=r"^(cheap|standard)$")


@router.post("/api/agents")
def agent_create(b: NewAgent):
    from workforce.custom_agents import create_agent
    return _call(create_agent, get_db(), **b.model_dump(), by="dashboard")


class AgentPatch(BaseModel):
    name: str | None = Field(default=None, max_length=60)
    emoji: str | None = Field(default=None, max_length=8)
    task: str | None = Field(default=None, max_length=4000)
    tools: list[str] | None = Field(default=None, max_length=20)
    schedule: str | None = Field(default=None, max_length=40)
    model_tier: str | None = Field(default=None, pattern=r"^(cheap|standard)$")
    enabled: bool | None = None
    token: str | None = Field(default=None, max_length=120)


@router.patch("/api/agents/{agent_id}")
def agent_update(agent_id: str, b: AgentPatch):
    from workforce.custom_agents import update_agent
    return _call(update_agent, get_db(), agent_id, by="dashboard", **b.model_dump(exclude_unset=True))


@router.delete("/api/agents/{agent_id}")
def agent_delete(agent_id: str):
    from workforce.custom_agents import delete_agent
    _call(delete_agent, get_db(), agent_id, by="dashboard")
    return {"deleted": agent_id}


@router.post("/api/agents/{agent_id}/run")
def agent_run(agent_id: str):
    return {"task_id": _enqueue("custom_agents", "run_task", {"agent_id": agent_id}, f"carundash:{agent_id}:{int(now() // 60)}")}


@router.post("/api/agents/{agent_id}/forget")
def agent_forget(agent_id: str):
    from workforce.custom_agents import forget
    return {"forgotten": forget(get_db(), agent_id)}


@router.get("/api/agents/{agent_id}/messages")
def agent_messages(agent_id: str, limit: int = 50):
    from workforce.custom_agents import recent_turns
    return recent_turns(get_db(), agent_id, min(limit, 200))


# ---- resume -----------------------------------------------------------------------------------------
@router.get("/api/resume")
def resume():
    d = get_db()
    act = d.one("SELECT id, version, filename, uploaded_at, length(text) chars FROM resumes WHERE active=1")
    return {"active": act, "versions": d.all("SELECT id, version, filename, uploaded_at, active FROM resumes ORDER BY uploaded_at DESC")}


@router.post("/api/resume")
async def resume_upload(file: UploadFile = File(...)):
    from workforce.resume import MAX_BYTES, save_resume
    data = await file.read(MAX_BYTES + 1)
    return _call(save_resume, get_db(), file.filename or "resume.pdf", data, by="dashboard")


def _fact(p: dict, k: str) -> str:
    f = (p.get("facts") or {}).get(k) or (p.get("answers") or {}).get(k) or {}
    return str(f.get("value") or "") if isinstance(f, dict) and f.get("verified") and not f.get("sensitive") else ""


@router.get("/api/resume/pdf")
def resume_pdf(application_id: str | None = None):
    """A plain one-page, ATS-friendly PDF: your verified facts plus either the bullets tailored
    for one application (only lines traced to your resume) or your resume's own lines."""
    from workforce.resume import active_resume, one_page_pdf, resume_lines
    from workforce.sources.registry import load_profile
    d, p = get_db(), load_profile()
    res = active_resume(d)
    bullets: list[str] = []
    if application_id:
        a = d.one("SELECT drafts_json FROM applications WHERE id=? OR id LIKE ?", (application_id, f"%{application_id}"))
        bullets = [b["text"] for b in (loads(a["drafts_json"], {}) if a else {}).get("bullets", []) if b.get("text")]
    if not bullets and res:
        bullets = resume_lines(res["text"])[:22]
    if not bullets:
        raise HTTPException(400, "Upload your resume first (Settings → Resume).")
    contact = " · ".join(x for x in (p.get("email"), _fact(p, "location"), _fact(p, "github"), _fact(p, "linkedin"),
                                     _fact(p, "portfolio")) if x)
    edu = " · ".join(x for x in (_fact(p, "degree"), _fact(p, "college") or _fact(p, "institution"),
                                 f"graduating {_fact(p, 'graduation_year')}" if _fact(p, "graduation_year") else "",
                                 f"CGPA {_fact(p, 'cgpa')}" if _fact(p, "cgpa") else "") if x)
    sections = [("Summary", [_fact(p, "short_bio")] if _fact(p, "short_bio") else []),
                ("Education", [edu] if edu else []), ("Skills", [_fact(p, "skills")] if _fact(p, "skills") else []),
                ("Experience and projects", bullets)]
    pdf = one_page_pdf(p.get("name") or "Your Name", contact, sections)
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": 'attachment; filename="resume-one-page.pdf"'})


# ---- career extras ---------------------------------------------------------------------------------
@router.get("/api/referrals")
def referrals(q: str = ""):
    from workforce.referrals import plan
    d = get_db()
    out = {"list": d.all("SELECT * FROM referrals ORDER BY updated_at DESC LIMIT 200")}
    if q.strip():
        out["plan"] = _call(plan, d, q)
    return out


class Referral(BaseModel):
    company: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=80)
    profile_url: str | None = Field(default=None, max_length=300)
    relation: str = Field(default="employee", pattern=r"^(alumni|employee|friend|recruiter)$")
    note: str | None = Field(default=None, max_length=600)


@router.post("/api/referrals")
def referral_add(b: Referral):
    from workforce.referrals import add_referral
    return _call(add_referral, get_db(), b.company, b.name, b.profile_url, b.relation, note=b.note, by="dashboard")


class Status(BaseModel):
    status: str = Field(pattern=r"^(to_contact|messaged|replied|referred|declined)$")


@router.post("/api/referrals/{rid}/status")
def referral_status(rid: str, b: Status):
    from workforce.referrals import set_status
    return _call(set_status, get_db(), rid, b.status, by="dashboard")


@router.get("/api/questions")
def questions(company: str = "", topic: str = ""):
    from workforce.debrief import bank, bank_stats
    return {"questions": bank(get_db(), company or None, topic or None, limit=200), "by_company": bank_stats(get_db())}


class Debrief(BaseModel):
    company: str = Field(min_length=1, max_length=80)
    stage: str | None = Field(default=None, max_length=30)
    questions: list[str] = Field(min_length=1, max_length=30)
    notes: str | None = Field(default=None, max_length=2000)
    went_well: bool | None = None


@router.post("/api/debrief")
def debrief(b: Debrief):
    from workforce.debrief import log_debrief
    n = _call(log_debrief, get_db(), b.company, b.stage, [q[:400] for q in b.questions if q.strip()], notes=b.notes,
              went_well=b.went_well, by="dashboard")
    return {"logged": n}


@router.get("/api/offers")
def offers():
    from workforce.offers import compare, weights
    return {"offers": compare(get_db()), "weights": weights()}


class Offer(BaseModel):
    company: str = Field(min_length=1, max_length=80)
    role: str | None = Field(default=None, max_length=120)
    stipend_month_inr: int | None = Field(default=None, ge=0, le=10_000_000)
    duration_months: float | None = Field(default=None, ge=0, le=36)
    location: str | None = Field(default=None, max_length=60)
    mode: str | None = Field(default=None, pattern=r"^(onsite|hybrid|remote)$")
    ppo: str = Field(default="unknown", pattern=r"^(yes|likely|unknown|no)$")
    learning: int | None = Field(default=None, ge=1, le=5)
    notes: str | None = Field(default=None, max_length=300)


@router.post("/api/offers")
def offer_add(b: Offer):
    from workforce.offers import add_offer
    return add_offer(get_db(), b.model_dump(), by="dashboard")


@router.delete("/api/offers/{oid}")
def offer_delete(oid: str):
    from workforce.offers import delete_offer
    return {"deleted": delete_offer(get_db(), oid)}


@router.get("/api/drives")
def drives():
    from workforce.drives import status
    return status(get_db())


class Company(BaseModel):
    company: str = Field(min_length=2, max_length=80)
    refresh: bool = False


@router.post("/api/dossier")
def dossier(b: Company):
    tid = _enqueue("applications", "company_dossier", {"company": b.company, "refresh": b.refresh},
                   f"dossier:{b.company.lower()}:{int(now() // 300)}")
    return {"task_id": tid}


@router.get("/api/dossier")
def dossier_get(company: str):
    from workforce.core.db import loads as _loads
    r = get_db().one("SELECT value_json FROM bot_memory WHERE bot_id='applications' AND key=?", (f"dossier:{company.strip().lower()}",))
    return _loads(r["value_json"]) if r else None


@router.get("/api/phone")
def phone():
    from workforce.policies.engine import Policies
    d = get_db()
    return {"policy": Policies().get("phone"), "calendar": Policies().get("calendar"),
            "integration": d.one("SELECT * FROM integrations WHERE name='phone'"),
            "calls": d.all("SELECT id, reason, message, status, error, created_at, placed_at FROM phone_calls ORDER BY created_at DESC LIMIT 20")}


@router.post("/api/phone/test")
def phone_test():
    return {"task_id": _enqueue("personal_ops", "test_call", {}, f"calltest:{int(now() // 300)}")}
