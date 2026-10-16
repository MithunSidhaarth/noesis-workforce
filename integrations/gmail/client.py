"""Gmail + Google Calendar via official REST APIs with an OAuth refresh token.

Setup creates a Google Cloud OAuth client (Desktop app) and a refresh token for
your account with least-privilege scopes:
  gmail.readonly + gmail.compose (drafts)            -> read + draft
  gmail.send                                          -> only if you enable sending
  calendar.readonly (+ calendar.events to create)     -> availability / events
See docs/INTEGRATIONS.md. Tokens live server-side in .env, never in the DB.
"""
from __future__ import annotations

import base64
import os
import time
from email.message import EmailMessage

import httpx

from workforce.policies.engine import IntegrationError, IntegrationRevoked

TOKEN_URL = "https://oauth2.googleapis.com/token"


def _revoked(msg: str) -> IntegrationRevoked:
    e = IntegrationRevoked(msg)
    e.integration = "google"  # type: ignore[attr-defined]
    return e


class GoogleAuth:
    def __init__(self, transport=None):
        self.client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
        self.client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")
        self.refresh_token = os.environ.get("GOOGLE_REFRESH_TOKEN", "")
        self.http = httpx.Client(transport=transport, timeout=30)
        self._token, self._exp = "", 0.0

    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.refresh_token)

    def token(self) -> str:
        if not self.configured():
            raise _revoked("Google is not configured (GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REFRESH_TOKEN)")
        if self._token and time.time() < self._exp - 60:
            return self._token
        r = self.http.post(TOKEN_URL, data={"client_id": self.client_id, "client_secret": self.client_secret,
                                            "refresh_token": self.refresh_token, "grant_type": "refresh_token"})
        if r.status_code in (400, 401) and "invalid_grant" in r.text:
            raise _revoked("Google refresh token was revoked or expired; re-run scripts/google_oauth.py")
        if r.status_code != 200:
            raise IntegrationError(f"Google token refresh failed HTTP {r.status_code}")
        d = r.json()
        self._token, self._exp = d["access_token"], time.time() + d.get("expires_in", 3600)
        return self._token

    def request(self, method: str, url: str, **kw) -> dict:
        r = self.http.request(method, url, headers={"Authorization": f"Bearer {self.token()}"}, **kw)
        if r.status_code == 401:
            raise _revoked("Google rejected the access token")
        if r.status_code == 403:
            raise _revoked(f"Google permission denied (missing scope?): {r.text[:150]}")
        if r.status_code >= 400:
            raise IntegrationError(f"Google API HTTP {r.status_code}: {r.text[:200]}")
        return r.json() if r.content else {}


class GmailClient:
    BASE = "https://gmail.googleapis.com/gmail/v1/users/me"

    def __init__(self, auth: GoogleAuth):
        self.auth = auth

    def configured(self) -> bool:
        return self.auth.configured()

    def list_recent(self, query: str = "newer_than:2d -category:promotions", max_results: int = 25) -> list[dict]:
        ids = self.auth.request("GET", f"{self.BASE}/messages", params={"q": query, "maxResults": max_results}).get("messages", [])
        out = []
        for m in ids:
            d = self.auth.request("GET", f"{self.BASE}/messages/{m['id']}", params={
                "format": "metadata", "metadataHeaders": ["From", "Subject", "Date", "List-Unsubscribe"]})
            h = {x["name"].lower(): x["value"] for x in d.get("payload", {}).get("headers", [])}
            out.append({"id": d["id"], "thread_id": d.get("threadId"), "from": h.get("from", ""), "subject": h.get("subject", ""),
                        "snippet": d.get("snippet", ""), "received_at": int(d.get("internalDate", 0)) / 1000,
                        "list_unsubscribe": bool(h.get("list-unsubscribe")), "labels": d.get("labelIds", [])})
        return out

    def _raw(self, to: str, subject: str, body: str, thread_id: str | None = None, in_reply_to: str | None = None,
             attachments: list | None = None) -> dict:
        import mimetypes
        from pathlib import Path
        m = EmailMessage()
        m["To"], m["Subject"] = to, subject
        if in_reply_to:
            m["In-Reply-To"] = m["References"] = in_reply_to
        m.set_content(body)
        for f in attachments or []:
            f = Path(f)
            ctype = (mimetypes.guess_type(f.name)[0] or "application/octet-stream").split("/", 1)
            m.add_attachment(f.read_bytes(), maintype=ctype[0], subtype=ctype[1], filename=f.name)
        d = {"raw": base64.urlsafe_b64encode(m.as_bytes()).decode()}
        if thread_id:
            d["threadId"] = thread_id
        return d

    def create_draft(self, to: str, subject: str, body: str, thread_id: str | None = None) -> dict:
        d = self.auth.request("POST", f"{self.BASE}/drafts", json={"message": self._raw(to, subject, body, thread_id)})
        return {"draft_id": d["id"], "evidence": f"gmail draft {d['id']}"}

    def send(self, to: str, subject: str, body: str, thread_id: str | None = None, attachments: list | None = None) -> dict:
        d = self.auth.request("POST", f"{self.BASE}/messages/send", json=self._raw(to, subject, body, thread_id, attachments=attachments))
        return {"message_id": d["id"], "evidence": f"gmail message {d['id']}"}


class CalendarClient:
    BASE = "https://www.googleapis.com/calendar/v3"

    def __init__(self, auth: GoogleAuth, calendar_id: str = "primary"):
        self.auth, self.cal = auth, calendar_id

    def configured(self) -> bool:
        return self.auth.configured()

    def busy(self, start: float, end: float) -> list[tuple[float, float]]:
        from datetime import datetime, timezone
        iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()
        d = self.auth.request("POST", f"{self.BASE}/freeBusy", json={"timeMin": iso(start), "timeMax": iso(end), "items": [{"id": self.cal}]})
        out = []
        for b in d["calendars"][self.cal].get("busy", []):
            out.append((datetime.fromisoformat(b["start"].replace("Z", "+00:00")).timestamp(),
                        datetime.fromisoformat(b["end"].replace("Z", "+00:00")).timestamp()))
        return out

    def create_event(self, summary: str, start_iso: str, end_iso: str, attendees: list[str] | None = None,
                     send_updates: bool = False) -> dict:
        body = {"summary": summary, "start": {"dateTime": start_iso}, "end": {"dateTime": end_iso}}
        if attendees:
            body["attendees"] = [{"email": a} for a in attendees]
        d = self.auth.request("POST", f"{self.BASE}/calendars/{self.cal}/events",
                              params={"sendUpdates": "all" if send_updates else "none"}, json=body)
        return {"event_id": d["id"], "url": d.get("htmlLink"), "evidence": f"calendar event {d['id']}"}
