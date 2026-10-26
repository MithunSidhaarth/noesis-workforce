"""Check every configured opportunity source from the machine you run this on.

  python scripts/check_sources.py            # enabled sources
  python scripts/check_sources.py --all      # include disabled ones
  python scripts/check_sources.py --probe "Razorpay" [--domain razorpay.com]
                                             # find a company's Greenhouse / Lever / Ashby slug

For each source it fetches the URL with the real fetcher (robots.txt respected,
honest User-Agent), parses it, and prints how many items and how many labelled
deadlines it found. Run it on the VPS after deploying: sources could not be
verified from the build environment, whose network blocks these hosts.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from integrations.web.fetcher import Fetcher  # noqa: E402
from workforce.sources import parsing  # noqa: E402
from workforce.sources.registry import load_sources  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--probe", default="", help="company name to look up on Greenhouse, Lever and Ashby")
    ap.add_argument("--domain", default=None)
    a = ap.parse_args()
    f = Fetcher(min_host_interval_s=1.0)
    if a.probe:
        from workforce.sources.boards import probe
        hits = probe(f, a.probe, a.domain)
        for h in hits:
            print(f"FOUND {h['ats']:10} slug={h['slug']:24} {h['total']:4} roles, {h['items']:3} BLR/India/remote internships")
            print(f"      add to config/sources/feeds.yaml: - {{company: {a.probe}, ats: {h['ats']}, slug: {h['slug']}"
                  + (f", domain: {a.domain}" if a.domain else "") + "}")
        if not hits:
            print(f"No Greenhouse / Lever / Ashby board found for {a.probe!r}. It may use Workday or its own site: "
                  "list it in docs/SOURCES.md and send postings with /add <link>.")
        return 0 if hits else 1
    bad = 0
    for s in load_sources():
        if not s.enabled and not a.all:
            continue
        if s.kind == "fixture":
            continue
        try:
            r = f.get(s.url, max_bytes=s.parser.get("max_bytes"))
            if s.kind == "rss":
                items = parsing.parse_feed(r.content, r.url)
            elif s.kind == "html":
                items = parsing.parse_html(r.content, r.url, s.parser)
            else:
                items = parsing.parse_json(r.content, r.url, s.parser)
            total = len(items)
            items = [i for i in items if parsing.keep_item(i, s.parser)]
            with_deadline = sum(1 for i in items if parsing.extract_deadline(f"{i.get('title', '')} {i.get('body', '')}", "Asia/Kolkata")[0])
            status = "OK " if items else "EMPTY"
            kept = f" (of {total})" if total != len(items) else ""
            print(f"{status} {s.id:28} {len(items):3} items{kept}, {with_deadline:3} with a labelled deadline   {s.url}")
            if not items:
                bad += 1
        except Exception as e:  # noqa: BLE001
            bad += 1
            print(f"FAIL {s.id:28} {type(e).__name__}: {str(e)[:120]}")
    print(f"\n{bad} source(s) need attention." if bad else "\nAll checked sources returned items.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
