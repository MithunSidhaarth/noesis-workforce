"""Resume store, ATS keyword score and a one-page PDF export.

The resume is the owner's own document, so its lines count as verified material:
tailored bullets may only rephrase a line that is in it (checked here), never add
a project, number or skill that isn't. The ATS score is deterministic: which
important keywords from the job description appear in the resume.
"""
from __future__ import annotations

import io
import re
from pathlib import Path

from .core.db import Database
from .core.settings import get_settings
from .core.state import audit
from .core.util import new_id, now

MAX_BYTES = 5_000_000

# Skills and terms recruiters' ATS filters look for in SWE / AI / ML / data internships.
LEXICON = [
    "python", "java", "c++", "c", "go", "golang", "rust", "javascript", "typescript", "kotlin", "swift", "scala", "sql", "bash",
    "react", "next.js", "node.js", "node", "express", "django", "flask", "fastapi", "spring", "spring boot", "graphql", "rest",
    "html", "css", "tailwind", "redux", "vue", "angular", "android", "ios", "flutter",
    "machine learning", "deep learning", "nlp", "computer vision", "llm", "llms", "rag", "transformers", "pytorch", "tensorflow",
    "keras", "scikit-learn", "pandas", "numpy", "hugging face", "langchain", "openai", "prompt engineering", "fine-tuning",
    "reinforcement learning", "statistics", "data analysis", "data science", "data engineering", "etl", "spark", "hadoop",
    "kafka", "airflow", "dbt", "tableau", "power bi", "excel",
    "aws", "gcp", "azure", "docker", "kubernetes", "terraform", "linux", "git", "ci/cd", "microservices", "distributed systems",
    "system design", "data structures", "algorithms", "dsa", "oop", "operating systems", "dbms", "computer networks",
    "postgresql", "mysql", "mongodb", "redis", "elasticsearch", "firebase", "sqlite",
    "testing", "unit testing", "selenium", "jest", "pytest", "agile", "scrum", "jira",
    "api", "backend", "frontend", "full stack", "mobile", "cloud", "security", "blockchain", "web3",
]
_TERMS = sorted(set(LEXICON), key=len, reverse=True)


class ResumeError(ValueError):
    """Bad upload; the message is safe to show the owner."""


def _rx(term: str) -> re.Pattern:
    return re.compile(rf"(?<![a-z0-9+#.]){re.escape(term)}(?![a-z0-9+#])", re.IGNORECASE)


def keywords(text: str, limit: int = 30) -> list[str]:
    """Important terms in a job description, most frequent first (lexicon terms only: no guessing)."""
    low = (text or "").lower()
    counts = []
    for t in _TERMS:
        n = len(_rx(t).findall(low))
        if n:
            counts.append((n, t))
    seen, out = set(), []
    for _, t in sorted(counts, key=lambda x: -x[0]):
        if t == "c" and "c++" in seen:
            continue
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out[:limit]


def ats_score(resume_text: str, jd_text: str) -> dict:
    """{"score": 0..100, "matched": [...], "missing": [...]} against the JD's keywords."""
    want = keywords(jd_text)
    if not want:
        return {"score": None, "matched": [], "missing": [], "note": "the listing names no skills I can match"}
    have = [k for k in want if _rx(k).search(resume_text or "")]
    missing = [k for k in want if k not in have]
    return {"score": round(100 * len(have) / len(want)), "matched": have, "missing": missing}


def extract_text(filename: str, data: bytes) -> str:
    name = filename.lower()
    if name.endswith(".pdf"):
        from pypdf import PdfReader
        try:
            reader = PdfReader(io.BytesIO(data))
            text = "\n".join((p.extract_text() or "") for p in reader.pages[:6])
        except Exception as e:  # noqa: BLE001
            raise ResumeError(f"I couldn't read that PDF ({type(e).__name__}). Export it again or upload a .txt/.md version.") from e
    elif name.endswith((".txt", ".md")):
        text = data.decode("utf-8", errors="replace")
    else:
        raise ResumeError("Upload the resume as PDF, .txt or .md.")
    text = re.sub(r"[ \t]+", " ", text).strip()
    if len(text) < 200:
        raise ResumeError("That file has almost no text (a scanned image?). Upload a text-based PDF or a .txt/.md file.")
    return text[:30000]


def save_resume(db: Database, filename: str, data: bytes, by: str) -> dict:
    if len(data) > MAX_BYTES:
        raise ResumeError("The resume must be under 5 MB.")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", Path(filename).name)[:80] or "resume.pdf"
    text = extract_text(safe, data)
    n = (db.scalar("SELECT COUNT(*) FROM resumes") or 0) + 1
    version = f"v{n}"
    d = Path(get_settings().data_dir) / "resume"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{version}-{safe}"
    path.write_bytes(data)
    rid = new_id("res")
    db.execute("UPDATE resumes SET active=0")
    db.execute("INSERT INTO resumes(id, version, filename, path, text, active, uploaded_at) VALUES (?,?,?,?,?,1,?)",
               (rid, version, safe, str(path), text, now()))
    audit(db, by, "resume.uploaded", None, {"version": version, "filename": safe})
    return {"id": rid, "version": version, "filename": safe, "chars": len(text)}


def active_resume(db: Database) -> dict | None:
    try:
        return db.one("SELECT * FROM resumes WHERE active=1 ORDER BY uploaded_at DESC LIMIT 1")
    except Exception:  # noqa: BLE001  (not migrated yet)
        return None


def resume_lines(text: str) -> list[str]:
    return [ln.strip(" •-*\t") for ln in (text or "").splitlines() if len(ln.strip(" •-*\t")) >= 15]


def grounded(line: str, resume_text: str, min_overlap: float = 0.6) -> bool:
    """A claimed source line is really in the resume (most of its words, in a line of it)."""
    words = set(re.findall(r"[a-z0-9+#.]{3,}", (line or "").lower()))
    if not words:
        return False
    for ln in resume_lines(resume_text):
        have = set(re.findall(r"[a-z0-9+#.]{3,}", ln.lower()))
        if have and len(words & have) / len(words) >= min_overlap:
            return True
    return False


def _latin(s: str) -> str:
    return (s or "").replace("₹", "Rs.").replace("–", "-").replace("—", "-").replace("•", "-").replace("’", "'") \
        .replace("“", '"').replace("”", '"').encode("latin-1", "replace").decode("latin-1")


def one_page_pdf(name: str, contact: str, sections: list[tuple[str, list[str]]]) -> bytes:
    """A plain single-column, ATS-friendly one-page PDF (text, no tables or images).
    Font size shrinks until it fits on one page."""
    from fpdf import FPDF
    for size in (10.5, 10, 9.5, 9, 8.5, 8):
        pdf = FPDF(format="A4", unit="mm")
        pdf.set_auto_page_break(False)
        pdf.add_page()
        pdf.set_margins(14, 12, 14)
        pdf.set_xy(14, 12)
        pdf.set_font("Helvetica", "B", size + 6)
        pdf.cell(0, 8, _latin(name), new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", size - 0.5)
        pdf.multi_cell(0, 4.5, _latin(contact), new_x="LMARGIN", new_y="NEXT")
        for title, items in sections:
            if not items:
                continue
            pdf.ln(1.5)
            pdf.set_font("Helvetica", "B", size + 1)
            pdf.cell(0, 6, _latin(title.upper()), new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("Helvetica", "", size)
            for it in items:
                pdf.multi_cell(0, size * 0.47, _latin(f"- {it}"), new_x="LMARGIN", new_y="NEXT")
        if pdf.get_y() <= 285 or size == 8:
            return bytes(pdf.output())
    return b""
