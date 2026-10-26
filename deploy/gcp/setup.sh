#!/usr/bin/env bash
# NOESIS on a Google Cloud e2-micro (Always Free) VM: no Docker, systemd only.
#
#   sudo bash deploy/gcp/setup.sh            # from a checkout of the repo, as root (or with sudo)
#
# Safe to run again: every step checks before it changes anything. It
#   1. adds 2 GB of swap (an e2-micro has 1 GB of RAM),
#   2. installs Python, ffmpeg, SQLite, Node 22 and the Claude Code CLI,
#   3. creates the `noesis` user and puts the app in /opt/noesis (data in /var/lib/noesis),
#   4. builds the Python venv and the dashboard,
#   5. creates /opt/noesis/.env from .env.example if it doesn't exist (you fill it in),
#   6. installs the systemd services and timers (backup 03:30 nightly, health check every 30 min),
#   7. installs Tailscale and serves the dashboard on your tailnet only (no public ports).
# Services start only once .env has the required values; see docs/GCP.md.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "Run as root: sudo bash $0"; exit 1; }
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP=/opt/noesis
DATA=/var/lib/noesis
USER_NAME=noesis
SERVICES=(noesis-api noesis-scheduler noesis-worker noesis-telegram noesis-agents)
say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

say "1/7 swap"
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
  chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  sysctl -w vm.swappiness=20 >/dev/null && echo 'vm.swappiness=20' > /etc/sysctl.d/90-noesis.conf
fi
swapon --show

say "2/7 packages, Node 22, Claude Code"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git ffmpeg sqlite3 curl ca-certificates rsync >/dev/null
if ! command -v node >/dev/null || [[ "$(node -v | cut -d. -f1)" != "v22" ]]; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash - >/dev/null
  apt-get install -y -qq nodejs >/dev/null
fi
node -v
command -v claude >/dev/null || npm install -g --silent @anthropic-ai/claude-code
claude --version || true

say "3/7 user and folders"
id "$USER_NAME" >/dev/null 2>&1 || useradd --system --create-home --home-dir /home/$USER_NAME --shell /bin/bash "$USER_NAME"
install -d -o "$USER_NAME" -g "$USER_NAME" -m 750 "$APP" "$DATA" "$DATA/backups" "$DATA/vault"
install -d -o "$USER_NAME" -g "$USER_NAME" -m 700 /home/$USER_NAME/.claude
if [[ "$SRC" != "$APP" ]]; then
  # .env, profile.yaml and the built dashboard on the server are never overwritten
  rsync -a --delete --exclude .git --exclude .venv --exclude node_modules --exclude .next --exclude 'apps/dashboard/out' \
        --exclude .env --exclude config/profile.yaml --exclude data "$SRC/" "$APP/"
fi
chown -R "$USER_NAME:$USER_NAME" "$APP"

say "4/7 Python venv and dashboard"
sudo -u "$USER_NAME" bash -c "cd $APP && { [[ -x .venv/bin/python ]] || python3 -m venv .venv; } && .venv/bin/pip install -q --upgrade pip && .venv/bin/pip install -q -r requirements.txt"
# the build needs ~700 MB; with 2 GB of swap it fits on an e2-micro (slowly: a few minutes)
sudo -u "$USER_NAME" bash -c "cd $APP/apps/dashboard && npm ci --no-audit --no-fund --silent && NODE_OPTIONS=--max-old-space-size=640 npm run build >/dev/null" \
  && echo "dashboard built" || echo "dashboard build failed: the API still runs; build it on your PC and copy apps/dashboard/out (docs/GCP.md)"

say "5/7 .env"
setenv() {  # KEY VALUE: set KEY in .env, replacing an existing (possibly blank) line
  if grep -q "^$1=" "$APP/.env"; then sed -i "s#^$1=.*#$1=$2#" "$APP/.env"; else echo "$1=$2" >> "$APP/.env"; fi
}
if [[ ! -f "$APP/.env" ]]; then
  cp "$APP/.env.example" "$APP/.env"
  setenv NOESIS_DATA_DIR "$DATA"
  setenv NOESIS_DB_PATH "$DATA/noesis.db"
  setenv NOESIS_VAULT_PATH "$DATA/vault"
  setenv NOESIS_PRIVATE_MODE true
  setenv NOESIS_WORKER_MODE shared
  setenv CLAUDE_CODE_ENABLED true
  setenv NOESIS_SESSION_SECRET "$("$APP/.venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(48))')"
  setenv NOESIS_SECRET_KEY "$("$APP/.venv/bin/python" -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
  echo "created $APP/.env (session secret and encryption key generated; fill in the rest)"
fi
chown "$USER_NAME:$USER_NAME" "$APP/.env" && chmod 600 "$APP/.env"
[[ -f "$APP/config/profile.yaml" ]] || sudo -u "$USER_NAME" cp "$APP/config/profile.example.yaml" "$APP/config/profile.yaml"

# `noesisctl <command>` from any shell: runs as the noesis user with .env loaded
cat > /usr/local/bin/noesisctl <<'EOF2'
#!/usr/bin/env bash
exec sudo -u noesis bash -c 'cd /opt/noesis && set -a && . ./.env && set +a && exec .venv/bin/python scripts/noesisctl.py "$@"' noesisctl "$@"
EOF2
chmod 755 /usr/local/bin/noesisctl

say "6/7 systemd services and timers"
install -m 644 "$APP"/deploy/gcp/systemd/*.service "$APP"/deploy/gcp/systemd/*.timer /etc/systemd/system/
chmod +x "$APP/deploy/gcp/backup.sh"
systemctl daemon-reload
systemctl enable --quiet "${SERVICES[@]}" noesis-backup.timer noesis-doctor.timer
need() { grep -Eq "^$1=.+" "$APP/.env"; }
if need NOESIS_ADMIN_PASSWORD_HASH && need TELEGRAM_BOT_TOKEN && need TELEGRAM_OWNER_ID; then
  noesisctl status >/dev/null 2>&1 || true   # bootstrap applies migrations
  systemctl restart "${SERVICES[@]}"
  systemctl start noesis-backup.timer noesis-doctor.timer
  echo "services (re)started"
else
  echo "Not starting yet: set NOESIS_ADMIN_PASSWORD_HASH, TELEGRAM_BOT_TOKEN and TELEGRAM_OWNER_ID in $APP/.env, then run this script again."
fi

say "7/7 Tailscale (dashboard on your tailnet only)"
command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh
if ! tailscale status >/dev/null 2>&1; then
  echo "Run: sudo tailscale up --ssh    (log in with the link it prints), then run this script again."
else
  tailscale serve --bg --https=443 http://127.0.0.1:8000 >/dev/null 2>&1 || tailscale serve --bg 8000 || true
  echo "Dashboard: https://$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
fi

cat <<EOF

Next (docs/GCP.md):
  sudo -iu noesis claude                        # log in to Claude Code once (your Pro/Max plan), then /exit
  noesisctl doctor                               # what's configured, what to fix
  systemctl status 'noesis-*'                    # all five should be active
  journalctl -u noesis-worker -f                 # live logs
EOF
