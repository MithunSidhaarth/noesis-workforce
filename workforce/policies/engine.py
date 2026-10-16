"""Deterministic policy engine and the single gateway for external side effects.

Every side-effecting call in the codebase goes through ActionGateway.execute().
The gateway checks, in order: idempotency (no duplicate actions), the bot's tool
grant, global pause, action-specific policy, mandatory-approval flags, and
dry-run. It records an auditable row in external_actions for every outcome.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import yaml

from ..contracts import ActionRequest, BotConfig, Decision, ExternalActionResult, PolicyDecision
from ..core.db import Database, dumps, get_db, loads
from ..core.errors import PermanentError
from ..core.settings import get_settings
from ..core.state import SystemState, audit, exec_log
from ..core.util import new_id, now, redact


class IntegrationError(Exception):
    """Transient integration failure; task should retry."""


class IntegrationRevoked(IntegrationError):
    """Credential missing/revoked; retrying will not help until a human fixes it."""


class ActionInFlight(IntegrationError):
    """Another attempt of the same action is executing right now; retry later."""


IN_FLIGHT_STALE_S = 1800   # an 'executing' row older than this belongs to a crashed attempt


class Policies:
    """Loaded once from disk. Immutable at runtime (frozen copies)."""

    def __init__(self, directory: str | None = None):
        d = Path(directory or get_settings().policies_dir)
        self._data: dict[str, dict] = {}
        for f in sorted(d.glob("*.yaml")):
            self._data[f.stem] = yaml.safe_load(f.read_text(encoding="utf-8")) or {}

    def get(self, name: str) -> dict:
        import copy
        return copy.deepcopy(self._data.get(name, {}))


class PolicyEngine:
    def __init__(self, db: Database | None = None, policies: Policies | None = None):
        self.db = db or get_db()
        self.p = policies or Policies()
        self.state = SystemState(self.db)
        self._rules: dict[str, Callable[[ActionRequest], PolicyDecision | None]] = {
            "social.publish": self._publish_rule,
            "email.send": self._email_rule,
            "calendar.create_event": self._calendar_rule,
            "calendar.send_invite": self._calendar_rule,
            "application.submit": self._application_rule,
            "outreach.send": self._outreach_rule,
        }

    def evaluate(self, req: ActionRequest, bot: BotConfig) -> PolicyDecision:
        g = self.p.get("global")
        actions = g.get("actions", {})
        if req.action not in actions:
            return PolicyDecision(decision=Decision.deny, reasons=[f"unknown action {req.action}"], policy="global")
        if req.action not in bot.tools:
            return PolicyDecision(decision=Decision.deny, reasons=[f"{bot.id} has no grant for {req.action}"], policy="global")
        side_effect = actions[req.action].get("side_effect", True)
        if side_effect and self.state.paused and req.action not in g.get("pause_exempt", []):
            return PolicyDecision(decision=Decision.deny, reasons=["global emergency pause is active"], policy="global",
                                  retry_at=now() + 900)

        approved = bool(req.attributes.get("approval_granted"))
        flags = set(req.attributes.get("flags", []))
        hard = flags & set(g.get("always_require_approval", []))

        rule = self._rules.get(req.action)
        d = rule(req) if rule else None
        if d and d.decision == Decision.deny:
            return d
        if hard and not approved:
            return PolicyDecision(decision=Decision.require_approval,
                                  reasons=[f"requires human approval: {', '.join(sorted(hard))}"], policy="global")
        if d and d.decision == Decision.require_approval and not approved:
            return d

        dry = side_effect and self.state.dry_run and req.action not in g.get("dry_run_exempt", [])
        reasons = (d.reasons if d else []) + (["approved by owner"] if approved else [])
        return PolicyDecision(decision=Decision.allow, reasons=reasons or ["policy checks passed"], dry_run=dry,
                              policy=d.policy if d else "global")

    # ---- action rules ---------------------------------------------------
    def _count_actions(self, action: str, since: float, statuses=("succeeded",), target: str | None = None) -> int:
        extra, args = (" AND target=?", (target,)) if target is not None else ("", ())
        return self.db.scalar(
            f"SELECT COUNT(*) FROM external_actions WHERE action=? AND created_at>=? AND status IN ({','.join('?' * len(statuses))}){extra}",
            (action, since, *statuses, *args)) or 0

    def _day_start(self) -> float:
        tz = ZoneInfo(get_settings().timezone)
        d = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        return d.timestamp()

    def _cap(self, reason: str, policy: str, weekly: bool = False) -> PolicyDecision:
        """A cap clears by itself: block now, retry when the window resets."""
        retry = now() + 86400 if weekly else self._day_start() + 86400 + 300
        return PolicyDecision(decision=Decision.deny, reasons=[reason], policy=policy, retry_at=retry)

    def _publish_rule(self, req: ActionRequest) -> PolicyDecision:
        p = self.p.get("publishing")
        a = req.attributes
        reasons, deny = [], []
        if not p.get("enabled"):
            return PolicyDecision(decision=Decision.require_approval, reasons=["automatic publishing is disabled"], policy="publishing")
        if req.target not in p.get("accounts_allowlist", []):
            deny.append(f"account {req.target} is not allowlisted")
        cat = a.get("category")
        if cat in p.get("approval_categories", []) or cat not in p.get("auto_categories", []):
            return PolicyDecision(decision=Decision.require_approval, reasons=[f"category '{cat}' needs approval"], policy="publishing")
        checks = {
            "require_media_validation": "media_valid", "require_duplicate_check": "not_duplicate",
            "require_license_check": "licenses_ok",
        }
        for k, attr in checks.items():
            if p.get(k) and not a.get(attr):
                deny.append(f"failed check: {attr}")
        if p.get("require_fact_check_when_claims") and a.get("has_claims") and not a.get("facts_verified"):
            deny.append("claims not verified")
        if deny:
            return PolicyDecision(decision=Decision.deny, reasons=deny, policy="publishing")
        per = req.target if p.get("caps_per_account") else None
        caps = {**p, **((p.get("account_caps") or {}).get(req.target) or {})} if per else p
        if self._count_actions("social.publish", self._day_start(), target=per) >= caps.get("max_posts_per_day", 1):
            return self._cap("daily post cap reached", "publishing")
        if self._count_actions("social.publish", now() - 7 * 86400, target=per) >= caps.get("max_posts_per_week", 4):
            return self._cap("weekly post cap reached", "publishing", weekly=True)
        return PolicyDecision(decision=Decision.allow, reasons=reasons or ["publishing policy satisfied"], policy="publishing")

    def _email_rule(self, req: ActionRequest) -> PolicyDecision:
        p = self.p.get("email")
        a = req.attributes
        if not p.get("allow_send"):
            return PolicyDecision(decision=Decision.require_approval, reasons=["email sending is not enabled; draft needs approval"], policy="email")
        dom = (req.target or "").rsplit("@", 1)[-1].lower()
        if a.get("category") not in p.get("auto_reply_categories", []) or dom not in p.get("recipient_allowlist_domains", []):
            return PolicyDecision(decision=Decision.require_approval, reasons=["recipient/category not in auto-reply allowlist"], policy="email")
        if self._count_actions("email.send", self._day_start()) >= p.get("max_sends_per_day", 10):
            return self._cap("daily email cap reached", "email")
        return PolicyDecision(decision=Decision.allow, reasons=["email policy satisfied"], policy="email")

    def _calendar_rule(self, req: ActionRequest) -> PolicyDecision:
        cal = self.p.get("email").get("calendar", {})
        key = "allow_create_event" if req.action == "calendar.create_event" else "allow_send_invites"
        if not cal.get(key):
            return PolicyDecision(decision=Decision.require_approval, reasons=[f"calendar {key} disabled"], policy="email")
        return PolicyDecision(decision=Decision.allow, reasons=["calendar policy satisfied"], policy="email")

    def _application_rule(self, req: ActionRequest) -> PolicyDecision:
        p = self.p.get("applications")
        a = req.attributes
        if a.get("submission_method") not in p.get("allowed_submission_methods", []):
            return PolicyDecision(decision=Decision.deny, reasons=[f"submission method {a.get('submission_method')} not allowed"], policy="applications")
        problems = []
        if a.get("verification_status") != "verified":
            problems.append("opportunity not verified")
        if a.get("eligibility_status") != "eligible":
            problems.append("eligibility not confirmed")
        if a.get("missing_fields"):
            problems.append("required information incomplete")
        if not a.get("all_answers_from_verified_profile"):
            problems.append("answers not fully backed by verified profile")
        bad = set(a.get("requirements", [])) & set(p.get("forbidden_requirements", []))
        if bad:
            problems.append(f"requires {', '.join(sorted(bad))}")
        rule = next((r for r in p.get("auto_submit_rules", []) if _rule_matches(r, a)), None)
        if rule is None:
            problems.append("no explicit auto-submit rule matches")
        if problems:
            return PolicyDecision(decision=Decision.require_approval, reasons=problems, policy="applications")
        if self._count_actions("application.submit", self._day_start()) >= p.get("max_auto_submits_per_day", 5):
            return self._cap("daily auto-submit cap reached", "applications")
        return PolicyDecision(decision=Decision.allow, reasons=[f"matched rule {rule['name']}"], policy="applications")

    def _outreach_rule(self, req: ActionRequest) -> PolicyDecision:
        p = self.p.get("outreach")
        a = req.attributes
        email = (req.target or "").lower()
        if self.db.scalar("SELECT 1 FROM suppression_list WHERE email=?", (email,)):
            return PolicyDecision(decision=Decision.deny, reasons=["recipient is on the suppression list"], policy="outreach")
        if a.get("kind") == "follow_up" and a.get("follow_ups_sent", 0) >= p.get("max_follow_ups", 2):
            return PolicyDecision(decision=Decision.deny, reasons=["follow-up limit reached"], policy="outreach")
        if (a.get("relevance") or 0) < p.get("require_relevance_score", 0.6):
            return PolicyDecision(decision=Decision.deny, reasons=["contact relevance below threshold"], policy="outreach")
        if not p.get("allow_send"):
            return PolicyDecision(decision=Decision.require_approval, reasons=["outreach sending not enabled"], policy="outreach")
        if self._count_actions("outreach.send", self._day_start()) >= p.get("max_sends_per_day", 5):
            return self._cap("daily outreach cap reached", "outreach")
        return PolicyDecision(decision=Decision.allow, reasons=["outreach policy satisfied"], policy="outreach")


def _rule_matches(rule: dict, a: dict) -> bool:
    if rule.get("opportunity_types") and a.get("opportunity_type") not in rule["opportunity_types"]:
        return False
    if rule.get("submission_methods") and a.get("submission_method") not in rule["submission_methods"]:
        return False
    if rule.get("domains") and not any((a.get("domain") or "").endswith(d) for d in rule["domains"]):
        return False
    effort_rank = {"low": 0, "medium": 1, "high": 2}
    if rule.get("require_owner_confirmed") and not a.get("owner_confirmed"):
        return False
    if rule.get("max_effort") and effort_rank.get(a.get("effort", "high"), 2) > effort_rank[rule["max_effort"]]:
        return False
    return True


Executor = Callable[[ActionRequest], dict[str, Any]]


class ActionGateway:
    def __init__(self, db: Database | None = None, engine: PolicyEngine | None = None):
        self.db = db or get_db()
        self.engine = engine or PolicyEngine(self.db)

    def _approval_granted(self, req: ActionRequest) -> str | None:
        r = self.db.one("SELECT id FROM approvals WHERE status='granted' AND idempotency_key=?", (req.idempotency_key,))
        return r["id"] if r else None

    def execute(self, req: ActionRequest, bot: BotConfig, executor: Executor) -> ExternalActionResult:
        g = self.engine.p.get("global")
        prior = self.db.one("SELECT * FROM external_actions WHERE idempotency_key=?", (req.idempotency_key,))
        # only a real success is final. A dry run is a rehearsal: once dry-run is
        # switched off the same action runs for real.
        if prior and (prior["status"] == "succeeded" or (prior["status"] == "dry_run" and self.engine.state.dry_run)):
            return ExternalActionResult(id=prior["id"], idempotency_key=req.idempotency_key, action=req.action,
                                        status="duplicate", dry_run=bool(prior["dry_run"]), evidence=prior["evidence"],
                                        result=loads(prior["result_json"], {}) or {})
        if prior and prior["status"] == "unknown":
            return ExternalActionResult(id=prior["id"], idempotency_key=req.idempotency_key, action=req.action,
                                        status="unknown", error=prior["error"])
        # approval can only come from a human decision stored in the approvals table;
        # an agent (or text it was fed) can never assert it.
        attrs = {k: v for k, v in req.attributes.items() if k != "approval_granted"}
        if self._approval_granted(req):
            attrs["approval_granted"] = True
        req = req.model_copy(update={"attributes": attrs})
        decision = self.engine.evaluate(req, bot)
        aid = prior["id"] if prior else new_id("act")

        def record(status: str, result: dict | None = None, evidence: str | None = None, error: str | None = None):
            t = now()
            error = redact(error) if error else error
            self.db.execute(
                "INSERT INTO external_actions(id, idempotency_key, bot_id, action, target, status, dry_run, request_json, result_json, "
                "evidence, error, task_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(idempotency_key) DO UPDATE SET status=excluded.status, dry_run=excluded.dry_run, "
                "result_json=excluded.result_json, evidence=excluded.evidence, error=excluded.error, updated_at=excluded.updated_at",
                (aid, req.idempotency_key, req.bot_id, req.action, req.target, status, int(decision.dry_run),
                 dumps({"params": req.params, "attributes": req.attributes, "summary": req.summary,
                        "decision": decision.model_dump()}),
                 dumps(result) if result is not None else None, evidence, error, req.task_id, t, t))
            audit(self.db, req.bot_id, f"action.{status}", req.task_id,
                  {"action": req.action, "target": req.target, "key": req.idempotency_key, "reasons": decision.reasons})

        if decision.decision == Decision.deny:
            record("blocked", error="; ".join(decision.reasons))
            return ExternalActionResult(id=aid, idempotency_key=req.idempotency_key, action=req.action, status="blocked",
                                        reasons=decision.reasons, error="; ".join(decision.reasons),
                                        retry_at=decision.retry_at)
        if decision.decision == Decision.require_approval:
            approval_id = self._request_approval(req, decision)
            record("approval_required")
            return ExternalActionResult(id=aid, idempotency_key=req.idempotency_key, action=req.action,
                                        status="approval_required", approval_id=approval_id, reasons=decision.reasons)
        if decision.dry_run:
            record("dry_run", result={"would_execute": req.params}, evidence="dry-run: no external call made")
            return ExternalActionResult(id=aid, idempotency_key=req.idempotency_key, action=req.action, status="dry_run",
                                        dry_run=True, evidence="dry-run: no external call made", reasons=decision.reasons)
        # reserve the key before calling out, so a concurrent or crashed attempt is visible
        state = self._reserve(req, aid, decision, g)
        if state == "in_flight":
            raise ActionInFlight(f"{req.action} {req.idempotency_key} is already executing")
        if state == "unknown":
            msg = ("a previous attempt started but its outcome was never recorded (crash or timeout); "
                   "check the platform before retrying")
            record("unknown", error=msg)
            return ExternalActionResult(id=aid, idempotency_key=req.idempotency_key, action=req.action,
                                        status="unknown", error=msg, reasons=[msg])
        try:
            out = executor(req)
        except IntegrationError as e:
            record("failed", error=str(e))
            raise
        except PermanentError as e:
            record("failed", error=str(e))
            raise
        except Exception as e:
            record("failed", error=f"{type(e).__name__}: {e}")
            raise IntegrationError(str(e)) from e
        evidence = out.get("evidence")
        if not evidence:
            record("failed", result=out, error="executor returned no evidence")
            raise IntegrationError(f"{req.action} returned no evidence of success")
        record("succeeded", result=out, evidence=str(evidence))
        exec_log(self.db, "info", f"{req.action} succeeded", bot_id=req.bot_id, task_id=req.task_id, data={"evidence": evidence})
        return ExternalActionResult(id=aid, idempotency_key=req.idempotency_key, action=req.action, status="succeeded",
                                    evidence=str(evidence), result=out, reasons=decision.reasons)

    def _reserve(self, req: ActionRequest, aid: str, decision: PolicyDecision, g: dict) -> str:
        """Mark the action 'executing' under the write lock. Returns 'ok', 'in_flight' or 'unknown'."""
        t = now()
        with self.db.tx() as c:
            r = c.execute("SELECT status, updated_at FROM external_actions WHERE idempotency_key=?",
                          (req.idempotency_key,)).fetchone()
            if r and r["status"] == "executing":
                if t - r["updated_at"] < IN_FLIGHT_STALE_S:
                    return "in_flight"
                if req.action in g.get("no_blind_retry", []):
                    return "unknown"
            c.execute(
                "INSERT INTO external_actions(id, idempotency_key, bot_id, action, target, status, dry_run, request_json, "
                "task_id, created_at, updated_at) VALUES (?,?,?,?,?,'executing',0,?,?,?,?) "
                "ON CONFLICT(idempotency_key) DO UPDATE SET status='executing', error=NULL, updated_at=excluded.updated_at",
                (aid, req.idempotency_key, req.bot_id, req.action, req.target,
                 dumps({"params": req.params, "attributes": req.attributes, "summary": req.summary,
                        "decision": decision.model_dump()}), req.task_id, t, t))
        return "ok"

    def _request_approval(self, req: ActionRequest, d: PolicyDecision) -> str:
        r = self.db.one("SELECT id FROM approvals WHERE status='pending' AND idempotency_key=?", (req.idempotency_key,))
        if r:
            return r["id"]
        aid = new_id("appr")
        self.db.execute(
            "INSERT INTO approvals(id, bot_id, action, summary, payload_json, reasons_json, task_id, status, created_at, "
            "idempotency_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (aid, req.bot_id, req.action, req.summary or f"{req.action} -> {req.target}",
             dumps({"idempotency_key": req.idempotency_key, "target": req.target, "params": req.params,
                    "attributes": req.attributes}), dumps(d.reasons), req.task_id, "pending", now(),
             req.idempotency_key))
        return aid


def decide_approval(db: Database, approval_id: str, grant: bool, by: str) -> dict | None:
    """Human-only entry point (API / Telegram owner). Resumes the waiting task."""
    from ..core.queue import TaskQueue
    r = db.one("SELECT * FROM approvals WHERE id=?", (approval_id,))
    if not r or r["status"] != "pending":
        return None
    db.execute("UPDATE approvals SET status=?, decided_by=?, decided_at=? WHERE id=?",
               ("granted" if grant else "denied", by, now(), approval_id))
    audit(db, by, "approval.granted" if grant else "approval.denied", r["task_id"], {"approval_id": approval_id})
    if r["task_id"]:
        q = TaskQueue(db)
        if grant:
            q.resume_after_approval(r["task_id"])
        else:
            db.execute("UPDATE tasks SET status='queued', run_after=?, payload_json=json_set(payload_json, '$.approval_denied', ?), "
                       "updated_at=? WHERE id=? AND status='waiting_approval'", (now(), approval_id, now(), r["task_id"]))
    return db.one("SELECT * FROM approvals WHERE id=?", (approval_id,))
