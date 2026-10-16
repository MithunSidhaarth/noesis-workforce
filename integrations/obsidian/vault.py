"""NOESIS Obsidian vault adapter.

Rules (enforced here, not in prompts):
  * The vault root is configurable (NOESIS_VAULT_PATH). Windows paths are only
    valid on Windows; on Linux point it at a synced working copy (Syncthing).
  * Writes are atomic (temp file + os.replace) and confined to the vault root.
  * Notes the workforce did not create are never modified: any existing file
    without `generated_by: noesis-workforce` in its frontmatter is read-only.
  * Every workforce note carries a `workforce_key`; writing the same key again
    updates that note instead of creating a duplicate.
  * Every write is appended to `99 System/Workforce/changelog.md`.
Folder conventions follow the existing NOESIS layout (see noesis.py DIRS).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import unicodedata
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

GENERATED_BY = "noesis-workforce"

DIRS = {
    "dashboard": "00 Dashboard", "inbox": "01 Inbox", "knowledge": "02 Knowledge", "concepts": "03 Concepts",
    "projects": "04 Learning Projects", "tasks": "05 Tasks", "sources": "06 Sources", "daily": "07 Daily Notes",
    "reviews": "08 Reviews", "opportunities": "09 Opportunities", "system": "99 System",
    "workforce": "99 System/Workforce",
}
KNOWLEDGE_DOMAINS = ["AI", "Programming", "Cybersecurity", "Mathematics", "Finance", "Science", "Business", "Other"]


class VaultError(Exception):
    pass


class UserNoteProtected(VaultError):
    pass


def slugify(text: str, maxlen: int = 60) -> str:
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    t = re.sub(r"[^a-zA-Z0-9]+", "-", t.lower()).strip("-")
    return t[:maxlen].rstrip("-") or "note"


def yaml_value(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return json.dumps(str(v), ensure_ascii=False)


def render_frontmatter(fm: dict) -> str:
    return "---\n" + "".join(f"{k}: {yaml_value(v)}\n" for k, v in fm.items()) + "---\n"


def parse_frontmatter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    out = {}
    for line in text[3:end].strip().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            v = v.strip()
            try:
                out[k.strip()] = json.loads(v)
            except json.JSONDecodeError:
                out[k.strip()] = v
    return out


class Vault:
    def __init__(self, root: str | os.PathLike, tz: str = "Asia/Kolkata"):
        if not str(root):
            raise VaultError("vault path is not configured (set NOESIS_VAULT_PATH)")
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise VaultError(f"vault path does not exist on this machine: {self.root}")
        self.tz = tz
        self._index: dict[str, Path] | None = None

    # ---- read -----------------------------------------------------------
    def resolve(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if self.root not in p.parents and p != self.root:
            raise VaultError(f"path escapes the vault: {rel}")
        return p

    def read(self, rel: str) -> str:
        return self.resolve(rel).read_text(encoding="utf-8")

    def structure(self, max_depth: int = 2) -> list[str]:
        out = []
        for p in sorted(self.root.rglob("*")):
            rel = p.relative_to(self.root)
            if any(part.startswith(".") for part in rel.parts) or len(rel.parts) > max_depth:
                continue
            if p.is_dir():
                out.append(str(rel) + "/")
        return out

    def search(self, query: str, limit: int = 10, folders: tuple[str, ...] = ("02 Knowledge", "03 Concepts")) -> list[dict]:
        terms = [t for t in re.findall(r"\w+", query.lower()) if len(t) > 2]
        results = []
        for folder in folders:
            base = self.root / folder
            if not base.is_dir():
                continue
            for p in base.rglob("*.md"):
                try:
                    txt = p.read_text(encoding="utf-8")
                except OSError:
                    continue
                low = txt.lower()
                score = sum(low.count(t) for t in terms)
                if score:
                    fm = parse_frontmatter(txt)
                    results.append({"path": str(p.relative_to(self.root)), "title": fm.get("title") or p.stem,
                                    "score": score, "source_url": fm.get("source_url")})
        return sorted(results, key=lambda r: -r["score"])[:limit]

    def index(self) -> dict[str, Path]:
        """workforce_key -> path for notes this system created."""
        if self._index is None:
            idx = {}
            for p in self.root.rglob("*.md"):
                if any(part.startswith(".") for part in p.relative_to(self.root).parts):
                    continue
                try:
                    head = p.read_text(encoding="utf-8")[:2000]
                except OSError:
                    continue
                if f'generated_by: "{GENERATED_BY}"' in head:
                    k = parse_frontmatter(head).get("workforce_key")
                    if k:
                        idx[k] = p
            self._index = idx
        return self._index

    # ---- write ----------------------------------------------------------
    def _atomic_write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".wf-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def upsert_note(self, key: str, rel_path: str, frontmatter: dict, body: str, actor: str) -> dict:
        """Create or update a workforce-owned note. Returns {path, created, changed}."""
        fm = {**frontmatter, "generated_by": GENERATED_BY, "workforce_key": key}
        content = render_frontmatter(fm) + "\n" + body.rstrip() + "\n"
        existing = self.index().get(key)
        if existing:
            target = existing
        else:
            target = self.resolve(rel_path)
            n = 2
            while target.exists():
                head = target.read_text(encoding="utf-8")[:2000]
                if f'generated_by: "{GENERATED_BY}"' not in head:
                    # never touch a user-authored note; pick a sibling name
                    target = target.with_name(f"{Path(rel_path).stem} ({n}).md")
                    n += 1
                else:
                    raise VaultError(f"workforce note at {target} has a different key")
        created = not target.exists()
        if not created:
            old = target.read_text(encoding="utf-8")
            if parse_frontmatter(old).get("generated_by") != GENERATED_BY:
                raise UserNoteProtected(f"refusing to modify user-authored note {target}")
            if old == content:
                return {"path": str(target.relative_to(self.root)), "created": False, "changed": False}
        self._atomic_write(target, content)
        self.index()[key] = target
        rel = str(target.relative_to(self.root))
        self._log(actor, "created" if created else "updated", rel)
        return {"path": rel, "created": created, "changed": True}

    def _log(self, actor: str, verb: str, rel: str) -> None:
        log = self.resolve(f"{DIRS['workforce']}/changelog.md")
        ts = datetime.now(ZoneInfo(self.tz)).replace(microsecond=0).isoformat()
        line = f"- {ts} · {actor} {verb} [[{Path(rel).with_suffix('').as_posix()}]]\n"
        if log.exists():
            content = log.read_text(encoding="utf-8") + line
        else:
            content = render_frontmatter({"title": "Workforce changelog", "generated_by": GENERATED_BY,
                                          "workforce_key": "workforce-changelog"}) + "\n# Workforce changelog\n\n" + line
        self._atomic_write(log, content)
