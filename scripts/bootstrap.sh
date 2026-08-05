#!/usr/bin/env bash
# One-command install for a fresh Ubuntu server.
#
#   curl -fsSL https://raw.githubusercontent.com/MichaelGrum10/Jarvis/claude/jarvis-ai-assistant-9tlgu3/scripts/bootstrap.sh -o bootstrap.sh
#   less bootstrap.sh      # read it first — you're about to run it as yourself
#   bash bootstrap.sh
#
# Installs Docker and git, opens the firewall, clones the repo, then hands over
# to scripts/setup.sh for credentials and starts everything.
#
# Written for phone use: one paste, then answer prompts. Safe to re-run — every
# step checks before acting.

set -euo pipefail

REPO="https://github.com/MichaelGrum10/Jarvis.git"
BRANCH="claude/jarvis-ai-assistant-9tlgu3"
TARGET="$HOME/Jarvis"

bold()  { printf '\n\033[1m%s\033[0m\n' "$1"; }
green() { printf '\033[32m%s\033[0m\n' "$1"; }
warn()  { printf '\033[33m%s\033[0m\n' "$1"; }
fail()  { printf '\033[31m%s\033[0m\n' "$1" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] && fail "Run as your normal user, not root. Docker needs a non-root user in its group."
command -v apt-get >/dev/null || fail "This script expects Ubuntu or Debian."

bold "Jarvis bootstrap"
echo "Installing prerequisites, opening ports, and cloning the repo."
echo "You'll be asked for your password by sudo, then for credentials at the end."

# ---------------------------------------------------------------- packages
bold "1/5  Installing Docker and git"
if command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then
  green "✓ Already installed"
else
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    docker.io docker-compose-v2 git curl openssl >/dev/null
  green "✓ Installed"
fi

if ! id -nG "$USER" | tr ' ' '\n' | grep -qx docker; then
  sudo usermod -aG docker "$USER"
  NEEDS_RELOGIN=1
  warn "! Added you to the docker group — this needs a new login to take effect."
else
  NEEDS_RELOGIN=0
fi

# ---------------------------------------------------------------- firewall
bold "2/5  Opening ports 80 and 443"
# Oracle's Ubuntu images ship a restrictive INPUT chain. Without this, Let's
# Encrypt cannot reach the server and no certificate is ever issued.
for port in 80 443; do
  if sudo iptables -C INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null; then
    green "✓ Port $port already open"
  else
    sudo iptables -I INPUT -p tcp --dport "$port" -j ACCEPT
    green "✓ Opened port $port"
  fi
done
if command -v netfilter-persistent >/dev/null; then
  sudo netfilter-persistent save >/dev/null 2>&1 && green "✓ Firewall rules saved"
else
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq iptables-persistent >/dev/null 2>&1 || true
  sudo netfilter-persistent save >/dev/null 2>&1 || warn "! Could not persist rules; they may reset on reboot."
fi

warn "  Oracle Cloud also blocks these in the web console."
warn "  Instance → subnet → Security List → Add Ingress Rules:"
warn "  source 0.0.0.0/0, TCP ports 80 and 443. Both layers are required."

# ---------------------------------------------------------------- clone
bold "3/5  Fetching Jarvis"
if [ -d "$TARGET/.git" ]; then
  git -C "$TARGET" fetch --quiet origin "$BRANCH"
  git -C "$TARGET" checkout --quiet "$BRANCH"
  git -C "$TARGET" pull --quiet origin "$BRANCH"
  green "✓ Updated existing checkout at $TARGET"
else
  git clone --quiet --branch "$BRANCH" "$REPO" "$TARGET"
  green "✓ Cloned to $TARGET"
fi
cd "$TARGET"

# ---------------------------------------------------------------- ip / domain
bold "4/5  Your server address"
PUBLIC_IP="$(curl -4 -fsS --max-time 10 ifconfig.me 2>/dev/null || echo '')"
if [ -n "$PUBLIC_IP" ]; then
  echo "  Public IP: $PUBLIC_IP"
  echo
  echo "  If you don't have a domain yet, do this now in a browser:"
  echo "    1. duckdns.org  →  sign in"
  echo "    2. add a domain, e.g. michael-jarvis"
  echo "    3. paste $PUBLIC_IP into its 'current ip' box  →  update ip"
  echo
  echo "  You need one: HTTPS certificates can't be issued for a bare IP, and"
  echo "  browsers block the microphone and location without HTTPS."
  echo
  read -rp "  Press Enter once that's done (or now, to set the domain later)… " _
else
  warn "! Could not detect the public IP. Find it on the Oracle instance page."
fi

# ---------------------------------------------------------------- credentials
bold "5/5  Credentials"
bash scripts/setup.sh

# ---------------------------------------------------------------- launch
bold "Starting Jarvis"
if [ "$NEEDS_RELOGIN" = "1" ]; then
  warn "Your shell doesn't have docker group membership yet."
  echo "Run these two lines to finish:"
  echo
  echo "    cd $TARGET && newgrp docker"
  echo "    docker compose up -d && docker compose exec jarvis python -m jarvis.doctor"
  echo
  exit 0
fi

docker compose up -d
echo
green "✓ Containers started. First build takes a few minutes."
echo
echo "Check everything connected:"
echo
echo "    cd $TARGET && docker compose exec jarvis python -m jarvis.doctor"
echo
echo "Then open your domain in Safari and Add to Home Screen."
