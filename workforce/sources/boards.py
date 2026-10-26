"""Company job boards: live test, probe for a company's ATS slug, and owner add/remove.

Used by the dashboard (Settings → Company boards), Telegram and scripts/check_sources.py,
so all three behave the same. A test or probe fetches the board's official public API
with the polite fetcher (robots.txt, honest User-Agent, size cap) and never stores items."""
from __future__ import annotations

import re

from ..core.db import Database
from ..core.state import audit
from ..core.util import now
from . import parsing
from .registry import ATS, CompanyBoard, board_source, sync_board


class BoardError(ValueError):
    """Bad input from the owner; the message is safe to show them."""


def parse_items(content: bytes, base_url: str, kind: str, parser: dict) -> tuple[int, list[dict]]:
    """(rows before the keep filter, kept items) for any source kind."""
    if kind == "rss":
        items = parsing.parse_feed(content, base_url)
    elif kind == "html":
        items = parsing.parse_html(content, base_url, parser)
    else:
        import json
        data = json.loads(content)
        total = len(parsing._path(data, parser["items"]) if parser.get("items") else data or [])
        del data
        items = parsing.parse_json(content, base_url, parser)
        return total, [i for i in items if parsing.keep_item(i, parser)]
    return len(items), [i for i in items if parsing.keep_item(i, parser)]


def test_board(fetcher, board: CompanyBoard) -> dict:
    """Fetch the board once. {"ok", "total" (all roles), "items" (BLR/India/remote internships), "sample", "error"}."""
    src = board_source(board)
    try:
        r = fetcher.get(src.url, max_bytes=src.parser.get("max_bytes"))
        total, items = parse_items(r.content, r.url, "json", src.parser)
    except Exception as e:  # noqa: BLE001  (shown to the owner as the test result)
        return {"ok": False, "total": 0, "items": 0, "sample": [], "error": str(e)[:300], "url": src.url}
    return {"ok": True, "total": total, "items": len(items), "url": src.url,
            "sample": [{"title": i["title"], "location": i.get("location"), "url": i.get("url")} for i in items[:5]]}


def slug_variants(company: str) -> list[str]:
    base = re.sub(r"[^a-z0-9 ]", "", company.lower()).strip()
    words = base.split()
    joined = "".join(words)
    out = [joined, "-".join(words), words[0] if words else ""]
    out += [joined + suffix for suffix in ("india", "hq", "labs", "tech", "technologies", "inc", "careers")]
    return [s for i, s in enumerate(out) if s and s not in out[:i]]


def probe(fetcher, company: str, domain: str | None = None, ats_list: list[str] | None = None) -> list[dict]:
    """Try each ATS with common slug spellings; return the ones that answered with JSON."""
    found = []
    for ats in ats_list or list(ATS):
        for slug in slug_variants(company):
            res = test_board(fetcher, CompanyBoard(company=company, ats=ats, slug=slug, domain=domain))
            if res["ok"]:
                found.append({"ats": ats, "slug": slug, **res})
                break
    return found


def add_board(db: Database, company: str, ats: str, slug: str, domain: str | None, by: str) -> dict:
    try:
        b = CompanyBoard(company=company.strip(), ats=ats.strip().lower(), slug=slug.strip(),
                         domain=(domain or "").strip().lower().removeprefix("https://").removeprefix("www.").strip("/") or None)
    except Exception as e:  # noqa: BLE001
        raise BoardError(f"That board doesn't look right: {e}") from e
    if db.scalar("SELECT 1 FROM company_boards WHERE id=?", (b.id,)):
        db.execute("UPDATE company_boards SET enabled=1, company=?, domain=COALESCE(?, domain) WHERE id=?", (b.company, b.domain, b.id))
    else:
        db.execute("INSERT INTO company_boards(id, company, ats, slug, domain, interval, enabled, origin, created_at) "
                   "VALUES (?,?,?,?,?,?,1,'dashboard',?)", (b.id, b.company, b.ats, b.slug, b.domain, b.interval, now()))
    sync_board(db, b)
    db.execute("UPDATE sources SET next_check=NULL WHERE id=?", (b.id,))   # checked on the next sweep
    audit(db, by, "board.added", None, {"board": b.id})
    return db.one("SELECT * FROM company_boards WHERE id=?", (b.id,))


def remove_board(db: Database, board_id: str, by: str) -> dict:
    row = db.one("SELECT * FROM company_boards WHERE id=?", (board_id,))
    if not row:
        raise BoardError("No company board with that id.")
    if row["origin"] == "dashboard":
        db.execute("DELETE FROM company_boards WHERE id=?", (board_id,))
        db.execute("DELETE FROM sources WHERE id=?", (board_id,))
    else:   # from config/sources/*.yaml: disable, so the next sync doesn't bring it back
        db.execute("UPDATE company_boards SET enabled=0 WHERE id=?", (board_id,))
        db.execute("UPDATE sources SET enabled=0 WHERE id=?", (board_id,))
    audit(db, by, "board.removed", None, {"board": board_id})
    return {"id": board_id, "deleted": row["origin"] == "dashboard"}


def record_test(db: Database, board_id: str, items: int) -> None:
    db.execute("UPDATE company_boards SET last_test_items=?, last_test_at=? WHERE id=?", (items, now(), board_id))
