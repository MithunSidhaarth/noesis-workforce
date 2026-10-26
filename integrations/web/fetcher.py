"""Polite HTTP fetcher for public pages and feeds.

* identifies itself with a descriptive User-Agent
* honors robots.txt (cached per host for 24h)
* enforces a minimum interval per host
* supports conditional requests (ETag / Last-Modified)
* caps response size and time
It never logs in, never sends cookies from a user session, and never tries to
get around blocks: a 401/403/429 is reported, not retried aggressively.
"""
from __future__ import annotations

import threading
import time
import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from workforce.core.settings import get_settings
from workforce.policies.engine import IntegrationError

MAX_BYTES = 5_000_000


@dataclass
class FetchResult:
    url: str
    status: int
    content: bytes
    content_type: str
    etag: str | None
    last_modified: str | None
    not_modified: bool = False

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


class FetchBlocked(IntegrationError):
    pass


class Fetcher:
    def __init__(self, transport: httpx.BaseTransport | None = None, min_host_interval_s: float = 5.0,
                 respect_robots: bool = True):
        self.ua = get_settings().http_user_agent
        self.client = httpx.Client(transport=transport, timeout=20, follow_redirects=True,
                                   headers={"User-Agent": self.ua, "Accept-Encoding": "gzip, deflate"})
        self.min_interval = min_host_interval_s
        self.respect_robots = respect_robots
        self._last: dict[str, float] = {}
        self._robots: dict[str, tuple[float, urllib.robotparser.RobotFileParser | None]] = {}
        self._lock = threading.Lock()

    def _robots_ok(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        p = urlparse(url)
        host = f"{p.scheme}://{p.netloc}"
        cached = self._robots.get(host)
        if not cached or time.time() - cached[0] > 86400:
            rp: urllib.robotparser.RobotFileParser | None = urllib.robotparser.RobotFileParser()
            try:
                r = self.client.get(f"{host}/robots.txt")
                if r.status_code >= 400:
                    rp = None  # no robots.txt -> allowed
                else:
                    rp.parse(r.text.splitlines())
            except httpx.HTTPError:
                rp = None
            cached = (time.time(), rp)
            self._robots[host] = cached
        return cached[1] is None or cached[1].can_fetch(self.ua, url)

    def _throttle(self, url: str) -> None:
        host = urlparse(url).netloc
        with self._lock:
            wait = self._last.get(host, 0) + self.min_interval - time.time()
            self._last[host] = max(time.time(), self._last.get(host, 0) + self.min_interval)
        if wait > 0:
            time.sleep(wait)

    def get(self, url: str, etag: str | None = None, last_modified: str | None = None,
            max_bytes: int | None = None) -> FetchResult:
        """max_bytes: per-source cap (SimplifyJobs' listing file is ~13 MB); default 5 MB."""
        limit = int(max_bytes or MAX_BYTES)
        if urlparse(url).scheme not in ("http", "https"):
            raise FetchBlocked(f"refusing non-http url {url}")
        if not self._robots_ok(url):
            raise FetchBlocked(f"robots.txt disallows {url}")
        self._throttle(url)
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        try:
            with self.client.stream("GET", url, headers=headers) as r:
                if r.status_code == 304:
                    return FetchResult(url, 304, b"", "", etag, last_modified, not_modified=True)
                if r.status_code in (401, 403, 451):
                    raise FetchBlocked(f"HTTP {r.status_code} from {url} (access restricted; not retrying around it)")
                if r.status_code == 429 or r.status_code >= 500:
                    raise IntegrationError(f"HTTP {r.status_code} from {url}")
                if r.status_code >= 400:
                    raise IntegrationError(f"HTTP {r.status_code} from {url}")
                buf = bytearray()
                for chunk in r.iter_bytes():
                    buf += chunk
                    if len(buf) > limit:
                        raise IntegrationError(f"response from {url} exceeds {limit} bytes")
                return FetchResult(str(r.url), r.status_code, bytes(buf), r.headers.get("content-type", ""),
                                   r.headers.get("etag"), r.headers.get("last-modified"))
        except httpx.HTTPError as e:
            raise IntegrationError(f"fetch failed for {url}: {e}") from e

    def head_ok(self, url: str) -> tuple[bool, str]:
        try:
            r = self.get(url)
            return True, f"HTTP {r.status} ({r.url})"
        except FetchBlocked as e:
            return False, str(e)
        except IntegrationError as e:
            return False, str(e)
