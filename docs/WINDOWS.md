# Running on your Windows PC

The whole system runs in Docker Desktop on your own computer. It writes
straight into your real Obsidian vault, so you don't need Syncthing, a domain
or a server. The catch is that it only works while the PC is on and awake.
Anything that came due while the PC was asleep runs once when it wakes up.

What was tested: the same containers on Linux, with the vault mounted from a
path that has spaces in it, like yours. The vault was writable, and the
vault's `.system` folder was invisible inside every container. Docker Desktop
on Windows itself was **not** tested; that first run is yours.

## 1. Install Docker Desktop and Ubuntu (one time, about 15 minutes)

1. In PowerShell **as Administrator**: `wsl --install -d Ubuntu`, then restart
   the PC. Ubuntu opens and asks for a Linux username and password.
2. Install **Docker Desktop for Windows** from docker.com and keep *Use WSL 2*
   ticked.
3. In Docker Desktop *Settings*:
   - *General*: tick **Start Docker Desktop when you sign in**.
   - *Resources → WSL integration*: turn on **Ubuntu**.
4. In Windows *Settings → System → Power*: set sleep *when plugged in* to
   **Never**. The bots stop whenever the PC sleeps.

## 2. Get the code (in the Ubuntu window)

Keep the code and database inside Ubuntu, not on `C:`. SQLite is unreliable
and slow on Windows folders shared into Docker.

```bash
sudo apt update && sudo apt -y install git gh
gh auth login                       # GitHub.com → HTTPS → log in with a browser
gh repo clone MithunSidhaarth/noesis-workforce ~/noesis-workforce
cd ~/noesis-workforce
mkdir -p data && sudo chown -R 10001:10001 data
cp config/profile.example.yaml config/profile.yaml
cp prompts/content/guidelines.md.example prompts/content/guidelines.md
cp .env.example .env && chmod 600 .env
```

## 3. Settings (`nano .env`)

Set these lines:

```
COMPOSE_FILE=docker-compose.yml:docker-compose.private.yml
NOESIS_VAULT_HOST=/mnt/c/Users/Mithun Sidhaarth AM/NOESIS
NOESIS_SESSION_SECRET=<paste the output of the first command below>
NOESIS_ADMIN_PASSWORD_HASH=<paste the output of the second command below>
TELEGRAM_BOT_TOKEN=<from @BotFather>
TELEGRAM_OWNER_ID=<your numeric id from @userinfobot>
```

Leave `NOESIS_DRY_RUN=true`.

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
docker compose build api && docker compose run --rm --no-deps api python scripts/hash_password.py
```

## 4. Start it

```bash
docker compose up -d --build
docker compose exec api python scripts/noesisctl.py doctor
docker compose exec api python scripts/noesisctl.py test-telegram
```

`doctor` should show *private mode*, *vault writable at /vault* and no `FIX`
lines. Open **http://localhost:8000** in Edge or Chrome and sign in. Then send
`/status` to your bot on Telegram.

Containers restart on their own after a reboot, once Docker Desktop has started.

## 5. Your vault

- The bots write only their own notes, in `09 Opportunities`, `08 Reviews`,
  `02 Knowledge`, `01 Inbox` and `99 System/Workforce`, and never overwrite a
  note you wrote.
- `.system/` is hidden from every container (an empty folder is mounted over
  it), and the code also refuses any path starting with a dot.
- A note written by a bot appears in Obsidian right away.

## 6. Dashboard on your phone (optional)

Install Tailscale on the PC and your phone and sign in to both. Then, in
PowerShell on the PC:

```powershell
tailscale serve --bg 8000
```

It prints an `https://…ts.net` address that only your own devices can open.

## 7. Backups

Make a backup now and then:

```bash
cd ~/noesis-workforce && docker compose exec -T api python scripts/backup.py
```

The archives are in `\\wsl$\Ubuntu\home\<you>\noesis-workforce\data\backups`.
Copy them somewhere outside the PC from time to time.

## Moving to a server later

Copy `.env`, `config/profile.yaml` and a backup archive to the server. Follow
DEPLOYMENT.md (or ORACLE.md) and restore the backup as described in step 10.
On a server, remove the `NOESIS_VAULT_HOST` line and sync the vault with
Syncthing instead.
