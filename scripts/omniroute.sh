#!/usr/bin/env bash
# Put OmniRoute behind Jarvis as one more endpoint in the failover pool.
#
#   bash scripts/omniroute.sh                       # interactive
#   bash scripts/omniroute.sh URL MODEL [KEY]       # one line
#   bash scripts/omniroute.sh status                # is it wired and reachable?
#
# What this fixes, and what it doesn't. The "token problem" is per-minute limits
# on free tiers: one busy minute on Groq and every request waits. OmniRoute is a
# local gateway in front of a large catalogue of providers, so when Groq's
# minute is spent the fallback is a catalogue rather than one other key. It
# spreads load across more allowances. It does not create allowance that does
# not exist — a request that needs 8,000 tokens when every tier is spent will
# wait regardless of how it is routed.
#
# Jarvis needs no code change for this: the custom provider slot exists for
# exactly it. This script sets the three values that slot needs — all three,
# because the pool silently ignores a half-filled slot — then restarts the
# container so the pool rebuilds. See docs/omniroute.md for the reasoning.

set -euo pipefail

cd "$(dirname "$0")/.."

GREEN='\033[32m'; RED='\033[31m'; YELLOW='\033[33m'; DIM='\033[2m'; BOLD='\033[1m'; RESET='\033[0m'
say()  { printf "${BOLD}%s${RESET}\n" "$1"; }
note() { printf "${DIM}%s${RESET}\n" "$1"; }
fail() { printf "${RED}%s${RESET}\n" "$1" >&2; exit 1; }

[ -f .env ] || fail "No .env here. Run: bash scripts/setup.sh"

current() { sed -n "s/^$1=//p" .env | tail -1 | sed "s/^'//; s/'$//"; }

if [ "${1:-}" = "status" ]; then
  URL="$(current CUSTOM_BASE_URL)"; MODEL="$(current CUSTOM_MODEL)"; KEY="$(current CUSTOM_API_KEY)"
  if [ -z "$URL" ] && [ -z "$MODEL" ] && [ -z "$KEY" ]; then
    say "OmniRoute is not wired in."
    note "bash scripts/omniroute.sh   to set it up"
    exit 0
  fi
  if [ -z "$URL" ] || [ -z "$MODEL" ] || [ -z "$KEY" ]; then
    printf "${RED}Half configured — the pool is ignoring it.${RESET}\n"
    [ -n "$URL" ]   || echo "  missing CUSTOM_BASE_URL"
    [ -n "$MODEL" ] || echo "  missing CUSTOM_MODEL"
    [ -n "$KEY" ]   || echo "  missing CUSTOM_API_KEY"
    exit 1
  fi
  say "Wired: $URL → $MODEL"
  # Reachability from the host. The container reaches the same address unless
  # it is localhost, which inside the container means the container.
  if curl -sS -m 5 -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $KEY" "${URL%/}/models" 2>/dev/null | grep -q '^2'; then
    printf "${GREEN}Reachable: %s/models answered.${RESET}\n" "${URL%/}"
  else
    printf "${YELLOW}Not answering at %s/models — is OmniRoute running?${RESET}\n" "${URL%/}"
  fi
  note "Measure it before trusting it:  docker compose exec jarvis python -m jarvis.benchmark"
  exit 0
fi

URL="${1:-}"; MODEL="${2:-}"; KEY="${3:-}"

if [ -z "$URL" ]; then
  say "Where is OmniRoute?"
  note "It serves an OpenAI-compatible API on port 20128 by default. From inside the"
  note "container, localhost is the container — use the host's Docker address:"
  read -r -p "  Base URL [http://172.17.0.1:20128/v1]: " URL
  URL="${URL:-http://172.17.0.1:20128/v1}"
fi
if [ -z "$MODEL" ]; then
  say "Which model from its catalogue?"
  note "Something that handles tool calls at full size — this assistant sends ~30"
  note "tool schemas on every turn, and a model that fumbles them is useless here."
  read -r -p "  Model id: " MODEL
  [ -n "$MODEL" ] || fail "A model id is required."
fi
if [ -z "$KEY" ]; then
  say "API key, if OmniRoute wants one"
  note "Anything non-empty works if it does not check. Typed here, never through chat."
  read -r -s -p "  Key [omniroute]: " KEY; echo
  KEY="${KEY:-omniroute}"
fi

case "$URL" in
  http://*|https://*) ;;
  *) fail "The base URL must start with http:// or https://" ;;
esac
case "$URL" in
  *localhost*|*127.0.0.1*)
    printf "${YELLOW}That address is the container itself once inside Docker.${RESET}\n"
    note "If OmniRoute runs on this host, use http://172.17.0.1:20128/v1 instead."
    ;;
esac

# All three, always. A half-filled slot is ignored rather than failing, and
# "ignored" is the failure mode this script exists to prevent.
bash scripts/setkey.sh CUSTOM_BASE_URL "$URL"
bash scripts/setkey.sh CUSTOM_MODEL "$MODEL"
bash scripts/setkey.sh CUSTOM_API_KEY "$KEY"

say "Restarting so the pool picks it up…"
docker compose up -d >/dev/null 2>&1 || fail "docker compose up failed — is Docker running?"

echo
say "OmniRoute is behind Jarvis as the fallback. Groq stays first."
note "That is deliberate: a gateway problem degrades into using Groq, not into nothing."
note "Measure before trusting:   docker compose exec jarvis python -m jarvis.benchmark"
note "Look at the custom: row. Tool calling at full size is the only thing that matters."
note "Once it has handled full-size turns for a few days, make it primary:"
note "  bash scripts/setkey.sh PRIMARY_PROVIDER custom && docker compose up -d"
