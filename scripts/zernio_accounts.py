"""List your Zernio accounts (read-only) so you can copy their ids into config/brands.yaml.

  docker compose exec api python scripts/zernio_accounts.py

Uses ZERNIO_API_KEY from .env. Makes one GET /accounts call; publishes nothing.
"""
from __future__ import annotations

import os
import sys

import httpx


def main() -> int:
    key = os.environ.get("ZERNIO_API_KEY", "")
    if not key:
        print("ZERNIO_API_KEY is not set in .env")
        return 1
    base = (os.environ.get("ZERNIO_API_URL") or "https://zernio.com/api/v1").rstrip("/")
    r = httpx.get(f"{base}/accounts", headers={"Authorization": f"Bearer {key}"}, timeout=30)
    if r.status_code in (401, 403):
        print(f"Zernio rejected the key (HTTP {r.status_code}). Create a new key in the Zernio dashboard.")
        return 1
    r.raise_for_status()
    accounts = r.json().get("accounts", [])
    if not accounts:
        print("No accounts connected yet. Connect LinkedIn and Instagram in the Zernio dashboard first.")
        return 1
    print("Zernio key works. Connected accounts:\n")
    for a in accounts:
        aid = a.get("_id") or a.get("id")
        print(f"  {a.get('platform'):10} {a.get('username') or a.get('displayName') or '':30} id={aid}"
              f"{'' if a.get('isActive', True) else '  (inactive: reconnect it)'}")
    print("\nPut each id in config/brands.yaml as zernio_account, and 'zernio:<id>' in "
          "policies/publishing.yaml accounts_allowlist.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
