"""Phone calls to the owner, reusing the call-me plugin's CALLME_* settings.

The call-me Claude Code plugin (github.com/ZeframLou/call-me) needs a phone
provider account, a number, and the owner's own number. NOESIS reads the same
variables so one setup serves both: interactive Claude Code sessions on the VM can
use the plugin, and the bots place one-way spoken alerts through this client.

  CALLME_PHONE_PROVIDER     twilio | telnyx
  CALLME_PHONE_ACCOUNT_SID  Twilio Account SID, or the Telnyx TeXML application / connection id
  CALLME_PHONE_AUTH_TOKEN   Twilio auth token, or the Telnyx API key
  CALLME_PHONE_NUMBER       the number calls come from (E.164, e.g. +14155550100)
  CALLME_USER_PHONE_NUMBER  YOUR number (E.164, e.g. +9198xxxxxxxx). The only number ever dialled.

The destination is fixed here from the environment. Nothing a bot or a model writes
can change who is called.

Twilio: the message is spoken with inline TwiML on the Calls API, so no webhook or
public URL is needed. Telnyx: the TeXML Calls API with inline TeXML (not yet tested
against a live account; Twilio is the tested path).
"""
from __future__ import annotations

import os
import re
from xml.sax.saxutils import escape

import httpx

from workforce.policies.engine import IntegrationError, IntegrationRevoked

E164 = re.compile(r"^\+[1-9]\d{7,14}$")
MAX_SPOKEN = 600


def twiml(message: str, voice: str = "alice", language: str = "en-IN") -> str:
    """Says the message twice with a pause, so a half-heard first pass is not lost."""
    m = escape(" ".join((message or "").split())[:MAX_SPOKEN])
    say = f'<Say voice="{voice}" language="{language}">{m}</Say>'
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Pause length="1"/>{say}<Pause length="2"/>' \
           f'<Say voice="{voice}" language="{language}">Again.</Say>{say}</Response>'


class PhoneClient:
    def __init__(self, provider: str, account: str, token: str, from_number: str, owner_number: str,
                 transport: httpx.BaseTransport | None = None):
        provider = (provider or "").strip().lower()
        if provider not in ("twilio", "telnyx"):
            raise IntegrationRevoked("CALLME_PHONE_PROVIDER must be twilio or telnyx")
        if not (account and token):
            raise IntegrationRevoked("CALLME_PHONE_ACCOUNT_SID / CALLME_PHONE_AUTH_TOKEN missing")
        for name, num in (("CALLME_PHONE_NUMBER", from_number), ("CALLME_USER_PHONE_NUMBER", owner_number)):
            if not E164.match((num or "").strip()):
                raise IntegrationRevoked(f"{name} must be in international format, e.g. +919812345678")
        self.provider, self.account, self.token = provider, account.strip(), token.strip()
        self.from_number, self.owner_number = from_number.strip(), owner_number.strip()
        self.client = httpx.Client(transport=transport, timeout=20)

    @classmethod
    def from_env(cls) -> "PhoneClient | None":
        """None when not set up at all; raises IntegrationRevoked when half set up."""
        e = os.environ
        if not e.get("CALLME_PHONE_PROVIDER") and not e.get("CALLME_USER_PHONE_NUMBER"):
            return None
        return cls(e.get("CALLME_PHONE_PROVIDER", ""), e.get("CALLME_PHONE_ACCOUNT_SID", ""), e.get("CALLME_PHONE_AUTH_TOKEN", ""),
                   e.get("CALLME_PHONE_NUMBER", ""), e.get("CALLME_USER_PHONE_NUMBER", ""))

    def masked_owner(self) -> str:
        return self.owner_number[:3] + "•" * max(0, len(self.owner_number) - 6) + self.owner_number[-3:]

    def _check(self, r: httpx.Response) -> dict:
        if r.status_code in (401, 403):
            raise IntegrationRevoked(f"{self.provider} rejected the credentials ({r.status_code})")
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code >= 400:
            msg = data.get("message") or (data.get("errors") or [{}])[0].get("detail") or r.text[:200]
            raise IntegrationError(f"{self.provider} call failed {r.status_code}: {str(msg)[:200]}")
        return data

    def call(self, message: str) -> dict:
        """Places the call. Returns {"call_id", "evidence"} once the provider accepted it."""
        try:
            if self.provider == "twilio":
                r = self.client.post(f"https://api.twilio.com/2010-04-01/Accounts/{self.account}/Calls.json",
                                     auth=(self.account, self.token),
                                     data={"To": self.owner_number, "From": self.from_number, "Twiml": twiml(message),
                                           "Timeout": "30"})
                data = self._check(r)
                cid = data.get("sid")
            else:
                r = self.client.post(f"https://api.telnyx.com/v2/texml/calls/{self.account}",
                                     headers={"Authorization": f"Bearer {self.token}"},
                                     json={"To": self.owner_number, "From": self.from_number, "Texml": twiml(message)})
                data = self._check(r)
                cid = (data.get("data") or data).get("call_sid") or (data.get("data") or data).get("sid")
        except httpx.HTTPError as e:
            raise IntegrationError(f"{self.provider} unreachable: {type(e).__name__}") from e
        if not cid:
            raise IntegrationError(f"{self.provider} accepted the request but returned no call id")
        return {"call_id": cid, "evidence": f"{self.provider} call {cid} to {self.masked_owner()}"}
