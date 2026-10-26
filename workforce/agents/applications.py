"""Applications Agent.

State machine:
DISCOVERED -> VERIFIED -> ELIGIBILITY_CHECK -> PREPARATION -> REVIEW -> READY_TO_SUBMIT -> SUBMITTED -> FOLLOW_UP -> CLOSED

Only facts marked `verified: true` in config/profile.yaml are ever used. Missing
facts become `missing` items for you to supply. Submission happens only through
the ActionGateway (application.submit) and only for an allowed method; web
forms are prepared for you to submit manually, then you record the confirmation.
"""
from __future__ import annotations

import re

from ..contracts import MessageType
from ..core.db import dumps, loads
from ..core.queue import Task
from ..core.state import audit
from ..core.util import new_id, now
from ..sources.parsing import domain
from ..sources.registry import load_profile
from ..policies.engine import IntegrationRevoked
from .base import Agent, AgentContext, PermanentError, register, untrusted

STATES = ["DISCOVERED", "VERIFIED", "ELIGIBILITY_CHECK", "PREPARATION", "REVIEW", "READY_TO_SUBMIT", "SUBMITTED",
          "FOLLOW_UP", "CLOSED"]
ALLOWED = {
    "DISCOVERED": {"VERIFIED", "CLOSED"}, "VERIFIED": {"ELIGIBILITY_CHECK", "CLOSED"},
    "ELIGIBILITY_CHECK": {"PREPARATION", "CLOSED"}, "PREPARATION": {"REVIEW", "CLOSED"},
    "REVIEW": {"READY_TO_SUBMIT", "PREPARATION", "CLOSED"}, "READY_TO_SUBMIT": {"SUBMITTED", "REVIEW", "CLOSED"},
    "SUBMITTED": {"FOLLOW_UP", "CLOSED"}, "FOLLOW_UP": {"CLOSED"}, "CLOSED": set(),
}

MATERIAL_TO_DOC = {"resume": "resume", "cv": "resume", "transcript": "transcript", "portfolio": "portfolio",
                   "cover letter": "cover_letter", "statement of purpose": "sop", "sop": "sop",
                   "letter of recommendation": "recommendation", "recommendation letter": "recommendation",
                   "github": "github", "id proof": "id_proof", "pitch deck": "pitch_deck", "video pitch": "video_pitch",
                   "writing sample": "writing_sample", "essay": "essay"}
SENSITIVE_FACTS = {"date_of_birth", "id_number", "aadhaar", "passport_number", "address", "phone", "income", "caste", "religion"}


def verified_facts(profile: dict) -> dict:
    return {k: v["value"] for k, v in (profile.get("facts") or {}).items()
            if isinstance(v, dict) and v.get("verified") and v.get("value") not in (None, "")}


def check_eligibility(statements: list[str], facts: dict) -> tuple[str, list[str], list[str]]:
    """Returns (status, reasons, unknown_criteria). Conservative: anything not
    provably satisfied is 'needs_info', never 'eligible'."""
    reasons, unknown, failed = [], [], False
    if not statements:
        return "needs_info", ["eligibility criteria not stated in the listing"], ["eligibility criteria"]
    for s in statements:
        low = s.lower()
        handled = False
        m = re.search(r"(?:under|below|younger than|not older than|up to)\s+(?:the age of\s+)?(\d{2})", low)
        if m:
            handled = True
            if "age" in facts:
                ok = int(facts["age"]) < int(m.group(1)) + (1 if "up to" in m.group(0) or "not older" in m.group(0) else 0)
                reasons.append(f"age {facts['age']} vs '{m.group(0)}': {'ok' if ok else 'fails'}")
                failed |= not ok
            else:
                unknown.append("age")
        if re.search(r"\b(citizens?|nationals?|residents?)\b", low) and "all nationalities" not in low:
            handled = True
            if "citizenship" in facts:
                ok = str(facts["citizenship"]).lower() in low
                reasons.append(f"citizenship {facts['citizenship']} vs '{s[:80]}': {'ok' if ok else 'not mentioned'}")
                failed |= not ok
            else:
                unknown.append("citizenship")
        if re.search(r"\b(undergraduate|bachelor|b\.?tech|b\.?e\.?|students?|enrolled|graduate students|postgraduate|master)", low):
            handled = True
            if "degree" in facts or "student_status" in facts:
                deg = f"{facts.get('degree', '')} {facts.get('student_status', '')}".lower()
                needs_pg = bool(re.search(r"\b(postgraduate|master'?s?|mba|phd)\b", low)) and not re.search(r"\b(undergraduate|bachelor)", low)
                ok = not needs_pg or any(k in deg for k in ("master", "mba", "phd", "postgraduate"))
                reasons.append(f"education '{deg.strip()}' vs '{s[:80]}': {'ok' if ok else 'fails'}")
                failed |= not ok
            else:
                unknown.append("education level")
        if re.search(r"\bwomen\b|\bfemale\b", low):
            handled = True
            if "gender" in facts:
                ok = str(facts["gender"]).lower() in ("woman", "female")
                reasons.append(f"gender requirement: {'ok' if ok else 'fails'}")
                failed |= not ok
            else:
                unknown.append("gender")
        if not handled:
            unknown.append(f"unrecognized criterion: {s[:120]}")
    if failed:
        return "ineligible", reasons, unknown
    if unknown:
        return "needs_info", reasons, sorted(set(unknown))
    return "eligible", reasons, []


def submission_method(opp: dict) -> tuple[str, str | None]:
    raw = loads(opp.get("raw_json"), {}).get("raw_text", "")
    m = re.search(r"(?:send|email|mail)[^.\n]{0,40}?\b([\w.+-]+@[\w-]+\.[\w.]+)", raw, re.IGNORECASE)
    if m:
        return "email", m.group(1)
    return "web_form", opp.get("official_url")


@register("applications")
class ApplicationsAgent(Agent):
    accepts = {"evaluate_opportunity", "prepare_application", "tailor_resume", "prepare_stage", "company_dossier"}

    def handlers(self):
        return {"evaluate_opportunity": self.evaluate, "prepare_application": self.evaluate,
                "submit_application": self.submit, "follow_up_sweep": self.follow_up_sweep,
                "tailor_resume": self.tailor_resume, "prepare_stage": self.prepare_stage,
                "company_dossier": self.company_dossier}

    def company_dossier(self, ctx: AgentContext, task: Task) -> dict:
        from ..dossier import build_dossier
        company = str(task.payload.get("company") or "").strip()
        if not company:
            raise PermanentError("company_dossier needs a company")
        d = build_dossier(ctx, company, use_cache=not task.payload.get("refresh"))
        ctx.notify_owner(d["text"], key=f"dossier:{company.lower()}:{task.id}", force_now=True)
        return {"company": company, "news": len(d["news"]), "summary": bool(d["summary"])}

    def _transition(self, ctx: AgentContext, app: dict, new: str, note: str) -> dict:
        if new != app["state"] and new not in ALLOWED[app["state"]]:
            raise PermanentError(f"illegal transition {app['state']} -> {new}")
        hist = loads(app["history_json"], []) + [{"ts": now(), "from": app["state"], "to": new, "note": note}]
        ctx.db.execute("UPDATE applications SET state=?, history_json=?, updated_at=? WHERE id=?", (new, dumps(hist), now(), app["id"]))
        ctx.db.execute("UPDATE opportunities SET application_status=? WHERE id=?", (new, app["opportunity_id"]))
        if new in ("SUBMITTED", "CLOSED"):
            from ..owner_actions import cancel_opportunity_reminders
            cancel_opportunity_reminders(ctx.db, app["opportunity_id"])
        audit(ctx.db, ctx.bot.id, "application.transition", None, {"application_id": app["id"], "from": app["state"], "to": new, "note": note})
        app = dict(app, state=new, history_json=dumps(hist))
        return app

    def evaluate(self, ctx: AgentContext, task: Task) -> dict:
        oid = task.payload["opportunity_id"]
        opp = ctx.db.one("SELECT * FROM opportunities WHERE id=?", (oid,))
        if not opp:
            raise PermanentError("opportunity not found")
        app = ctx.db.one("SELECT * FROM applications WHERE opportunity_id=?", (oid,))
        if app is None:
            aid = new_id("app")
            ctx.db.execute("INSERT OR IGNORE INTO applications(id, opportunity_id, state, created_at, updated_at) VALUES (?,?,?,?,?)",
                           (aid, oid, "DISCOVERED", now(), now()))
            app = ctx.db.one("SELECT * FROM applications WHERE opportunity_id=?", (oid,))
        elif app["state"] == "REVIEW" and task.payload.get("refresh"):
            # new information from the owner (a confirmed link, a profile fact): prepare again
            app = self._transition(ctx, app, "PREPARATION", "refreshed with new information")
        elif app["state"] not in ("DISCOVERED", "VERIFIED", "ELIGIBILITY_CHECK", "PREPARATION"):
            return {"application_id": app["id"], "state": app["state"], "duplicate": True}
        if opp["verification_status"] != "verified":
            return {"application_id": app["id"], "state": app["state"], "waiting": "opportunity not verified"}
        if app["state"] == "DISCOVERED":
            app = self._transition(ctx, app, "VERIFIED", "verified by Quality agent")
        if app["state"] == "VERIFIED":
            app = self._transition(ctx, app, "ELIGIBILITY_CHECK", "checking against verified profile")

        profile = load_profile()
        facts = verified_facts(profile)
        status, reasons, unknown = check_eligibility(loads(opp["eligibility_json"], []), facts)
        ctx.db.execute("UPDATE opportunities SET eligibility_status=? WHERE id=?", (status, oid))
        if status == "ineligible":
            app = self._transition(ctx, app, "CLOSED", "ineligible: " + "; ".join(reasons))
            return {"application_id": app["id"], "state": "CLOSED", "eligibility": status, "reasons": reasons}

        if app["state"] == "ELIGIBILITY_CHECK":
            app = self._transition(ctx, app, "PREPARATION", f"eligibility {status}")
        answers, missing, docs = self._prepare(ctx, opp, profile, unknown)
        method, dest = submission_method(opp)
        drafts = tailor(ctx, opp, profile)
        ctx.db.execute("UPDATE applications SET answers_json=?, missing_json=?, documents_json=?, submission_method=?, drafts_json=?, "
                       "resume_version=?, updated_at=? WHERE id=?",
                       (dumps(answers), dumps(missing), dumps(docs), method, dumps(drafts), drafts.get("resume_version"), now(), app["id"]))
        app = self._transition(ctx, app, "REVIEW", f"prepared; {len(missing)} missing item(s)")
        from ..core.state import SystemState
        from ..owner_actions import clip, closes_in, missing_label, ref, schedule_deadline_reminders
        schedule_deadline_reminders(ctx.db, opp, ctx.bot.id)
        ctx.send(MessageType.RESEARCH_FINDING, {"application_id": app["id"], "opportunity_id": oid, "missing": missing},
                 topic="application.ready", dedupe_key=f"appready:{app['id']}")
        elig = {"eligible": "you meet every stated criterion", "needs_info": "not confirmed yet, some details are missing",
                "unknown": "not checked"}.get(status, status)
        test = " Test mode is on, so it will only be rehearsed." if SystemState(ctx.db).dry_run else ""
        ats = drafts.get("ats") or {}
        extra = []
        if ats.get("score") is not None:
            extra.append(f"Resume keyword match: {ats['score']}%" + (f" (missing: {', '.join(ats['missing'][:5])})" if ats.get("missing") else ""))
        if drafts.get("cover_letter") or drafts.get("bullets"):
            extra.append(f"Drafted for you: {', '.join(k for k, v in (('cover letter', drafts.get('cover_letter')), ('resume bullets', drafts.get('bullets')), ('common answers', drafts.get('answers'))) if v)}. "
                         f"Copy them from /drafts {ref(app['id'])} or the dashboard.")
        if drafts.get("missing_facts"):
            extra.append("Facts I'd need to say more: " + ", ".join(drafts["missing_facts"][:5]))
        msg = (f"📝 Application draft ready{' (needs your input)' if missing else ''}\n\n{clip(opp['title'], 120)}\n"
               f"{closes_in(opp['deadline'])}\nEligibility: {elig}" + "".join("\n" + x for x in extra)
               + (f"\nStill needed from you: {', '.join(missing_label(m) for m in missing[:8])}. Send /missing to see how to add them."
                  if missing else "\nNothing is missing.")
               + ("\n\nNext: submit it on the organiser's site, then send /submitted " + ref(app['id']) + " <confirmation>."
                  if method == "web_form" else
                  "\n\nNext: you confirmed this one, so I'll send it by email as soon as every answer checks out as verified; "
                  "if anything doesn't, I'll ask you first." + test
                  if opp["confirmed_by"] and not missing else
                  "\n\nNext: it goes by email, and only after you approve it.")
               + f"\nref {ref(app['id'])}")
        soon = bool(opp["deadline"]) and opp["deadline"] - now() < 72 * 3600
        ctx.notify_owner(msg, key=f"appprep:{app['id']}:{len(missing)}", urgent=soon)
        if method in ("email", "api") and opp["confirmed_by"] and not missing:
            # you already said yes to this opportunity: go straight to submission; the
            # applications policy still checks every answer and the daily cap, and asks you otherwise
            ctx.enqueue_self("submit_application", {"application_id": app["id"]}, idempotency_key=f"autosubmit:{app['id']}")
        return {"application_id": app["id"], "state": "REVIEW", "eligibility": status, "missing": missing,
                "submission_method": method, "destination": dest}

    def _prepare(self, ctx: AgentContext, opp: dict, profile: dict, unknown: list[str]) -> tuple[dict, list[str], list[dict]]:
        answers, missing, docs = {}, list(unknown), []
        verified_answers = {k: v["value"] for k, v in (profile.get("answers") or {}).items()
                            if isinstance(v, dict) and v.get("verified") and v.get("value") not in (None, "")}
        documents = {k: v for k, v in (profile.get("documents") or {}).items() if isinstance(v, dict) and v.get("verified")}
        for mat in loads(opp["required_materials_json"], []):
            key = MATERIAL_TO_DOC.get(mat, mat)
            if key in documents:
                docs.append({"material": mat, "path": documents[key]["path"]})
            elif key in verified_answers:
                answers[key] = {"text": verified_answers[key], "source": f"profile.answers.{key}", "status": "verified"}
            else:
                missing.append(mat)
        if "short_bio" in verified_answers:
            answers["short_bio"] = {"text": verified_answers["short_bio"], "source": "profile.answers.short_bio", "status": "verified"}
            if ctx.has_model("write"):
                facts = {**verified_facts(profile), **verified_answers}
                safe = {k: v for k, v in facts.items() if k not in SENSITIVE_FACTS}
                prompt = ("Write a tailored 120-word 'why I am applying' draft using ONLY these verified facts. Do not add "
                          "achievements, numbers or claims that are not in the facts. If facts are insufficient, say "
                          f"INSUFFICIENT.\nFACTS: {dumps(safe)}\nOPPORTUNITY:\n" + untrusted(opp["summary"] or opp["title"], 3000))
                try:
                    txt = ctx.llm("write", prompt, max_tokens=400).text.strip()
                    if txt and "INSUFFICIENT" not in txt:
                        answers["why_applying"] = {"text": txt, "source": "model draft from verified facts",
                                                   "status": "draft_requires_your_review"}
                except Exception as e:  # noqa: BLE001
                    ctx.log(f"tailoring skipped: {e}", level="debug")
        else:
            missing.append("short_bio (verified)")
        if not verified_facts(profile):
            missing.append("verified profile facts (config/profile.yaml)")
        return answers, sorted(set(missing)), docs

    def submit(self, ctx: AgentContext, task: Task) -> dict:
        app = ctx.db.one("SELECT * FROM applications WHERE id=?", (task.payload["application_id"],))
        if not app:
            raise PermanentError("application not found")
        if app["state"] == "SUBMITTED":
            return {"application_id": app["id"], "state": "SUBMITTED", "duplicate": True}
        opp = ctx.db.one("SELECT * FROM opportunities WHERE id=?", (app["opportunity_id"],))
        missing = loads(app["missing_json"], [])
        method, dest = submission_method(opp)
        if task.payload.get("approval_denied"):
            self._transition(ctx, app, "REVIEW", "submission denied by owner")
            return {"application_id": app["id"], "state": "REVIEW", "denied": True}
        if method == "web_form":
            if app["state"] == "REVIEW":
                self._transition(ctx, app, "READY_TO_SUBMIT", "web form: manual submission required")
            return {"application_id": app["id"], "state": "READY_TO_SUBMIT", "manual": True, "url": dest}
        integration = ctx.services.get("application_submitters", {}).get(method)
        attrs = {"submission_method": "email" if method == "email" else method, "verification_status": opp["verification_status"],
                 "eligibility_status": opp["eligibility_status"], "missing_fields": missing,
                 "all_answers_from_verified_profile": all(a.get("status") == "verified" for a in loads(app["answers_json"], {}).values()),
                 "opportunity_type": opp["type"], "domain": domain(opp["official_url"]),
                 "effort": opp["estimated_effort"] or "high", "requirements": task.payload.get("requirements", []),
                 "owner_confirmed": bool(opp["confirmed_by"])}

        def executor(req):
            if integration is None:
                e = IntegrationRevoked(f"no live '{method}' submission integration is configured; submit manually")
                e.integration = f"application_submit_{method}"
                raise e
            return integration(app, opp)

        res = ctx.act("application.submit", f"submit:{app['id']}", executor, target=dest, attributes=attrs,
                      summary=f"Submit application for {opp['title'][:80]} via {method}")
        if res.status in ("succeeded", "duplicate") and res.evidence and not res.dry_run:
            if app["state"] == "REVIEW":
                app = self._transition(ctx, app, "READY_TO_SUBMIT", "approved")
            self._transition(ctx, app, "SUBMITTED", f"evidence: {res.evidence}")
            ctx.db.execute("UPDATE applications SET evidence=?, confirmation_id=?, follow_up_at=? WHERE id=?",
                           (res.evidence, (res.result or {}).get("confirmation_id"), now() + 14 * 86400, app["id"]))
            from ..pipeline import set_stage
            set_stage(ctx.db, opp["id"], "applied", by=ctx.bot.id, note=f"sent by email: {res.evidence}")
        return {"application_id": app["id"], "action": res.status, "evidence": res.evidence, "reasons": res.reasons}


    def follow_up_sweep(self, ctx: AgentContext, task: Task) -> dict:
        """Submitted applications whose follow-up date passed: ask the owner how it went.
        Also: anything sitting at 'applied' for 14 days with no reply gets one nudge."""
        from ..owner_actions import clip, ref
        from ..pipeline import stale_applied
        nudged = 0
        for o in stale_applied(ctx.db):
            days = int((now() - o["stage_updated_at"]) // 86400)
            ctx.notify_owner(f"⏳ No reply in {days} days\n\n“{clip(o['title'], 100)}”{' at ' + o['organizer'] if o['organizer'] else ''} "
                             "is still at Applied. A short, polite follow-up to the recruiter often helps; or mark it as no reply.",
                             key=f"stale:{o['id']}:{int(o['stage_updated_at'])}",
                             buttons=[[{"text": "👻 No reply", "data": f"st:{o['id']}:ghosted"},
                                       {"text": "⏳ Still waiting", "data": f"st:{o['id']}:wait"}]])
            nudged += 1
        due = ctx.db.all("SELECT a.*, o.title FROM applications a JOIN opportunities o ON o.id=a.opportunity_id "
                         "WHERE a.state='SUBMITTED' AND a.follow_up_at IS NOT NULL AND a.follow_up_at<=?", (now(),))
        for a in due:
            self._transition(ctx, a, "FOLLOW_UP", "follow-up date reached")
            ctx.notify_owner(f"📬 Any news on an application?\n\n“{clip(a['title'], 100)}” was submitted two weeks ago. "
                             f"How did it go?\nReply /outcome {ref(a['id'])} accepted, rejected or no_response.",
                             key=f"followup:{a['id']}")
        return {"follow_ups": len(due), "stale_nudged": nudged}

    def tailor_resume(self, ctx: AgentContext, task: Task) -> dict:
        """On demand (/tailor <ref>, dashboard): ATS score + tailored bullets, without an application."""
        opp = ctx.db.one("SELECT * FROM opportunities WHERE id=?", (task.payload["opportunity_id"],))
        if not opp:
            raise PermanentError("opportunity not found")
        drafts = tailor(ctx, opp, load_profile())
        ctx.memory_set(f"tailor:{opp['id']}", drafts)
        from ..owner_actions import clip, ref
        ats = drafts.get("ats") or {}
        lines = [f"🧵 Resume tailored for “{clip(opp['title'], 80)}”", ""]
        if ats.get("score") is not None:
            lines.append(f"Keyword match: {ats['score']}%. Has: {', '.join(ats['matched'][:8]) or 'none'}. "
                         f"Missing: {', '.join(ats['missing'][:8]) or 'none'}")
        for b in drafts.get("bullets", [])[:5]:
            lines.append(f"• {b['text']}")
        if not drafts.get("resume_version"):
            lines.append("Upload your resume in the dashboard (Settings → Resume) so I can score and tailor it.")
        lines.append(f"\nOne-page PDF: dashboard → Internships → {ref(opp['id'])} → Export PDF")
        ctx.notify_owner("\n".join(lines), key=f"tailor:{opp['id']}:{int(now() // 60)}", force_now=True)
        return {"opportunity_id": opp["id"], "ats": ats, "bullets": len(drafts.get("bullets", []))}

    def prepare_stage(self, ctx: AgentContext, task: Task) -> dict:
        """A listing moved to an OA or interview: a prep plan for that company and round."""
        from ..prep import prep_plan
        opp = ctx.db.one("SELECT * FROM opportunities WHERE id=?", (task.payload["opportunity_id"],))
        if not opp:
            raise PermanentError("opportunity not found")
        plan = prep_plan(ctx, opp)
        if opp.get("organizer"):
            ctx.enqueue_self("company_dossier", {"company": opp["organizer"]},
                             idempotency_key=f"dossier:{opp['id']}:{opp.get('stage')}")
        return {"opportunity_id": opp["id"], "stage": opp["stage"], "sessions": len(plan.get("sessions", []))}


COMMON_QUESTIONS = ["why_this_company", "tell_me_about_yourself", "relevant_project", "availability", "why_this_role"]


def tailor(ctx: AgentContext, opp: dict, profile: dict) -> dict:
    """Drafts for one listing, for the owner to review and copy: a cover letter / "why me",
    3-5 resume bullets mapped to the job description, answers to common questions, and the
    ATS keyword score. Only verified facts and lines from the owner's resume are used; a
    bullet the model can't trace to a resume line is dropped; missing facts are listed."""
    from ..resume import active_resume, ats_score, grounded
    res = active_resume(ctx.db)
    resume_text = res["text"] if res else ""
    jd = f"{opp['title']}\n{opp.get('summary') or ''}\n{loads(opp.get('raw_json'), {}).get('raw_text', '')}"
    out: dict = {"resume_version": res["version"] if res else None, "ats": ats_score(resume_text, jd) if res else None,
                 "status": "draft_requires_your_review"}
    facts = {k: v for k, v in verified_facts(profile).items() if k not in SENSITIVE_FACTS}
    answers = {k: v["value"] for k, v in (profile.get("answers") or {}).items()
               if isinstance(v, dict) and v.get("verified") and v.get("value") not in (None, "")}
    avail = ", ".join(f"{k.replace('_', ' ')}: {facts[k]}" for k in ("available_from", "duration_months", "target_season") if k in facts)
    out["answers"] = {"availability": avail} if avail else {}
    if not ctx.has_model("write") or not (facts or resume_text):
        out["missing_facts"] = [] if facts else ["verified profile facts"]
        if not resume_text:
            out["missing_facts"].append("your resume (upload it in the dashboard)")
        return out
    prompt = ("Write application drafts for a student, as JSON with keys: cover_letter (150-220 words), why_me (60 words), "
              "bullets (3-5 objects {text, maps_to, from_resume}: a resume bullet rephrased to match a requirement in the "
              "listing; maps_to is that requirement; from_resume is the resume line it rephrases, copied exactly), answers "
              f"(object with keys {COMMON_QUESTIONS}), missing_facts (facts you would need but don't have). Use ONLY the "
              "facts, answers and resume lines below. Never add employers, numbers, awards or skills that are not there. "
              "If something isn't covered, leave it out and list it in missing_facts.\n"
              f"FACTS: {dumps(facts)}\nSAVED ANSWERS: {dumps(answers)}\nRESUME:\n{resume_text[:6000]}\nLISTING:\n"
              + untrusted(jd, 5000))
    try:
        data = ctx.llm("write", prompt, max_tokens=1800).json()
    except Exception as e:  # noqa: BLE001  (drafts are optional; the deterministic parts stand)
        ctx.log(f"tailoring skipped: {e}", level="debug")
        return out
    if not isinstance(data, dict):
        return out
    for k in ("cover_letter", "why_me"):
        if isinstance(data.get(k), str) and len(data[k].strip()) > 20:
            out[k] = data[k].strip()[:3000]
    bullets = []
    for b in data.get("bullets") or []:
        if isinstance(b, dict) and isinstance(b.get("text"), str) and grounded(str(b.get("from_resume", "")), resume_text):
            bullets.append({"text": b["text"].strip()[:300], "maps_to": str(b.get("maps_to", ""))[:200],
                            "from_resume": str(b["from_resume"])[:300]})
    out["bullets"] = bullets[:5]
    for k, v in (data.get("answers") or {}).items():
        if k in COMMON_QUESTIONS and isinstance(v, str) and len(v.strip()) > 10:
            out["answers"][k] = v.strip()[:1500]
    out["missing_facts"] = [str(m)[:80] for m in (data.get("missing_facts") or []) if isinstance(m, (str, int))][:8]
    return out


def record_manual_submission(db, application_id: str, confirmation: str, by: str) -> dict:
    """Human-only: you submitted it yourself; store the confirmation as evidence."""
    app = db.one("SELECT * FROM applications WHERE id=?", (application_id,))
    if not app:
        raise ValueError("application not found")
    if app["state"] not in ("REVIEW", "READY_TO_SUBMIT"):
        raise ValueError(f"cannot record submission from state {app['state']}")
    hist = loads(app["history_json"], []) + [{"ts": now(), "from": app["state"], "to": "SUBMITTED", "note": f"manual by {by}"}]
    db.execute("UPDATE applications SET state='SUBMITTED', confirmation_id=?, evidence=?, follow_up_at=?, history_json=?, updated_at=? WHERE id=?",
               (confirmation, f"recorded by {by}: {confirmation}", now() + 14 * 86400, dumps(hist), now(), application_id))
    db.execute("UPDATE opportunities SET application_status='SUBMITTED' WHERE id=?", (app["opportunity_id"],))
    audit(db, by, "application.submitted_manually", None, {"application_id": application_id, "confirmation": confirmation})
    return db.one("SELECT * FROM applications WHERE id=?", (application_id,))
