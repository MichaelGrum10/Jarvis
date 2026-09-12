#!/usr/bin/env bash
# Use the best free models — as measured, not as remembered.
#
#   bash scripts/bestmodels.sh             # measure every provider, apply the order
#   bash scripts/bestmodels.sh show        # measure, print the order, change nothing
#   bash scripts/bestmodels.sh weekly      # do this every Monday morning, unattended
#   bash scripts/bestmodels.sh weekly off
#
# "Best" here is not an opinion. The benchmark sends every configured provider
# the requests this assistant actually sends — a normal turn, a tool call, and a
# full-size turn carrying ~30 tool schemas — and ranks them: the ones that pass
# the full-size tool-calling turn first, fastest first. That order is written to
# PROVIDER_ORDER and the container is restarted, so the pool walks the best
# provider first and the rest are backups in measured order.
#
# Free tiers change under you: models are renamed, quotas cut, tiers withdrawn.
# `weekly` re-measures on a schedule, so the order stays true without anyone
# remembering to check. It writes one crontab line and nothing else.

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

measure() {
  # -T: no TTY, so this works from cron as well as from a terminal.
  docker compose exec -T jarvis python -m jarvis.benchmark 2>&1
}

apply() {
  local mode="$1" output order was
  say "Measuring every configured provider (a minute or two)…"
  output="$(measure)" || true
  printf '%s\n' "$output" | sed '/^RECOMMENDED_ORDER=/d'
  order="$(printf '%s\n' "$output" | sed -n 's/^RECOMMENDED_ORDER=//p' | tail -1)"

  if [ -z "$order" ]; then
    warn "No provider passed the tool test, so the order was left as it is."
    note "Read the table above: every row explains its own failure."
    return 1
  fi

  was="$(current PROVIDER_ORDER)"
  echo
  if [ "$mode" = "show" ]; then
    say "Measured order: $order"
    [ "$was" = "$order" ] && note "That is already PROVIDER_ORDER." \
      || note "PROVIDER_ORDER is currently '${was:-unset}'. Apply with:  bash scripts/bestmodels.sh"
    return 0
  fi
  if [ "$was" = "$order" ]; then
    good "Already using the measured order: $order"
    return 0
  fi
  bash scripts/setkey.sh PROVIDER_ORDER "$order" >/dev/null
  docker compose up -d >/dev/null 2>&1 || fail "docker compose up failed"
  good "Now trying providers in measured order: $order"
  [ -n "$was" ] && note "was: $was"
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
  good "Every Monday at 06:00 the providers are re-measured and the order updated."
  note "Log: $LOG        Stop it: bash scripts/bestmodels.sh weekly off"
}

case "${1:-}" in
  "") apply apply ;;
  show) apply show ;;
  weekly) weekly "${2:-}" ;;
  *) fail "Usage: bash scripts/bestmodels.sh [show | weekly | weekly off]" ;;
esac
