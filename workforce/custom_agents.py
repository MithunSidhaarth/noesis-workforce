"""Custom agents: each one is its own Telegram bot (a BotFather token) with one job.

The owner creates them in the dashboard: name, emoji, a template or blank, the bot
token (validated live with Telegram getMe, stored encrypted), the task in their own
words, which tools it may use (a subset of a fixed allow-list), and an optional
schedule. Agents cannot change their own task, tools, token, policies or other
agents: those columns are only written here, from the owner's dashboard / CLI.

The built-in Assistant (id `assistant`) uses the same engine with the main bot: free
text the Chief of Staff can't map to a workflow goes to it.
"""
from __future__ import annotations

import re

from .core.db import Database, dumps, loads
from .core.state import audit
from .core.util import new_id, now
from .secrets import encrypt, mask_token, valid_bot_token

MEMORY_TURNS = 12
BUILTIN_ID = "assistant"

# The fixed allow-list. Each maps to an existing safe path (AgentContext.act + policies);
# none can message anyone but the owner. "needs" are the integrations it depends on.
TOOLS: dict[str, dict] = {
    "create_reminder": {"label": "Set reminders for you", "args": "when (e.g. 'tomorrow 09:00', 'in 2h'), what"},
    "search_opportunities": {"label": "Search your internship list", "args": "query, days (optional), limit (optional)"},
    "today_shortlist": {"label": "Today's best matches and your streak", "args": "(none)"},
    "add_opportunity_url": {"label": "Add a posting from a link", "args": "url, text (optional pasted description)"},
    "draft_email": {"label": "Write Gmail drafts (never sends)", "args": "to, subject, body", "needs": "gmail"},
    "save_note": {"label": "Save notes to your vault", "args": "title, body", "needs": "vault"},
    "set_stage": {"label": "Move an application stage (you confirm)", "args": "ref, stage"},
    "company_dossier": {"label": "Company brief before an OA/interview", "args": "company"},
    "tailor_resume": {"label": "Tailor your resume to a listing", "args": "ref"},
    "find_referrals": {"label": "Referral search links + a drafted note", "args": "ref or company"},
    "log_debrief": {"label": "Log interview questions (your question bank)", "args": "company, stage, questions (list)"},
    "question_bank": {"label": "Read your past interview questions", "args": "company (optional), topic (optional)"},
    "call_me": {"label": "Phone you (urgent only; quiet hours apply)", "args": "message", "needs": "phone"},
    "ask_main_agent": {"label": "Hand a request to the Chief of Staff", "args": "text"},
}

TEMPLATES: dict[str, dict] = {
    "scout": {
        "name": "Scout", "emoji": "🔭", "schedule": "at 08:30", "model_tier": "cheap",
        "tools": ["search_opportunities", "today_shortlist", "add_opportunity_url", "create_reminder", "find_referrals"],
        "task": "You are my Bengaluru internship hunter. Every morning, find the 5 best new SWE / AI / ML / data internships "
                "in Bengaluru or remote-India from my list, closing in the next 2 weeks, and give a one-line pitch for each "
                "(why it fits me). When I send a link, add it. Flag anything that looks like a ghost listing.",
    },
    "apply": {
        "name": "Apply", "emoji": "📝", "schedule": None, "model_tier": "standard",
        "tools": ["search_opportunities", "today_shortlist", "tailor_resume", "set_stage", "create_reminder", "find_referrals",
                  "draft_email"],
        "task": "You are my application writer and tracker. Help me hit my daily quota of quality applications: pick the "
                "listing, tailor my resume, prepare answers from my verified facts only, and keep each application's stage "
                "up to date. Never invent achievements.",
    },
    "assistant": {
        "name": "Assistant", "emoji": "🧑‍💼", "schedule": None, "model_tier": "standard",
        "tools": ["create_reminder", "draft_email", "save_note", "search_opportunities", "ask_main_agent", "call_me"],
        "task": "You are my personal assistant. Handle one-off tasks: reminders, email drafts (never sent), notes, quick "
                "questions. Be brief and concrete.",
    },
    "coach": {
        "name": "Interview Coach", "emoji": "🎤", "schedule": "at 19:00", "model_tier": "standard",
        "tools": ["question_bank", "log_debrief", "company_dossier", "create_reminder"],
        "task": "You are my interview coach. Every evening ask me one DSA or system-design question suited to a SWE/ML intern "
                "and wait for my answer (typed or a voice note). Grade it out of 10: correctness, complexity, communication, "
                "with one concrete improvement. Run a mock interview when I say 'mock <company>'. Use my question bank and "
                "upcoming interviews to pick questions.",
    },
    "inbox": {
        "name": "Inbox", "emoji": "📬", "schedule": "every 6h", "model_tier": "cheap",
        "tools": ["draft_email", "set_stage", "create_reminder"],
        "task": "You summarise recruiter and application email for me and draft replies (as Gmail drafts I review). Point out "
                "anything that changes an application's stage, and deadlines I must not miss.",
    },
}


class AgentError(ValueError):
    """Bad input from the owner; the message is safe to show them."""


SCHEDULE_RE = re.compile(r"^(?:every\s+(\d{1,3})\s*(m|h|d)|at\s+(\d{1,2}):(\d{2})(?:\s+on\s+([a-z, ]+))?)$", re.I)


def parse_schedule(text: str | None) -> dict | None:
    """'every 6h' | 'at 09:00' | 'at 09:00 on mon,thu' -> the scheduler's shape, or None."""
    t = (text or "").strip().lower()
    if not t or t in ("none", "off", "manual"):
        return None
    m = SCHEDULE_RE.match(t)
    if not m:
        raise AgentError("Schedule must look like 'every 6h', 'at 09:00' or 'at 09:00 on mon,thu'.")
    if m.group(1):
        n, unit = int(m.group(1)), m.group(2)
        every = n * {"m": 60, "h": 3600, "d": 86400}[unit]
        if every < 900:
            raise AgentError("The shortest schedule is every 15m.")
        return {"every_s": every, "at_time": None, "weekdays": None}
    hh, mm = int(m.group(3)), int(m.group(4))
    if hh > 23 or mm > 59:
        raise AgentError("Use a 24-hour time like 09:00.")
    days = ",".join(d.strip()[:3] for d in (m.group(5) or "").split(",") if d.strip()) or None
    return {"every_s": None, "at_time": f"{hh:02d}:{mm:02d}", "weekdays": days}


def compute_next_run(schedule: str | None, after: float | None = None, last: float | None = None) -> float | None:
    spec = parse_schedule(schedule)
    if not spec:
        return None
    from .core.settings import get_settings
    from .scheduler import next_run
    return next_run({**spec, "tz": get_settings().timezone, "next_run": last}, after or now())


def _clean_tools(tools: list[str] | None) -> list[str]:
    bad = [t for t in tools or [] if t not in TOOLS]
    if bad:
        raise AgentError(f"Unknown tools: {', '.join(bad)}. Pick from: {', '.join(TOOLS)}.")
    return list(dict.fromkeys(tools or []))


def public(row: dict) -> dict:
    """What the API/dashboard may see: never the token, encrypted or not."""
    r = {k: v for k, v in row.items() if k not in ("telegram_token_enc", "updates_offset")}
    r["tools"] = loads(r.pop("tools_json", "[]"), [])
    r["has_token"] = bool(row.get("telegram_token_enc"))
    return r


def ensure_builtin(db: Database) -> None:
    if db.scalar("SELECT 1 FROM custom_agents WHERE id=?", (BUILTIN_ID,)):
        return
    t = TEMPLATES["assistant"]
    db.execute("INSERT OR IGNORE INTO custom_agents(id, name, emoji, template, task, tools_json, model_tier, enabled, builtin, "
               "created_at, updated_at) VALUES (?,?,?,?,?,?,?,1,1,?,?)",
               (BUILTIN_ID, "Assistant (main bot)", t["emoji"], "assistant", t["task"], dumps(t["tools"]), t["model_tier"],
                now(), now()))


def validate_token(token: str, me=None) -> dict:
    """Calls Telegram getMe with the token. `me` is injectable for tests."""
    import os
    token = (token or "").strip()
    if token and token == os.environ.get("TELEGRAM_BOT_TOKEN", "").strip():
        raise AgentError("That's the main NOESIS bot's token. Each agent needs its own bot: create one with /newbot in @BotFather.")
    if not valid_bot_token(token):
        raise AgentError("That doesn't look like a BotFather token (it looks like 123456789:AA...). Copy it again from @BotFather.")
    if me is None:
        from integrations.telegram.client import TelegramClient
        me = lambda tok: TelegramClient(tok, "0").me()  # noqa: E731
    try:
        info = me(token)
    except Exception as e:  # noqa: BLE001
        from .core.util import redact
        raise AgentError(f"Telegram rejected that token: {redact(str(e))[:150]}. Check it in @BotFather.") from e
    if not info.get("is_bot"):
        raise AgentError("That token doesn't belong to a bot.")
    return {"username": info.get("username"), "id": info.get("id"), "name": info.get("first_name")}


def create_agent(db: Database, *, name: str, task: str, token: str, emoji: str = "🤖", template: str | None = None,
                 tools: list[str] | None = None, schedule: str | None = None, model_tier: str = "standard",
                 by: str = "dashboard", me=None) -> dict:
    name, task = (name or "").strip()[:60], (task or "").strip()[:4000]
    if len(name) < 2 or len(task) < 10:
        raise AgentError("Give the agent a name and describe its task in a sentence or two.")
    if model_tier not in ("cheap", "standard"):
        raise AgentError("Model tier must be cheap or standard.")
    tools = _clean_tools(tools)
    parse_schedule(schedule)
    info = validate_token(token, me)
    if db.scalar("SELECT 1 FROM custom_agents WHERE telegram_username=?", (info["username"],)):
        raise AgentError(f"@{info['username']} is already one of your agents.")
    aid = new_id("ag")
    db.execute("INSERT INTO custom_agents(id, name, emoji, template, telegram_token_enc, telegram_username, token_hint, task, "
               "tools_json, schedule, model_tier, enabled, next_run, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)",
               (aid, name, (emoji or "🤖")[:8], template if template in TEMPLATES else None, encrypt(token.strip()),
                info["username"], mask_token(token), task, dumps(tools), (schedule or "").strip() or None, model_tier,
                compute_next_run(schedule), now(), now()))
    audit(db, by, "custom_agent.created", None, {"agent_id": aid, "username": info["username"], "tools": tools})
    return public(db.one("SELECT * FROM custom_agents WHERE id=?", (aid,)))


def update_agent(db: Database, agent_id: str, by: str = "dashboard", me=None, **fields) -> dict:
    row = db.one("SELECT * FROM custom_agents WHERE id=?", (agent_id,))
    if not row:
        raise AgentError("No agent with that id.")
    sets, args = [], []
    if fields.get("name") is not None:
        sets.append("name=?")
        args.append(str(fields["name"]).strip()[:60])
    if fields.get("emoji") is not None:
        sets.append("emoji=?")
        args.append(str(fields["emoji"])[:8])
    if fields.get("task") is not None:
        if len(str(fields["task"]).strip()) < 10:
            raise AgentError("Describe the task in a sentence or two.")
        sets.append("task=?")
        args.append(str(fields["task"]).strip()[:4000])
    if fields.get("tools") is not None:
        sets.append("tools_json=?")
        args.append(dumps(_clean_tools(fields["tools"])))
    if "schedule" in fields and fields["schedule"] is not None:
        sched = str(fields["schedule"]).strip() or None
        parse_schedule(sched)
        sets += ["schedule=?", "next_run=?"]
        args += [sched, compute_next_run(sched)]
    if fields.get("model_tier") in ("cheap", "standard"):
        sets.append("model_tier=?")
        args.append(fields["model_tier"])
    if fields.get("enabled") is not None:
        sets.append("enabled=?")
        args.append(int(bool(fields["enabled"])))
    if fields.get("token"):
        if row["builtin"]:
            raise AgentError("The built-in assistant uses the main bot's token.")
        info = validate_token(fields["token"], me)
        sets += ["telegram_token_enc=?", "telegram_username=?", "token_hint=?", "updates_offset=NULL"]
        args += [encrypt(fields["token"].strip()), info["username"], mask_token(fields["token"])]
    if not sets:
        return public(row)
    sets.append("updated_at=?")
    args.append(now())
    db.execute(f"UPDATE custom_agents SET {', '.join(sets)} WHERE id=?", (*args, agent_id))
    audit(db, by, "custom_agent.updated", None, {"agent_id": agent_id, "fields": sorted(k for k in fields if k != "token")
                                                 + (["token"] if fields.get("token") else [])})
    return public(db.one("SELECT * FROM custom_agents WHERE id=?", (agent_id,)))


def delete_agent(db: Database, agent_id: str, by: str = "dashboard") -> None:
    row = db.one("SELECT builtin FROM custom_agents WHERE id=?", (agent_id,))
    if not row:
        raise AgentError("No agent with that id.")
    if row["builtin"]:
        raise AgentError("The built-in assistant can be turned off, not deleted.")
    db.execute("DELETE FROM custom_agents WHERE id=?", (agent_id,))
    db.execute("DELETE FROM custom_agent_messages WHERE agent_id=?", (agent_id,))
    audit(db, by, "custom_agent.deleted", None, {"agent_id": agent_id})


def list_agents(db: Database) -> list[dict]:
    ensure_builtin(db)
    out = []
    day = now() - 86400
    for r in db.all("SELECT * FROM custom_agents ORDER BY builtin DESC, created_at"):
        p = public(r)
        p["messages_24h"] = db.scalar("SELECT COUNT(*) FROM custom_agent_messages WHERE agent_id=? AND ts>?", (r["id"], day))
        out.append(p)
    return out


# ---- memory --------------------------------------------------------------------
def remember(db: Database, agent_id: str, role: str, text: str) -> None:
    db.execute("INSERT INTO custom_agent_messages(agent_id, role, text, ts) VALUES (?,?,?,?)", (agent_id, role, text[:4000], now()))
    # keep the last 200 turns per agent; the prompt uses the last MEMORY_TURNS
    db.execute("DELETE FROM custom_agent_messages WHERE agent_id=? AND id NOT IN (SELECT id FROM custom_agent_messages "
               "WHERE agent_id=? ORDER BY id DESC LIMIT 200)", (agent_id, agent_id))


def recent_turns(db: Database, agent_id: str, n: int = MEMORY_TURNS) -> list[dict]:
    return list(reversed(db.all("SELECT role, text, ts FROM custom_agent_messages WHERE agent_id=? ORDER BY id DESC LIMIT ?",
                                (agent_id, n))))


def forget(db: Database, agent_id: str) -> int:
    return db.execute("DELETE FROM custom_agent_messages WHERE agent_id=?", (agent_id,)).rowcount


def due_runs(db: Database, t: float | None = None) -> list[dict]:
    t = t or now()
    return db.all("SELECT id, schedule, next_run FROM custom_agents WHERE enabled=1 AND schedule IS NOT NULL AND next_run IS NOT NULL "
                  "AND next_run<=?", (t,))
