#!/usr/bin/env bash
# Run OmniRoute next to Jarvis and put it behind the failover pool.
#
#   bash scripts/omniroute.sh                 # install: start it here, wire it in
#   bash scripts/omniroute.sh seed            # hand it the Groq/Gemini keys Jarvis already has
#   bash scripts/omniroute.sh status          # running? reachable? wired?
#   bash scripts/omniroute.sh logs            # its container log
#   bash scripts/omniroute.sh model auto/fast # change which of its models Jarvis asks for
#   bash scripts/omniroute.sh off             # stop it and unwire it (data is kept)
#   bash scripts/omniroute.sh URL MODEL [KEY] # an OmniRoute running somewhere else
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
# exactly it. OmniRoute runs as a service in the same docker-compose file, on
# the "omniroute" profile, so it starts and updates with everything else. The
# Jarvis container reaches it as http://omniroute:20128/v1; the dashboard is on
# this host's loopback only, reached over an SSH tunnel. See docs/omniroute.md.

set -euo pipefail

cd "$(dirname "$0")/.."

GREEN='\033[32m'; RED='\033[31m'; YELLOW='\033[33m'; DIM='\033[2m'; BOLD='\033[1m'; RESET='\033[0m'
say()  { printf "${BOLD}%s${RESET}\n" "$1"; }
note() { printf "${DIM}%s${RESET}\n" "$1"; }
good() { printf "${GREEN}✓ %s${RESET}\n" "$1"; }
warn() { printf "${YELLOW}! %s${RESET}\n" "$1"; }
fail() { printf "${RED}✗ %s${RESET}\n" "$1" >&2; exit 1; }

[ -f .env ] || fail "No .env here. Run: bash scripts/setup.sh"

LOCAL_URL="http://omniroute:20128/v1"       # as the Jarvis container sees it
HOST_URL="http://127.0.0.1:20128"           # as this host sees it
DEFAULT_MODEL="auto"

current() { sed -n "s/^$1=//p" .env | tail -1 | sed "s/^'//; s/'$//"; }
set_quiet() { bash scripts/setkey.sh "$1" "$2" >/dev/null; }

# Interpolation of the compose file needs these even when the profile is off,
# so they are written empty by nothing and generated here exactly once.
ensure_secret() {
  if [ -z "$(current "$1")" ]; then
    set_quiet "$1" "$(openssl rand "$2" "$3")"
  fi
}

# The host-side probe. From the host, "omniroute" does not resolve; the same
# service is published on loopback.
host_probe() {
  curl -sS -m 5 -o /dev/null -w '%{http_code}' "$HOST_URL/v1/models" 2>/dev/null || echo 000
}

wait_for_it() {
  local i code
  for i in $(seq 1 45); do
    code="$(host_probe)"
    # 401 is fine here: it means the server is up and merely wants a key.
    case "$code" in 2*|401) return 0 ;; esac
    sleep 2
  done
  return 1
}

# ------------------------------------------------------------------ status

status() {
  local url model key running code
  url="$(current CUSTOM_BASE_URL)"; model="$(current CUSTOM_MODEL)"; key="$(current CUSTOM_API_KEY)"
  running="$(docker compose ps --status running --format '{{.Name}}' 2>/dev/null | grep -c '^jarvis-omniroute$' || true)"

  if [ "$running" = "1" ]; then
    good "OmniRoute container is running"
  elif [ "$(current COMPOSE_PROFILES)" = "omniroute" ]; then
    warn "OmniRoute is enabled but its container is not running"
    note "docker compose up -d omniroute     then     bash scripts/omniroute.sh logs"
  else
    note "OmniRoute is not installed here.   bash scripts/omniroute.sh   to set it up"
  fi

  if [ -z "$url" ] && [ -z "$model" ] && [ -z "$key" ]; then
    note "Not wired into the pool."
    return 0
  fi
  if [ -z "$url" ] || [ -z "$model" ] || [ -z "$key" ]; then
    printf "${RED}Half configured — the pool is ignoring it.${RESET}\n"
    [ -n "$url" ]   || echo "  missing CUSTOM_BASE_URL"
    [ -n "$model" ] || echo "  missing CUSTOM_MODEL"
    [ -n "$key" ]   || echo "  missing CUSTOM_API_KEY"
    return 1
  fi
  say "Wired: $url → $model"

  local probe="${url%/}"
  [ "$url" = "$LOCAL_URL" ] && probe="$HOST_URL/v1"
  code="$(curl -sS -m 5 -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $key" "$probe/models" 2>/dev/null || echo 000)"
  case "$code" in
    2*) good "Reachable: $probe/models answered" ;;
    401) warn "Reachable, but it rejected the key ($probe/models → 401)" ;;
    000) warn "Not answering at $probe/models — is it running?" ;;
    *) warn "$probe/models answered HTTP $code" ;;
  esac
  note "Measure it before trusting it:  docker compose exec jarvis python -m jarvis.benchmark"
}

# ------------------------------------------------------------------- seed
#
# OmniRoute keeps its own provider list, so the Groq and Gemini keys already in
# .env do it no good until they are handed over. Its management API takes them
# after a login with the dashboard password; the keys travel only between two
# processes on this machine.

seed() {
  local password jar body result added=0 name key label
  password="$(current OMNIROUTE_PASSWORD)"
  [ -n "$password" ] || fail "OMNIROUTE_PASSWORD is not set — run: bash scripts/omniroute.sh"
  wait_for_it || fail "OmniRoute is not answering on $HOST_URL — bash scripts/omniroute.sh logs"

  jar="$(mktemp)"
  trap 'rm -f "$jar"' RETURN
  body="$(PW="$password" python3 -c 'import json,os; print(json.dumps({"password": os.environ["PW"]}))')"
  result="$(curl -sS -m 15 -c "$jar" -X POST "$HOST_URL/api/auth/login" \
    -H 'Content-Type: application/json' -d "$body" 2>&1)" || fail "Could not log in to OmniRoute: $result"
  if ! grep -q . "$jar"; then
    printf "${RED}Login did not produce a session.${RESET} OmniRoute said:\n"
    printf "${DIM}%s${RESET}\n" "$(printf '%s' "$result" | head -c 300)"
    note "If the password was changed in its dashboard, update it here too:"
    note "  bash scripts/setkey.sh OMNIROUTE_PASSWORD …"
    exit 1
  fi

  # Left is OmniRoute's provider id, right is the .env name Jarvis reads. Both
  # sides see the key: Jarvis's own pool directly, OmniRoute through here.
  for pair in "groq:GROQ_API_KEY" "gemini:GEMINI_API_KEY" "mistral:MISTRAL_API_KEY" \
              "nvidia:NVIDIA_API_KEY" "openrouter:OPENROUTER_API_KEY" "cerebras:CEREBRAS_API_KEY" \
              "together:TOGETHER_API_KEY" "huggingface:HUGGINGFACE_API_KEY" "anthropic:ANTHROPIC_API_KEY"; do
    name="${pair%%:*}"; label="${pair##*:}"
    key="$(current "$label")"
    [ -n "$key" ] || continue
    body="$(P="$name" K="$key" python3 -c 'import json,os; print(json.dumps({"provider": os.environ["P"], "apiKey": os.environ["K"], "name": "Jarvis " + os.environ["P"]}))')"
    result="$(curl -sS -m 20 -b "$jar" -o /dev/stderr -w '%{http_code}' -X POST "$HOST_URL/api/providers" \
      -H 'Content-Type: application/json' -d "$body" 2>/tmp/omniroute-seed-body || echo 000)"
    case "$result" in
      2*) good "Added $name to OmniRoute"; added=$((added + 1)) ;;
      409) note "$name already there" ;;
      *) warn "$name: OmniRoute answered HTTP $result — $(head -c 200 /tmp/omniroute-seed-body | tr -d '\n')"
         note "Add it by hand instead: dashboard → Providers → Add Provider → $name" ;;
    esac
  done
  rm -f /tmp/omniroute-seed-body
  [ "$added" -gt 0 ] && note "OmniRoute now routes across those as well as its own free providers."
  return 0
}

# ---------------------------------------------------------------- install

install() {
  command -v docker >/dev/null || fail "Docker is not installed."
  command -v openssl >/dev/null || fail "openssl is needed to generate its secrets."

  say "Secrets"
  ensure_secret OMNIROUTE_JWT_SECRET -base64 48
  ensure_secret OMNIROUTE_API_KEY_SECRET -hex 32
  ensure_secret OMNIROUTE_WS_BRIDGE_SECRET -base64 32
  if [ -z "$(current OMNIROUTE_PASSWORD)" ]; then
    local pw
    note "The dashboard password. Enter to have one generated and shown once."
    read -r -s -p "  Dashboard password: " pw; echo
    if [ -z "$pw" ]; then
      pw="$(openssl rand -base64 18 | tr -d '/+=' | head -c 20)"
      printf "  Generated: ${BOLD}%s${RESET}   (kept in .env as OMNIROUTE_PASSWORD)\n" "$pw"
    fi
    set_quiet OMNIROUTE_PASSWORD "$pw"
  fi
  good "Secrets in place"

  say "Starting OmniRoute"
  set_quiet COMPOSE_PROFILES omniroute
  docker compose pull -q omniroute 2>&1 | grep -v "^$" || true
  docker compose up -d omniroute >/dev/null 2>&1 || fail "docker compose up failed — see: docker compose logs omniroute"
  if wait_for_it; then
    good "Answering on $HOST_URL"
  else
    printf "${RED}Started, but not answering after 90s.${RESET}\n"
    note "docker compose logs --tail 60 omniroute"
    exit 1
  fi

  say "Wiring it into the pool"
  # All three, always. A half-filled slot is ignored rather than failing, and
  # "ignored" is the failure mode this script exists to prevent. The key is a
  # placeholder: REQUIRE_API_KEY is off and the socket is not on the internet.
  set_quiet CUSTOM_BASE_URL "$LOCAL_URL"
  [ -n "$(current CUSTOM_MODEL)" ] || set_quiet CUSTOM_MODEL "$DEFAULT_MODEL"
  [ -n "$(current CUSTOM_API_KEY)" ] || set_quiet CUSTOM_API_KEY "omniroute"
  docker compose up -d >/dev/null 2>&1 || fail "docker compose up failed"
  good "Jarvis will fall back to OmniRoute ($(current CUSTOM_MODEL)) when its own providers are spent"

  say "Handing it the provider keys Jarvis already has"
  seed || true

  echo
  say "Next: open its dashboard, once, to connect free providers"
  note "It is on this machine's loopback only. From your Mac:"
  printf "  ssh -N -L 20128:127.0.0.1:20128 %s@%s\n" "$(whoami)" "$(hostname -I 2>/dev/null | awk '{print $1}')"
  note "then http://localhost:20128 in a browser, password as above."
  note "Its first-run wizard offers 'Set up free providers' — accept the ones you want."
  echo
  note "Then measure it before trusting it:"
  note "  docker compose exec jarvis python -m jarvis.benchmark      look at the custom: rows"
  note "Groq stays first on purpose: an OmniRoute problem degrades into Groq, not into nothing."
  note "Once it has handled full-size turns for a few days:"
  note "  bash scripts/setkey.sh PRIMARY_PROVIDER custom && docker compose up -d"
}

# -------------------------------------------------------------------- off

off() {
  docker compose stop omniroute >/dev/null 2>&1 || true
  docker compose rm -f omniroute >/dev/null 2>&1 || true
  # Blank rather than delete: setkey replaces in place, and a blank slot is
  # exactly what the pool treats as absent.
  set_quiet COMPOSE_PROFILES ""
  set_quiet CUSTOM_BASE_URL ""
  set_quiet CUSTOM_MODEL ""
  set_quiet CUSTOM_API_KEY ""
  docker compose up -d >/dev/null 2>&1 || true
  good "OmniRoute stopped and unwired. Its data volume is kept; 'docker volume rm jarvis_omniroute-data' removes it."
}

# ------------------------------------------------------------------ dispatch

case "${1:-}" in
  status) status ;;
  seed) seed ;;
  logs) docker compose logs --tail 80 omniroute ;;
  off) off ;;
  model)
    [ -n "${2:-}" ] || fail "Usage: bash scripts/omniroute.sh model auto/fast"
    set_quiet CUSTOM_MODEL "$2"
    docker compose up -d >/dev/null 2>&1 || true
    good "Jarvis now asks OmniRoute for $2"
    ;;
  "")
    install ;;
  http://*|https://*)
    # An OmniRoute running somewhere else: wire it, don't run it.
    URL="$1"; MODEL="${2:-}"; KEY="${3:-omniroute}"
    [ -n "$MODEL" ] || fail "Usage: bash scripts/omniroute.sh URL MODEL [KEY]"
    case "$URL" in
      *localhost*|*127.0.0.1*)
        warn "That address is the container itself once inside Docker."
        note "For an OmniRoute on this host outside compose, use http://172.17.0.1:20128/v1." ;;
    esac
    set_quiet CUSTOM_BASE_URL "$URL"; set_quiet CUSTOM_MODEL "$MODEL"; set_quiet CUSTOM_API_KEY "$KEY"
    docker compose up -d >/dev/null 2>&1 || fail "docker compose up failed"
    good "Wired: $URL → $MODEL"
    status || true
    ;;
  *)
    fail "Unknown command: $1   (install | seed | status | logs | model NAME | off | URL MODEL [KEY])" ;;
esac
