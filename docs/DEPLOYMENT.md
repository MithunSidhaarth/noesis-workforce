# Deployment guide (small Linux VPS)

Target: Ubuntu 24.04 LTS, 2 vCPU, 2–4 GB RAM, 40 GB disk, a domain or
subdomain you control. Everything runs in Docker Compose; your computer can be
off. Commands are run as a sudo-capable user on the VPS unless marked
**(your computer)**.

What was verified while building: the image builds, the full stack starts
(api, worker with ten bot processes, scheduler, telegram, Caddy), every service
reports healthy, HTTPS is served by Caddy (internal certificate on
`localhost`), login works, a killed bot process is restarted, and
backup → wipe → restore returns the data. Not verified: a real domain with a
Let's Encrypt certificate, Syncthing, and the Redis profile.

Using Oracle Cloud's free ARM machine without a domain? Read
[ORACLE.md](ORACLE.md) first; it says which steps below change.

## 1. Prepare the VPS

```bash
sudo apt update && sudo apt -y upgrade
sudo apt -y install git ufw unattended-upgrades
sudo ufw allow OpenSSH && sudo ufw allow 80/tcp && sudo ufw allow 443/tcp && sudo ufw allow 443/udp
sudo ufw allow 22000/tcp && sudo ufw allow 22000/udp     # only if you sync the vault with Syncthing
sudo ufw --force enable
sudo dpkg-reconfigure -plow unattended-upgrades
```

Use SSH keys and disable password login (`PasswordAuthentication no` in
`/etc/ssh/sshd_config`, then `sudo systemctl restart ssh`).

## 2. Install Docker

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER" && newgrp docker
docker compose version
```

Get the code (replace with the repository you push to):

```bash
git clone https://github.com/<you>/noesis-workforce.git ~/noesis-workforce
cd ~/noesis-workforce
mkdir -p data vault && sudo chown -R 10001:10001 data vault   # the containers run as uid 10001
cp config/profile.example.yaml config/profile.yaml             # fill in your verified facts later
cp prompts/content/guidelines.md.example prompts/content/guidelines.md   # replace with your real guidelines
```

## 3. Configure environment variables

```bash
cp .env.example .env && chmod 600 .env
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # → NOESIS_SESSION_SECRET
nano .env
```

Set at least `NOESIS_DOMAIN`, `NOESIS_SESSION_SECRET`, and (next step)
`NOESIS_ADMIN_PASSWORD_HASH`. Leave `NOESIS_DRY_RUN=true`.

## 4. Authentication and HTTPS

```bash
docker compose build
docker compose run --rm --no-deps api python scripts/hash_password.py   # paste the output into .env
```

Point a DNS **A** record for `NOESIS_DOMAIN` at the VPS IP. Caddy obtains and
renews the certificate automatically on first start; ports 80 and 443 must be
reachable. There is no signup page: the only account is the password you just
hashed.

## 5. Database migrations

Migrations in `database/migrations/` are applied automatically, in order and
idempotently, whenever any service starts. To apply them explicitly:

```bash
docker compose run --rm --no-deps api python -c "from workforce.factory import bootstrap; bootstrap(); print('migrated')"
```

## 6. Start the services

```bash
docker compose up -d                          # api, worker, scheduler, telegram, caddy
# optional extras:
docker compose --profile fast up -d           # Redis wake-ups (also set REDIS_URL=redis://redis:6379/0)
docker compose --profile vault up -d          # Syncthing for the Obsidian vault (step 9)
```

## 7. Check health

```bash
docker compose ps                             # every service "healthy" / "running"
curl -s https://your-domain/api/health     # {"ok":true,"scheduler_heartbeat_age_s":…,"bots_alive":10}
docker compose exec api python scripts/noesisctl.py status
docker compose exec api python scripts/check_sources.py   # live check of every opportunity source
docker compose logs -f --tail 50 worker
```

Then open `https://NOESIS_DOMAIN`, sign in, and check the Integrations page.

## 8. Connect Telegram

1. In Telegram, message **@BotFather** → `/newbot` → copy the token.
2. Message **@userinfobot** to get your numeric user id.
3. Put both in `.env` (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_OWNER_ID`), then:

```bash
docker compose up -d
docker compose exec api python scripts/noesisctl.py test-telegram   # sends you a real test message
```

4. Send `/start` to your bot, then `/status`. Messages from any other account
   are ignored. From now on you get one morning brief at 08:00, urgent pings
   only when something can't wait, and buttons on approvals and matches.

## 9. Configure authorized integrations

Follow `docs/INTEGRATIONS.md` one integration at a time (each starts read-only
or draft-only). For the vault:

1. `docker compose --profile vault up -d`
2. **(your computer)** `ssh -L 8384:127.0.0.1:8384 you@vps` and open
   http://127.0.0.1:8384 — set a GUI password, note the server's device ID.
3. **(your computer)** install Syncthing, add the server's device ID, and share
   your `C:/Users/Mithun Sidhaarth AM/NOESIS` folder with it. On the server,
   accept it with folder path `/var/syncthing/vault`.
4. Exclude Obsidian internals and secrets: in the folder's **Ignore Patterns**
   add `.obsidian/workspace*`, `.system/`, `.trash/`.
5. Set `NOESIS_VAULT_PATH=/vault` (already the default in `.env.example`) and
   `docker compose up -d`. The Integrations page shows *obsidian: ok*.

## 10. Backups and restore

```bash
# nightly at 03:15, keeps 14 archives in data/backups
( crontab -l 2>/dev/null; echo '15 3 * * * cd ~/noesis-workforce && docker compose exec -T api python scripts/backup.py >> ~/noesis-backup.log 2>&1' ) | crontab -
# copy them off the server, e.g. from your computer:
scp 'you@vps:~/noesis-workforce/data/backups/*.tar.gz' ./noesis-backups/
```

Restore:

```bash
docker compose stop worker scheduler telegram api
docker compose run --rm --no-deps api python scripts/restore.py /data/backups/noesis-YYYYmmdd-HHMMSS.tar.gz
docker compose up -d
```

The previous database is kept as `data/noesis.db.pre-restore-<time>`.
Secrets are not in backups; keep `.env` in your password manager.

## 11. Update the application

```bash
cd ~/noesis-workforce
docker compose exec -T api python scripts/backup.py          # migrations only move forward
docker tag noesis-workforce:latest noesis-workforce:previous
git rev-parse HEAD > .previous-commit
git pull
docker compose build && docker compose up -d
curl -s https://your-domain/api/health
```

## 12. Roll back a broken deployment

```bash
cd ~/noesis-workforce
git checkout "$(cat .previous-commit)"
docker tag noesis-workforce:previous noesis-workforce:latest
docker compose up -d --no-build
# only if the new version ran a migration the old code cannot read:
docker compose stop worker scheduler telegram api
docker compose run --rm --no-deps api python scripts/restore.py /data/backups/<backup taken in step 11>.tar.gz
docker compose up -d --no-build
```

## Day-to-day

| Need | Command |
|---|---|
| Stop all external actions now | Telegram `/pause` (or Settings → Pause all bots) |
| Turn dry-run off when ready | Settings → Dry-run, or `noesisctl.py dry-run off` |
| Run something now | `docker compose exec api python scripts/noesisctl.py run opportunity_scout sweep_sources` |
| Approve from the shell | `noesisctl.py approvals`, `noesisctl.py approve <id>` |
| Check everything | `docker compose exec api python scripts/noesisctl.py doctor` |
| Add a profile fact | Telegram `/profile set graduation_year 2027` (or Settings → Your profile) |
| Disk usage | logs rotate at 3 × 10 MB per service; backups keep 14; the database prunes itself nightly |
