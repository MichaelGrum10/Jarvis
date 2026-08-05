#!/usr/bin/env bash
# First-run setup: generates secrets, collects credentials, writes .env and
# points the Caddyfile at your domain.
#
#   bash scripts/setup.sh
#
# Safe to re-run: it reads existing values from .env and offers them as defaults,
# so you can change one thing without retyping the rest.

set -euo pipefail

cd "$(dirname "$0")/.."
ENV_FILE=".env"
CADDYFILE="Caddyfile"

bold()  { printf '\033[1m%s\033[0m\n' "$1"; }
green() { printf '\033[32m%s\033[0m\n' "$1"; }
warn()  { printf '\033[33m%s\033[0m\n' "$1"; }
fail()  { printf '\033[31m%s\033[0m\n' "$1" >&2; exit 1; }

command -v openssl >/dev/null || fail "openssl is required. sudo apt install -y openssl"

# Read a key's current value out of .env, if it's there.
existing() {
  [ -f "$ENV_FILE" ] || return 0
  sed -n "s/^$1=//p" "$ENV_FILE" | tail -1
}

# prompt VAR "Question" ["default"] — leaves VAR set in the environment.
prompt() {
  local var="$1" question="$2" default="${3:-}" answer
  if [ -n "$default" ]; then
    read -rp "$question [$default]: " answer
    answer="${answer:-$default}"
  else
    read -rp "$question: " answer
  fi
  printf -v "$var" '%s' "$answer"
}

prompt_secret() {
  local var="$1" question="$2" default="${3:-}" answer
  if [ -n "$default" ]; then
    read -rsp "$question [keep existing]: " answer; echo
    answer="${answer:-$default}"
  else
    read -rsp "$question: " answer; echo
  fi
  printf -v "$var" '%s' "$answer"
}

echo
bold "Jarvis setup"
echo "Nothing here leaves this machine. .env is gitignored and written 600."
echo

# ---------------------------------------------------------------- secrets
AUTH_SECRET="$(existing AUTH_SECRET)"
BRIDGE_TOKEN="$(existing BRIDGE_TOKEN)"
[ -n "$AUTH_SECRET" ]  || AUTH_SECRET="$(openssl rand -base64 32)"
[ -n "$BRIDGE_TOKEN" ] || BRIDGE_TOKEN="$(openssl rand -base64 32)"
green "✓ Signing secrets generated"

# ---------------------------------------------------------------- password
echo
bold "1. Access password"
echo "   You type this once per device. It is the only thing between the"
echo "   internet and your email — make it long."
ACCESS_PASSWORD_OLD="$(existing ACCESS_PASSWORD)"
while :; do
  prompt_secret ACCESS_PASSWORD "   Password" "$ACCESS_PASSWORD_OLD"
  [ -z "$ACCESS_PASSWORD" ] && { warn "   Cannot be empty."; continue; }
  [ ${#ACCESS_PASSWORD} -lt 10 ] && { warn "   Use at least 10 characters."; continue; }
  break
done

# ---------------------------------------------------------------- groq
echo
bold "2. Groq API key"
echo "   Free, no card: https://console.groq.com/keys"
echo "   Covers both chat and voice transcription."
while :; do
  prompt GROQ_API_KEY "   Key (gsk_...)" "$(existing GROQ_API_KEY)"
  [ -z "$GROQ_API_KEY" ] && { warn "   Required."; continue; }
  case "$GROQ_API_KEY" in
    gsk_*) break ;;
    *) warn "   That doesn't look like a Groq key (they start with gsk_). Continuing anyway."; break ;;
  esac
done

# ---------------------------------------------------------------- apple
echo
bold "3. Apple mail + calendar"
echo "   Needs an APP-SPECIFIC password, not your Apple ID password:"
echo "   https://account.apple.com → Sign-In and Security → App-Specific Passwords"
prompt ICLOUD_EMAIL "   Apple ID email" "$(existing ICLOUD_EMAIL)"
prompt_secret ICLOUD_APP_PASSWORD "   App-specific password (xxxx-xxxx-xxxx-xxxx)" "$(existing ICLOUD_APP_PASSWORD)"

if [ -n "$ICLOUD_APP_PASSWORD" ] && ! printf '%s' "$ICLOUD_APP_PASSWORD" | grep -qE '^[a-z]{4}-[a-z]{4}-[a-z]{4}-[a-z]{4}$'; then
  warn "   Note: app-specific passwords look like abcd-efgh-ijkl-mnop."
  warn "   If you pasted your Apple ID password, iCloud will reject the login."
fi

# ---------------------------------------------------------------- misc
echo
bold "4. Basics"
prompt OWNER_NAME "   What should Jarvis call you" "$(existing OWNER_NAME)"
DEFAULT_TZ="$(existing TIMEZONE)"
[ -n "$DEFAULT_TZ" ] || DEFAULT_TZ="$(timedatectl show -p Timezone --value 2>/dev/null || echo America/New_York)"
prompt TIMEZONE "   Your timezone" "$DEFAULT_TZ"

# ---------------------------------------------------------------- domain
echo
bold "5. Domain"
echo "   Required for voice and location — browsers block the microphone and"
echo "   geolocation without HTTPS, and HTTPS needs a domain. Free one takes"
echo "   two minutes: see docs/domain.md"
CURRENT_DOMAIN="$(sed -n 's/^\([a-zA-Z0-9.-]*\) {$/\1/p' "$CADDYFILE" 2>/dev/null | head -1)"
[ "$CURRENT_DOMAIN" = "jarvis.example.com" ] && CURRENT_DOMAIN=""
prompt DOMAIN "   Your domain (blank to skip for now)" "$CURRENT_DOMAIN"

# ---------------------------------------------------------------- write
echo
umask 077
cat > "$ENV_FILE" <<EOF
# Written by scripts/setup.sh. Never commit this file.

OWNER_NAME=$OWNER_NAME
TIMEZONE=$TIMEZONE

AUTH_SECRET=$AUTH_SECRET
ACCESS_PASSWORD=$ACCESS_PASSWORD

GROQ_API_KEY=$GROQ_API_KEY
GROQ_MODEL=llama-3.3-70b-versatile
GROQ_FAST_MODEL=llama-3.1-8b-instant
WHISPER_MODEL=whisper-large-v3-turbo

ICLOUD_EMAIL=$ICLOUD_EMAIL
ICLOUD_APP_PASSWORD=$ICLOUD_APP_PASSWORD

BRIDGE_TOKEN=$BRIDGE_TOKEN

AUTONOMY_ENABLED=false
AUTONOMY_MAX_ITERATIONS=6
LOG_LEVEL=INFO
EOF
chmod 600 "$ENV_FILE"
green "✓ Wrote .env (permissions 600)"

if [ -n "$DOMAIN" ] && [ -f "$CADDYFILE" ]; then
  sed -i.bak "s/^jarvis\.example\.com {$/$DOMAIN {/" "$CADDYFILE"
  [ -n "$CURRENT_DOMAIN" ] && sed -i "s/^$CURRENT_DOMAIN {$/$DOMAIN {/" "$CADDYFILE"
  rm -f "$CADDYFILE.bak"
  green "✓ Caddyfile now serves $DOMAIN"
fi

# SearXNG ships with a placeholder secret; replace it once, silently.
if [ -f searxng/settings.yml ] && grep -q "CHANGE_ME_TO_SOMETHING_RANDOM" searxng/settings.yml; then
  sed -i "s/CHANGE_ME_TO_SOMETHING_RANDOM/$(openssl rand -hex 24)/" searxng/settings.yml
  green "✓ SearXNG secret set"
fi

# ---------------------------------------------------------------- summary
echo
bold "Done. Next:"
echo
echo "  docker compose up -d"
echo "  docker compose logs -f jarvis"
echo
if [ -n "$DOMAIN" ]; then
  echo "  Then open https://$DOMAIN and sign in with your access password."
else
  warn "  No domain set yet. Read docs/domain.md, then re-run this script."
fi
echo
echo "  Bridge token (for a Mac running the iMessage bridge):"
echo "    $BRIDGE_TOKEN"
echo
warn "  If you have ever pasted these credentials anywhere outside this server,"
warn "  revoke and regenerate them now — both are revocable in seconds."
echo
