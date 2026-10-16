# Running on Oracle Cloud Always Free

This is the €0 option: an ARM (Ampere A1) virtual machine from Oracle's Always
Free tier, in **private mode**. Private mode means no domain and no public ports.
The dashboard is reachable only from your own phone and PC over Tailscale, and
Telegram works as usual because it only makes outbound connections.

Oracle's free-tier terms below are from memory (October 2026). Check the
current Always Free page when you sign up.

## What was tested

- The image **builds and runs on ARM64**. It was built for `linux/arm64` under
  emulation. Inside it, 81 of 82 tests passed; the reel-rendering test was
  skipped because that build had no ffmpeg. The API and dashboard also started
  and answered. That build skipped the Debian packages (ffmpeg, tini) because
  the build environment can't reach Debian's mirrors. Both packages exist for
  ARM64 in Debian, so the real build on the server installs them.
- Private mode was started with Docker Compose. The API answered only on
  `127.0.0.1:8000` and no public port was open.
- Oracle itself, Tailscale and a real A1 machine were **not** tested. That
  first run is yours.

## Two things to know before you start

1. **Idle reclamation.** Oracle can reclaim an Always Free machine that looks
   idle for 7 days: CPU, network and memory all under about 20%. This system
   is light, so it would look idle. The fix is to **upgrade the account to Pay
   As You Go**. Always Free resources stay free on that plan, and reclamation
   doesn't apply to it. Set a budget alert (step 2) so a mistake can't cost
   money.
2. **"Out of capacity."** Free ARM machines are often sold out in popular
   regions. You choose your *home region* at signup and can't change it later.
   If creating the machine fails, retry later or pick another availability
   domain. Pay As You Go accounts usually get capacity more easily.

## Steps

### 1. Account
Sign up at cloud.oracle.com. A card is required for identity checks. Choose a
home region near you, for example India South (Hyderabad) or India West
(Mumbai). Then upgrade to Pay As You Go: *Billing → Upgrade and Manage Payment*.

### 2. Budget alert
*Billing → Budgets → Create budget*: $1 a month, alert at 100% to your email.
Anything that would cost money then emails you immediately.

### 3. Create the machine
*Compute → Instances → Create instance*:
- Image: **Canonical Ubuntu 24.04** (the aarch64 build is picked automatically for A1)
- Shape: **VM.Standard.A1.Flex**, 2 OCPU, 12 GB memory. That is half the free
  allowance and plenty for this system.
- Networking: create a new VCN with a public subnet and assign a public IPv4
  address. You need it for SSH until Tailscale is set up.
- SSH keys: upload your public key.
- Boot volume: the default (about 47 GB) is fine.

Connect with `ssh ubuntu@<public-ip>`.

### 4. Firewall
Oracle's Ubuntu images come with their own `iptables` rules that allow only SSH
inbound. **Don't install or enable `ufw`**: Oracle advises against it on these
images. Skip the `ufw` lines in DEPLOYMENT.md step 1, but do the system update
and unattended-upgrades. Private mode needs no other inbound port. Syncthing
and Tailscale both work over outbound connections.

### 5. Tailscale
On the server:
```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --ssh            # opens a login link; use your Tailscale account
```
Install Tailscale on your phone and PC and sign in with the same account. In
the Tailscale admin console, under *DNS*, enable **MagicDNS** and **HTTPS
certificates**.

From now on you can SSH with `ssh ubuntu@<machine-name>` over Tailscale. You
can then remove the SSH (port 22) ingress rule in Oracle's security list, so
nothing on the machine is open to the internet.

### 6. Docker and the code
Follow DEPLOYMENT.md step 2. `get.docker.com` supports ARM64.

### 7. Environment, private mode
Follow DEPLOYMENT.md steps 3 and 4 (password hash, session secret). Leave
`NOESIS_DOMAIN` as it is and uncomment this line in `.env`:
```
COMPOSE_FILE=docker-compose.yml:docker-compose.private.yml
```
Then:
```bash
docker compose up -d --build       # the first build on A1 takes a few minutes
docker compose exec api python scripts/noesisctl.py doctor   # shows "private mode"
```

### 8. The dashboard over Tailscale
```bash
sudo tailscale serve --bg 8000
tailscale serve status             # prints https://<machine>.<tailnet>.ts.net
```
Open that address on any device signed in to your Tailscale account. Nobody
else can reach it.

### 9. Continue with the normal guide
DEPLOYMENT.md step 8 (Telegram), step 9 (vault), step 10 (backups). For the
vault, the Syncthing admin page is reachable over Tailscale or an SSH tunnel;
it's never published.

## Moving to a paid VPS later
Copy `.env`, `config/profile.yaml` and the latest `data/backups/` archive to
the new machine, then follow DEPLOYMENT.md step 10 (restore). Private mode
works the same on any host. Remove the `COMPOSE_FILE` line if you want a public
domain with Caddy instead.
