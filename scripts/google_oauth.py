"""One-time Google OAuth for Gmail (read + drafts) and Calendar (read).

Run this on YOUR OWN computer, not the server:

  1. In Google Cloud Console create an OAuth client of type "Desktop app" and
     enable the Gmail API and Google Calendar API for the project.
  2. GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=... python scripts/google_oauth.py
  3. Sign in in the browser window that opens and approve the listed scopes.
  4. Put the printed GOOGLE_REFRESH_TOKEN in the server's .env (never in Git).

Scopes requested:
  gmail.readonly    read and classify mail
  gmail.compose     create drafts (and send, which policies/email.yaml keeps OFF
                    until you enable it; every send also needs your approval)
  calendar.readonly read busy times to suggest meeting slots
Revoke at any time at https://myaccount.google.com/permissions
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import os
import secrets
import sys
import urllib.parse
import webbrowser

import httpx

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/gmail.compose",
          "https://www.googleapis.com/auth/calendar.readonly",
          # v2.0 calendar sync: events on your own calendar only (deadlines, OAs, interviews; no invites).
          # Already set up v1? Run this script again once to grant it.
          "https://www.googleapis.com/auth/calendar.events"]


def main() -> None:
    cid, secret = os.environ.get("GOOGLE_CLIENT_ID"), os.environ.get("GOOGLE_CLIENT_SECRET")
    if not cid or not secret:
        sys.exit("Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET first (see the docstring).")
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    result: dict = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            result.update(q)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"You can close this tab and return to the terminal.")

        def log_message(self, *a):
            pass

    # Inside Docker (docs/SETUP_GUIDE.md): OAUTH_BIND=0.0.0.0 OAUTH_PORT=8765 with -p 127.0.0.1:8765:8765,
    # so the browser on your PC reaches the container while Google still sees a loopback redirect.
    srv = http.server.HTTPServer((os.environ.get("OAUTH_BIND", "127.0.0.1"), int(os.environ.get("OAUTH_PORT", "0"))), H)
    redirect = f"http://127.0.0.1:{srv.server_port}"
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": cid, "redirect_uri": redirect, "response_type": "code", "scope": " ".join(SCOPES),
        "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256"})
    print("Opening your browser. If it does not open, visit:\n", url)
    webbrowser.open(url)
    srv.handle_request()
    if result.get("state") != state or "code" not in result:
        sys.exit(f"Authorization failed: {result.get('error', 'state mismatch')}")
    r = httpx.post("https://oauth2.googleapis.com/token", data={
        "client_id": cid, "client_secret": secret, "code": result["code"], "code_verifier": verifier,
        "grant_type": "authorization_code", "redirect_uri": redirect}, timeout=30)
    if r.status_code != 200 or "refresh_token" not in r.json():
        sys.exit(f"Token exchange failed: HTTP {r.status_code}")
    print("\nAdd this line to the server's .env:\nGOOGLE_REFRESH_TOKEN=" + r.json()["refresh_token"])


if __name__ == "__main__":
    main()
