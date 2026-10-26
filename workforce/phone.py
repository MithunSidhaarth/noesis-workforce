"""Calling the owner: one gate for every trigger (interview in 1 h, last-day deadline,
a custom agent's call_me tool, /call test).

Every call goes through ctx.act("phone.call_owner") and so through policies/phone.yaml
(off by default, quiet hours, daily cap, minimum gap). A call that the policy blocks,
or that fails, is not lost: the same alert goes to Telegram as an urgent message.
"""
from __future__ import annotations

from .core.util import new_id, now, sha


def phone_status(services: dict) -> str:
    return "configured" if services.get("phone") is not None else "not_configured"


def call_owner(ctx, message: str, reason: str, key: str, fallback_text: str | None = None) -> dict:
    """Returns {"status": placed | dry_run | blocked | failed | not_configured | duplicate, "detail"}."""
    db, client = ctx.db, ctx.services.get("phone")
    message = " ".join((message or "").split())[:600]
    row = db.one("SELECT * FROM phone_calls WHERE key=?", (key,))
    if row and row["status"] in ("placed", "blocked", "failed"):
        return {"status": "duplicate", "detail": row["status"]}
    if not row:
        db.execute("INSERT OR IGNORE INTO phone_calls(id, key, reason, message, status, created_at) VALUES (?,?,?,?,?,?)",
                   (new_id("call"), key, reason, message, "queued", now()))
    out: dict
    if client is None or "phone.call_owner" not in ctx.bot.tools:
        out = {"status": "not_configured", "detail": "no phone provider set up (CALLME_* in .env)"}
    else:
        try:
            res = ctx.act("phone.call_owner", f"call:{sha(key)[:24]}", lambda r: client.call(r.params["message"]),
                          target="owner", params={"message": message}, attributes={"reason": reason},
                          summary=f"call you: {message[:80]}", wait_on_approval=False)
            if res.status in ("succeeded", "duplicate"):
                out = {"status": "placed", "detail": res.evidence or ""}
            elif res.status == "dry_run":
                out = {"status": "dry_run", "detail": "test mode"}
            else:
                out = {"status": "blocked", "detail": "; ".join(res.reasons) or res.error or res.status}
        except Exception as e:  # noqa: BLE001  (a failed call must not lose the alert)
            from .core.util import redact
            out = {"status": "failed", "detail": redact(str(e))[:200]}
    db.execute("UPDATE phone_calls SET status=?, provider_id=?, error=?, placed_at=? WHERE key=?",
               ("placed" if out["status"] in ("placed", "dry_run") else out["status"],
                out["detail"] if out["status"] == "placed" else None,
                None if out["status"] in ("placed", "dry_run") else out["detail"],
                now() if out["status"] in ("placed", "dry_run") else None, key))
    if out["status"] not in ("placed", "dry_run") and fallback_text:
        ctx.notify_owner(fallback_text, key=f"callfb:{key}", urgent=True)
    ctx.log(f"call {reason}: {out['status']}", detail=out["detail"][:120])
    return out


def call_triggers() -> dict:
    from .policies.engine import Policies
    return (Policies().get("phone").get("call_for") or {})


def reminder_call_reason(reminder: dict) -> str | None:
    """Which reminders deserve a phone call (on top of the Telegram message)."""
    k = reminder.get("dedupe_key") or ""
    if k.startswith("stage:") and k.endswith(":1"):
        return "interview_1h"
    if k.startswith("oppdl:") and k.endswith(":1"):
        return "deadline_last_day"
    return None
