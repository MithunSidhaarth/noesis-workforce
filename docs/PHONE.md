# Phone calls to you

NOESIS can ring your phone and read out an alert: an hour before an OA or
interview you logged, on the last day of an application you confirmed but haven't
sent, or when one of your agents with the **call me** tool decides something is
urgent. It reuses the settings of the
[call-me](https://github.com/ZeframLou/call-me) Claude Code plugin, so one phone
setup serves both.

## Safety rules (built in)

- Calls go **only** to `CALLME_USER_PHONE_NUMBER` from `.env`. No bot, model or
  message can change the number; the dialler reads it from the environment.
- Every call goes through the policy engine as `phone.call_owner`
  (`policies/phone.yaml`): **off by default**, no calls in quiet hours
  (22:00–08:00), at most 3 a day, at least 30 minutes apart, and only for the
  reasons you leave switched on under `call_for`.
- A call that is blocked or fails is never lost: the same alert goes to Telegram.
- Calls are one-way spoken messages (said twice). NOESIS never calls anyone else
  and never sends SMS.

## Setup with Twilio (the tested path, with fakes)

1. Sign up at [twilio.com](https://www.twilio.com). A trial account works: it can
   call **verified** numbers, and your own number is the only one NOESIS calls.
   (Trial calls start with a short Twilio notice.)
2. Console → Phone Numbers → Verified Caller IDs → add **your** mobile number.
3. Get a Twilio number (the trial gives you one).
4. Voice → Settings → **Geo permissions** → allow **India** (or your country),
   or calls to +91 numbers are refused.
5. In `.env`:
   ```
   CALLME_PHONE_PROVIDER=twilio
   CALLME_PHONE_ACCOUNT_SID=AC...            # Console home page
   CALLME_PHONE_AUTH_TOKEN=...               # Console home page
   CALLME_PHONE_NUMBER=+1...                 # your Twilio number
   CALLME_USER_PHONE_NUMBER=+91...           # your mobile
   ```
6. `policies/phone.yaml`: set `enabled: true` (and adjust quiet hours / caps).
7. Restart (`sudo systemctl restart 'noesis-*'` on the VM, or
   `docker compose up -d`) and send `/call test` on Telegram, or press **Test
   call** in Settings. The result comes back on Telegram.

Telnyx (`CALLME_PHONE_PROVIDER=telnyx`, account = TeXML application id, token =
API key) is implemented but **not tested** against a live account.

## The call-me plugin itself (optional)

The plugin lets an interactive Claude Code session talk with you by phone (two-way,
with speech-to-text). If you want that on the VM too, install it in Claude Code by
following its README; it reads the same `CALLME_*` values plus
`CALLME_OPENAI_API_KEY` and `CALLME_NGROK_AUTHTOKEN` (only the plugin needs those).
NOESIS's own alerts don't use the plugin, OpenAI or ngrok.
