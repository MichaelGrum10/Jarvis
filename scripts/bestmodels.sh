#!/usr/bin/env bash
# Use the best free models — as measured, not as remembered.
#
#   bash scripts/bestmodels.sh             # measure everything, apply what passes
#   bash scripts/bestmodels.sh show        # measure, print what it would change, change nothing
#   bash scripts/bestmodels.sh weekly      # do this every Monday morning, unattended
#   bash scripts/bestmodels.sh weekly off
#
# "Best" here is not an opinion. The benchmark sends every configured provider
# the requests this assistant actually sends — a normal turn, a tool call, and a
# full-size turn carrying ~30 tool schemas — and then tries a few more models
# from each provider's own catalogue the same way. Two things come out of it:
#
#   PROVIDER_ORDER   which provider to ask first, and the order of the rest:
#                    the ones that pass the full-size tool call, fastest first
#   <PROVIDER>_MODEL each provider's own ladder: the models that passed,
#                    fastest first (GROQ_MODEL_LADDER, GEMINI_MODEL, CUSTOM_MODEL…)
#
# Both are written to .env and the container restarted. Free tiers change under
# you — models renamed, quotas cut, tiers withdrawn — so `weekly` re-measures
# on a schedule and the settings stay true without anyone remembering to check.
# It writes one crontab line and nothing else.
#
# BESTMODELS_EXPLORE=N sets how many extra catalogue models to try per provider
# (default 3). More is slower and spends more of the free allowances.

set -euo pipefail

cd "$(dirname "$0")/.."

GREEN='\033[32m'; RED='\033[31m'; YELLOW='\033[33m'; DIM='\033[2m'; BOLD='\033[1m'; RESET='\033[0m'
say()  { printf "${BOLD}%s${RESET}\n" "$1"; }
note() { printf "${DIM}%s${RESET}\n" "$1"; }
good() { printf "${GREEN}✓ %s${RESET}\n" "$1"; }
warn() { printf "${YELLOW}! %s${RESET}\n" "$1"; }
fail() { printf "${RED}✗ %s${RESET}\n" "$1" >&2; exit 1; }

[ -f .env ] || fail "No .env here. Run: bash scripts/setup.sh"

current() { sed -n "s/^$1=//p" .env | tail -1 | sed "s/^'//; s/'$//"; }

MARK="# jarvis-bestmodels"
LOG="data/bestmodels.log"
EXPLORE="${BESTMODELS_EXPLORE:-3}"

# The benchmark runs inside the container, and the container runs whatever
# was built last — a `git pull` alone changes nothing in it. An old benchmark
# prints none of the lines this script reads, which used to come out as "no
# provider passed" while two of them had. Ask first.
preflight() {
  docker compose exec -T jarvis python -c \
    "import jarvis.benchmark as b, sys; sys.exit(0 if hasattr(b, 'recommend_models') else 1)" \
    >/dev/null 2>&1 && return 0
  printf "${RED}The container is running an older Jarvis than this checkout.${RESET}\n"
  note "Rebuild it, then run this again:   bash scripts/update.sh"
  exit 1
}

measure() {
  # -T: no TTY, so this works from cron as well as from a terminal.
  docker compose exec -T jarvis python -m jarvis.benchmark --explore "$EXPLORE" 2>&1
}

apply() {
  local mode="$1" output changed=0 line name value was
  preflight
  say "Measuring every configured provider, plus up to $EXPLORE more models each (a few minutes)…"
  output="$(measure)" || true
  printf '%s\n' "$output" | sed '/^RECOMMENDED_/d'

  if ! printf '%s\n' "$output" | grep -q '^RECOMMENDED_'; then
    echo
    warn "Nothing passed the full-size tool test, so no setting was changed."
    note "Read the table above: every row explains its own failure."
    return 1
  fi

  echo
  while IFS= read -r line; do
    name="${line#RECOMMENDED_}"; value="${name#*=}"; name="${name%%=*}"
    [ "$name" = "ORDER" ] && name="PROVIDER_ORDER"
    was="$(current "$name")"
    if [ "$was" = "$value" ]; then
      note "$name unchanged: $value"
      continue
    fi
    if [ "$mode" = "show" ]; then
      printf "  %-20s %s   ${DIM}(now: %s)${RESET}\n" "$name" "$value" "${was:-unset}"
      changed=$((changed + 1))
      continue
    fi
    bash scripts/setkey.sh "$name" "$value" >/dev/null
    good "$name = $value"
    [ -n "$was" ] && note "    was: $was"
    changed=$((changed + 1))
  done < <(printf '%s\n' "$output" | sed -n 's/^RECOMMENDED_//p' | sed 's/^/RECOMMENDED_/')

  if [ "$mode" = "show" ]; then
    [ "$changed" -gt 0 ] && note "Apply with:  bash scripts/bestmodels.sh" || good "Already using the measured settings."
    return 0
  fi
  if [ "$changed" -gt 0 ]; then
    docker compose up -d >/dev/null 2>&1 || fail "docker compose up failed"
    good "Applied. Jarvis now asks the measured best first."
  else
    good "Already using the measured settings."
  fi
  note "Check the pool any time:  docker compose exec jarvis python -m jarvis.doctor"
}

weekly() {
  local line="0 6 * * 1 cd $(pwd) && bash scripts/bestmodels.sh >> $(pwd)/$LOG 2>&1 $MARK"
  if [ "${1:-}" = "off" ]; then
    (crontab -l 2>/dev/null | grep -v "$MARK" || true) | crontab -
    good "Weekly re-measure removed."
    return 0
  fi
  mkdir -p "$(dirname "$LOG")"
  # Idempotent: replace any previous line of ours rather than add a second.
  { crontab -l 2>/dev/null | grep -v "$MARK" || true; echo "$line"; } | crontab -
  good "Every Monday at 06:00 the providers are re-measured and the settings updated."
  note "Log: $LOG        Stop it: bash scripts/bestmodels.sh weekly off"
}

case "${1:-}" in
  "") apply apply ;;
  show) apply show ;;
  weekly) weekly "${2:-}" ;;
  *) fail "Usage: bash scripts/bestmodels.sh [show | weekly | weekly off]" ;;
esac
