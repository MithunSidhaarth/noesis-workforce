# Running on Google Cloud (e2-micro, Always Free)

One small VM, no Docker: five systemd services, a nightly backup to Cloud Storage,
and a health check that messages you on Telegram. The dashboard is private: only
your own devices reach it, over Tailscale. Telegram, the custom agents and the
model calls only make outbound connections, so no port is opened to the internet.

Google's free-tier terms below are from memory (October 2026). Check the current
[Free Tier page](https://cloud.google.com/free/docs/free-cloud-features#compute)
when you sign up.

## What was tested

- The systemd units parse (`systemd-analyze verify`), and `setup.sh` and
  `backup.sh` pass `bash -n`. The services' commands are the same ones the tests
  and the local run use.
- `noesisctl.py doctor --alert` ran against a test database.
- A real GCE VM, Tailscale, `gcloud storage` and Claude Code logged in on a VM
  were **not** tested from the build environment. That first run is yours.

## What you get for free (check the page)

- One **e2-micro** VM a month in **us-west1** (Oregon), **us-central1** (Iowa) or
  **us-east1** (South Carolina). Other regions are billed. From India these add
  ~250 ms of latency, which bots don't notice.
- **30 GB** standard persistent disk and **5 GB** of Cloud Storage in those
  regions, plus some free outbound traffic.
- Google has charged for **external IPv4 addresses** since 2024. Check whether
  your free-tier VM's address is billed. The VM needs outbound internet (Telegram,
  models, job boards), so it needs either an external address or Cloud NAT. Set a
  **budget alert** (step 1) so a surprise is a few rupees, not a bill.

An e2-micro has 1 GB of RAM. The setup adds 2 GB of swap, runs all bots in one
worker process, and caps each service's memory (see the table below). The Claude
Code CLI (~260 MB while it answers) runs one call at a time.

## Steps

### 1. Project and budget
1. [console.cloud.google.com](https://console.cloud.google.com): create a project, e.g. `noesis`.
2. Billing → Budgets & alerts → create a budget of ₹100 with alerts at 50%, 90%, 100%.

### 2. The VM
Compute Engine → VM instances → Create instance:
- Region **us-central1** (or us-west1 / us-east1), series **E2**, type **e2-micro**.
- Boot disk: **Debian 12**, **Standard persistent disk**, **30 GB**.
- Firewall: leave HTTP/HTTPS **unticked**. Nothing needs to come in.
- Security → Access scopes: "Set access for each API" → **Storage: Read Write**
  (for the backup copy; or use "Allow full access" and a service account limited to the bucket).

### 3. A bucket for backups
Cloud Storage → Create bucket, e.g. `noesis-backups-<something-unique>`, location
type **Region**, same region as the VM, class **Standard**. Under Lifecycle, add
a rule "Delete object, age 30 days" so it stays inside the free 5 GB.

### 4. Get the code onto the VM
SSH from the console (the "SSH" button), then:
```bash
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/MithunSidhaarth/noesis-workforce.git ~/noesis-workforce
cd ~/noesis-workforce && git checkout main
```
(Private repo: create a fine-grained GitHub token with read access to it and use
`https://<token>@github.com/...` once, or copy the folder with `gcloud compute scp`.)

### 5. Run the setup
```bash
sudo bash deploy/gcp/setup.sh
```
The first run installs everything, creates `/opt/noesis/.env` with a generated
session secret and encryption key, and stops there because the rest of `.env`
is yours to fill in. It takes ~10 minutes (the dashboard build is the slow part).

### 6. Fill in `/opt/noesis/.env`
```bash
sudo -u noesis nano /opt/noesis/.env
```
Required:
- `NOESIS_ADMIN_PASSWORD_HASH`: run
  `sudo -u noesis /opt/noesis/.venv/bin/python /opt/noesis/scripts/hash_password.py` and paste the result.
- `TELEGRAM_BOT_TOKEN` (from @BotFather) and `TELEGRAM_OWNER_ID` (your numeric id, from @userinfobot).

Recommended:
- `GEMINI_API_KEY` and/or `GROQ_API_KEY` (free; the fallback when Claude Code is at its limit; Groq also transcribes voice notes).
- `NOESIS_GCS_BUCKET=noesis-backups-<…>` from step 3.
- `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` / `GOOGLE_REFRESH_TOKEN` for Gmail stage detection and calendar sync
  (run `scripts/google_oauth.py` on your own computer; see INTEGRATIONS).
- `CALLME_*` for phone calls ([PHONE](PHONE.md)).

Already set by the script: `NOESIS_DATA_DIR=/var/lib/noesis`, `NOESIS_DB_PATH`,
`NOESIS_VAULT_PATH`, `NOESIS_PRIVATE_MODE=true`, `NOESIS_WORKER_MODE=shared`,
`CLAUDE_CODE_ENABLED=true`, `NOESIS_SESSION_SECRET`, `NOESIS_SECRET_KEY`.
**Copy `NOESIS_SECRET_KEY` into your password manager**: it decrypts your agents'
bot tokens, and a lost key means pasting each token again.

Then your profile: `sudo -u noesis nano /opt/noesis/config/profile.yaml`.

### 7. Log Claude Code in (once)
```bash
sudo -iu noesis claude
```
Pick "Claude account with subscription", open the link it prints on your phone or
PC, paste the code back, then `/exit`. The bots now write with your Pro/Max plan.
If you're ever logged out, the health check tells you on Telegram and the bots
fall back to Gemini/Groq until you log in again.

### 8. Tailscale, then run the setup again
```bash
sudo tailscale up --ssh          # open the link and log in with the account your phone/PC use
sudo bash ~/noesis-workforce/deploy/gcp/setup.sh
```
The second run starts the services, enables the timers, and prints the
dashboard address: `https://<vm-name>.<your-tailnet>.ts.net`. Open it on a device
signed in to Tailscale.

### 9. Check
```bash
systemctl status 'noesis-*'                       # five services active, two timers waiting
noesisctl doctor                                  # the setup installs this wrapper: runs as noesis with .env loaded
noesisctl test-telegram
```
Then on Telegram send `/status`. The first morning brief arrives at 08:00 IST.

## Services

| Unit | What it runs | MemoryMax |
|---|---|---|
| `noesis-api` | API + dashboard on 127.0.0.1:8000 (`tailscale serve` puts it on your tailnet with HTTPS) | 220 MB |
| `noesis-scheduler` | schedules, housekeeping, custom-agent schedules | 160 MB |
| `noesis-worker` | every bot in one process (`--shared`); the Claude Code CLI runs as its child | 750 MB |
| `noesis-telegram` | the main bot (long polling) | 160 MB |
| `noesis-agents` | your custom agents' bots (one long-poll thread each; reloads every 30 s) | 160 MB |
| `noesis-backup.timer` | 03:30 nightly: SQLite snapshot + config, 7 kept on the VM, copied to the bucket | |
| `noesis-doctor.timer` | every 30 min: `noesisctl doctor --alert`, messages you on Telegram when something is broken (and once when it's fixed) | |

All of them have `Restart=always` and log to the journal: `journalctl -u noesis-worker -f`.

## Update
```bash
cd ~/noesis-workforce && git pull && sudo bash deploy/gcp/setup.sh
```
`.env`, `config/profile.yaml` and the data in `/var/lib/noesis` are never touched.
New database migrations apply on start.

## Restore from a backup
```bash
gcloud storage cp gs://<bucket>/backups/noesis-<date>.tar.gz /tmp/
sudo systemctl stop 'noesis-*'
sudo chmod 644 /tmp/noesis-<date>.tar.gz
sudo -u noesis bash -c 'cd /opt/noesis && set -a && . ./.env && set +a && .venv/bin/python scripts/restore.py /tmp/noesis-<date>.tar.gz'
sudo systemctl start noesis-api noesis-scheduler noesis-worker noesis-telegram noesis-agents
```

## Troubleshooting

- **The dashboard build failed (out of memory).** Build it on your PC
  (`cd apps/dashboard && npm ci && npm run build`) and copy it:
  `gcloud compute scp --recurse apps/dashboard/out <vm>:/tmp/out`, then
  `sudo rsync -a --delete /tmp/out/ /opt/noesis/apps/dashboard/out/ && sudo chown -R noesis: /opt/noesis/apps/dashboard/out && sudo systemctl restart noesis-api`.
- **"Claude Code: LOGGED OUT"** from the health check: step 7 again.
- **A service keeps restarting**: `journalctl -u <unit> -n 100`. `MemoryMax` kills
  show as `oom-kill` in `systemctl status`; raise that unit's `MemoryMax` in
  `/etc/systemd/system/<unit>` or give the VM more swap.
- **An agent shows "can't be decrypted"**: `NOESIS_SECRET_KEY` changed. Put the old
  key back, or paste each agent's token again in the dashboard.
- **Backups not reaching the bucket**: the VM's access scope needs Storage
  read/write (step 2); check with `gcloud storage ls gs://<bucket>` as the noesis user.
