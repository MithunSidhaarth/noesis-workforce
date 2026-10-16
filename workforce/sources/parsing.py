"""Source parsers and deterministic opportunity extraction.

Extraction rules: every value we store must be traceable to the source text.
Unknown stays unknown (None / 'unknown'); we never guess a deadline, an
eligibility rule or an application URL.
"""
from __future__ import annotations

import html
import json
import re
from datetime import datetime
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode
from zoneinfo import ZoneInfo

import feedparser
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "ref", "fbclid", "gclid", "igshid"}

TYPE_KEYWORDS = [
    ("hackathon", ["hackathon", "hack-a-thon", "buildathon", "codeathon", "ideathon", "datathon"]),
    ("fellowship", ["fellowship", "fellows program", "fellows programme"]),
    ("scholarship", ["scholarship", "bursary", "tuition waiver"]),
    ("internship", ["internship", "intern ", "interns ", "summer analyst", "industrial training"]),
    ("grant", ["grant", "funding call", "seed fund", "microgrant"]),
    ("competition", ["competition", "challenge", "contest", "olympiad", "case study competition"]),
    ("conference", ["conference", "summit", "symposium", "workshop"]),
    ("volunteering", ["volunteer", "volunteering"]),
    ("event", ["meetup", "webinar", "bootcamp", "event"]),
    ("job", ["full-time", "full time", "graduate program", "new grad", "job opening", "hiring"]),
]

MONTHS = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
DATE_RE = (
    rf"(?:\d{{1,2}}(?:st|nd|rd|th)?\s+{MONTHS},?\s+\d{{4}}"
    rf"|{MONTHS}\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}"
    r"|\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}/\d{1,2}/\d{4})"
)
DEADLINE_RE = re.compile(
    rf"(?:deadline|apply by|applications? close[sd]?|last date(?: to apply| of application)?|closing date|due date|submit by|register by|registration closes?)"
    rf"[^.\n]{{0,40}}?({DATE_RE})(?:[^.\n]{{0,15}}?(\d{{1,2}}(?::\d{{2}})?\s*(?:am|pm)?\s*(?:IST|UTC|GMT|EST|PST|CET|[A-Z]{{2,4}})?))?",
    re.IGNORECASE)
TZ_ABBR = {"IST": "Asia/Kolkata", "UTC": "UTC", "GMT": "UTC", "EST": "America/New_York", "PST": "America/Los_Angeles",
           "CET": "Europe/Berlin"}

ELIGIBILITY_RE = re.compile(r"([^.\n]*(?:eligib|open to|must be|applicants should|who can apply|requirements?:|candidates should)[^.\n]*[.\n])",
                            re.IGNORECASE)
MATERIALS = ["resume", "cv", "cover letter", "statement of purpose", "sop", "transcript", "letter of recommendation",
             "recommendation letter", "portfolio", "essay", "video pitch", "pitch deck", "github", "writing sample", "id proof"]


def clean_text(s: str | None) -> str:
    if not s:
        return ""
    txt = BeautifulSoup(s, "html.parser").get_text(" ") if "<" in s else s
    return re.sub(r"\s+", " ", html.unescape(txt)).strip()


def normalize_url(u: str | None) -> str | None:
    if not u:
        return None
    p = urlparse(u.strip())
    if p.scheme not in ("http", "https") or not p.netloc:
        return None
    q = urlencode([(k, v) for k, v in parse_qsl(p.query) if k.lower() not in TRACKING_PARAMS])
    path = p.path.rstrip("/") or "/"
    return urlunparse(("https" if p.scheme == "http" else p.scheme, p.netloc.lower().removeprefix("www."), path, "", q, ""))


def domain(u: str | None) -> str:
    return urlparse(u or "").netloc.lower().removeprefix("www.")


def normalize_title(t: str) -> str:
    t = re.sub(r"[^a-z0-9 ]+", " ", t.lower())
    t = re.sub(r"\b(20\d\d|the|a|an|of|for|and|apply|now|open|applications?|call)\b", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def classify_type(text: str, default: str | None = None) -> str:
    low = f" {text.lower()} "
    for typ, kws in TYPE_KEYWORDS:
        if any(k in low for k in kws):
            return typ
    return default or "unknown"


def remote_status(text: str) -> str:
    low = text.lower()
    has_remote = any(k in low for k in ("remote", "online", "virtual", "work from home", "wfh"))
    has_onsite = any(k in low for k in ("in-person", "in person", "on-site", "onsite", "offline", "venue"))
    if "hybrid" in low or (has_remote and has_onsite):
        return "hybrid"
    if has_remote:
        return "remote"
    if has_onsite:
        return "in_person"
    return "unknown"


def extract_deadline(text: str, default_tz: str) -> tuple[float | None, str | None, str | None]:
    """Returns (utc_epoch, matched_text, tz). Only dates explicitly labelled as a
    deadline count. Date-only deadlines are taken as 23:59 in the stated or
    default timezone (recorded so it can be shown)."""
    m = DEADLINE_RE.search(text)
    if not m:
        return None, None, None
    date_s, time_s = m.group(1), (m.group(2) or "").strip()
    tzname = default_tz
    for abbr, zone in TZ_ABBR.items():
        if abbr in time_s.upper().split():
            tzname = zone
    try:
        d = dateparser.parse(date_s, dayfirst=True if re.match(r"\d{1,2}/", date_s) else False)
    except (ValueError, OverflowError):
        return None, m.group(0).strip(), None
    tclean = re.sub(r"[A-Za-z]{2,4}$", "", time_s).strip() if time_s else ""
    hh, mm = 23, 59
    if tclean and re.search(r"\d", tclean):
        try:
            t = dateparser.parse(tclean)
            hh, mm = t.hour, t.minute
        except (ValueError, OverflowError):
            pass
    local = datetime(d.year, d.month, d.day, hh, mm, tzinfo=ZoneInfo(tzname))
    return local.timestamp(), m.group(0).strip(), tzname


def extract_eligibility(text: str) -> list[str]:
    out = []
    for m in ELIGIBILITY_RE.finditer(text):
        s = m.group(1).strip()
        if 10 < len(s) < 300 and s not in out:
            out.append(s)
    return out[:8]


def extract_materials(text: str) -> list[str]:
    low = text.lower()
    found = []
    for m in MATERIALS:
        if re.search(rf"\b{re.escape(m)}\b", low) and m not in found:
            found.append(m)
    return found


NOT_OFFICIAL = ("twitter.com", "x.com", "facebook.com", "instagram.com", "linkedin.com", "t.me", "wa.me", "whatsapp.com",
                "youtube.com", "youtu.be", "bit.ly", "tinyurl.com", "medium.com", "reddit.com", "discord.gg", "discord.com")


def body_links(html: str, base_url: str) -> list[str]:
    if not html or "<a" not in html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    return [urljoin(base_url, a.get("href")) for a in soup.select("a[href]")][:20]


def candidate_official_links(links: list[str], listing_url: str | None) -> list[str]:
    """Links inside a listing that might be the organiser's own page. Unconfirmed:
    shown to the owner as a hint, never used as the official URL automatically."""
    own = domain(listing_url) if listing_url else ""
    out = []
    for u in links or []:
        u = normalize_url(u)
        if not u or not u.startswith("https://"):
            continue
        d = domain(u)
        if not d or d == own or d.endswith("." + own) or any(d == s or d.endswith("." + s) for s in NOT_OFFICIAL):
            continue
        if u not in out:
            out.append(u)
    return out[:5]


def parse_feed(content: bytes, base_url: str) -> list[dict]:
    f = feedparser.parse(content)
    items = []
    for e in f.entries:
        body = ""
        if e.get("content"):
            body = " ".join(c.get("value", "") for c in e.content)
        body = body or e.get("summary", "") or e.get("description", "")
        published = None
        for k in ("published_parsed", "updated_parsed"):
            if e.get(k):
                published = datetime(*e[k][:6]).timestamp()
                break
        items.append({"title": clean_text(e.get("title")), "url": urljoin(base_url, e.get("link", "")),
                      "body": clean_text(body), "published": published, "links": body_links(body, base_url),
                      "author": clean_text(e.get("author")) or None,
                      "tags": [t.get("term") for t in e.get("tags", []) if t.get("term")]})
    return items


def parse_html(content: bytes, base_url: str, cfg: dict) -> list[dict]:
    soup = BeautifulSoup(content, "html.parser")
    items = []
    for node in soup.select(cfg["item"]):
        def pick(sel: str | None, attr: str | None = None) -> str:
            if not sel:
                return ""
            el = node.select_one(sel)
            if el is None:
                return ""
            return (el.get(attr) or "") if attr else el.get_text(" ", strip=True)
        link = pick(cfg.get("link", "a"), "href")
        items.append({"title": clean_text(pick(cfg.get("title"))), "url": urljoin(base_url, link) if link else None,
                      "links": [urljoin(base_url, a.get("href")) for a in node.select("a[href]")][:20],
                      "body": clean_text(pick(cfg.get("body")) or node.get_text(" ", strip=True)),
                      "published": None, "author": clean_text(pick(cfg.get("organizer"))) or None, "tags": []})
    return items


def _path(d: Any, path: str) -> Any:
    for part in path.split("."):
        if isinstance(d, list):
            d = d[int(part)] if part.isdigit() and int(part) < len(d) else None
        elif isinstance(d, dict):
            d = d.get(part)
        else:
            return None
    return d


def parse_json(content: bytes, base_url: str, cfg: dict) -> list[dict]:
    data = json.loads(content)
    rows = _path(data, cfg["items"]) if cfg.get("items") else data
    out = []
    for r in rows or []:
        f = cfg.get("fields", {})
        title = _path(r, f.get("title", "title"))
        url = _path(r, f.get("url", "url"))
        body_parts = [str(_path(r, p) or "") for p in f.get("body", [])]
        deadline_raw = _path(r, f["deadline"]) if f.get("deadline") else None
        out.append({"title": clean_text(str(title or "")), "url": urljoin(base_url, url) if url else None,
                    "body": clean_text(" ".join(body_parts)), "published": None,
                    "author": clean_text(str(_path(r, f["organizer"]) or "")) if f.get("organizer") else None,
                    "tags": [], "structured_deadline": deadline_raw})
    return out


def keep_item(item: dict, cfg: dict) -> bool:
    """Source-level relevance filter: `parser: {include_any: [india, bengaluru]}` keeps only matching items."""
    words = [w.lower() for w in cfg.get("include_any") or []]
    if not words:
        return True
    text = f"{item.get('title', '')} {item.get('body', '')} {' '.join(item.get('tags') or [])}".lower()
    return any(w in text for w in words)


def normalize_item(item: dict, source: dict, default_tz: str) -> dict | None:
    title = item.get("title") or ""
    if len(title) < 4:
        return None
    text = f"{title}. {item.get('body', '')}"
    deadline, deadline_text, tz = extract_deadline(text, default_tz)
    if deadline is None and item.get("structured_deadline"):
        try:
            d = dateparser.parse(str(item["structured_deadline"]))
            if d.tzinfo is None:
                d = d.replace(tzinfo=ZoneInfo(default_tz))
            deadline, deadline_text, tz = d.timestamp(), f"deadline field: {item['structured_deadline']}", str(d.tzinfo)
        except (ValueError, OverflowError):
            pass
    url = normalize_url(item.get("url"))
    typ = classify_type(f"{title} {' '.join(item.get('tags', []))}")
    if typ == "unknown":
        typ = classify_type(text, source.get("default_type"))
    return {
        "title": title[:300],
        "organizer": item.get("author") if source.get("author_is_organizer") else None,
        "official_url": url,
        "discovery_url": url,
        "type": typ,
        "location": None,
        "remote_status": remote_status(text),
        "deadline": deadline,
        "deadline_text": deadline_text,
        "deadline_tz": tz,
        "eligibility": extract_eligibility(text),
        "required_materials": extract_materials(text),
        "summary": (item.get("body") or "")[:600],
        "published": item.get("published"),
        "raw_text": text[:8000],
        "candidate_links": candidate_official_links(item.get("links", []), url),
    }


def fingerprint(opp: dict) -> str:
    import hashlib
    key = opp.get("official_url") or ""
    if not key:
        key = normalize_title(opp["title"]) + "|" + (opp.get("organizer") or "").lower()
    return hashlib.sha256(key.encode()).hexdigest()[:32]
