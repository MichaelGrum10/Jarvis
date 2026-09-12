#!/usr/bin/env bash
# Ship what Jarvis wrote, and take it back if it broke.
#
#   bash scripts/autodeploy.sh            # deploy if the branch moved
#   bash scripts/autodeploy.sh watch      # do that every 10 minutes, unattended
#   bash scripts/autodeploy.sh watch off
#   bash scripts/autodeploy.sh status     # what's deployed vs what's committed
#
# ## Why this exists
#
# The self-improvement engine runs *inside* the container and merges its work
# into the checkout on this host. That changes nothing about what is running:
# the server code is baked into the image at build time, so until something
# rebuilds, Jarvis has fixed a bug in a file it is not executing. This is the
# missing half of IMPROVE_MODE=apply.
#
# It cannot live inside the container for the obvious reason — a process cannot
# rebuild and restart the container it is running in and still be around to
# check whether that worked.
#
# ## The guard rail
#
# Every deploy records the commit it came from, waits for the server to answer
# its health check, and on failure resets to that commit and rebuilds again.
# The work is never lost: the engine's own branch (jarvis/auto/...) still holds
# it, so a rolled-back change is a branch to read, not a thing that vanished.

set -euo pipefail

cd "$(dirname "$0")/.."

GREEN='\033[32m'; RED='\033[31m'; YELLOW='\033[33m'; DIM='\033[2m'; BOLD='\033[1m'; RESET='\033[0m'
say()  { printf "${BOLD}%s${RESET}\n" "$1"; }
note() { printf "${DIM}%s${RESET}\n" "$1"; }
good() { printf "${GREEN}✓ %s${RESET}\n" "$1"; }
warn() { printf "${YELLOW}! %s${RESET}\n" "$1"; }
fail() { printf "${RED}✗ %s${RESET}\n" "$1" >&2; exit 1; }

MARK="# jarvis-autodeploy"
STATE="data/deployed.commit"
LOG="data/autodeploy.log"
HEALTH="http://127.0.0.1:8000/api/health"
# Generous: a rebuild plus a cold start on a small ARM box is not quick.
HEALTH_TRIES=45

stamp() { date "+%Y-%m-%d %H:%M:%S"; }

deployed() { [ -f "$STATE" ] && cat "$STATE" || echo ""; }
head_commit() { git rev-parse HEAD; }

healthy() {
  local i
  for i in $(seq 1 "$HEALTH_TRIES"); do
    if curl -fsS --max-time 4 "$HEALTH" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

build() {
  # --build because the point is new code; without it compose happily restarts
  # the old image and reports success.
  docker compose up -d --build >/dev/null 2>&1
}

status() {
  local have want
  have="$(deployed)"; want="$(head_commit)"
  say "Checked out : ${want:0:7}  $(git log -1 --format=%s | head -c 60)"
  if [ -z "$have" ]; then
    note "Deployed    : unrecorded (nothing has auto-deployed yet)"
  elif [ "$have" = "$want" ]; then
    good "Deployed    : ${have:0:7} — up to date"
  else
    warn "Deployed    : ${have:0:7} — behind by $(git rev-list --count "$have..$want" 2>/dev/null || echo '?') commit(s)"
    note "bash scripts/autodeploy.sh   to ship it"
  fi
  if crontab -l 2>/dev/null | grep -q "$MARK"; then
    good "Watching    : every 10 minutes"
  else
    note "Watching    : off   (bash scripts/autodeploy.sh watch)"
  fi
  [ -f "$LOG" ] && { echo; note "Recent:"; tail -5 "$LOG"; }
}

deploy() {
  local before want
  want="$(head_commit)"
  before="$(deployed)"

  if [ "$before" = "$want" ]; then
    note "$(stamp) nothing new to deploy (${want:0:7})"
    return 0
  fi

  # First ever run: record and rebuild once, so there is a known-good commit to
  # fall back to next time rather than an empty string.
  if [ -z "$before" ]; then
    say "$(stamp) first deploy — recording ${want:0:7} as the baseline"
  else
    say "$(stamp) deploying ${before:0:7} → ${want:0:7}"
    git --no-pager log --oneline "$before..$want" 2>/dev/null | sed 's/^/  /' || true
  fi

  mkdir -p "$(dirname "$STATE")"
  if ! build; then
    warn "The build itself failed; nothing was restarted."
    note "docker compose logs --tail 50 jarvis"
    printf '%s  BUILD FAILED at %s\n' "$(stamp)" "${want:0:7}" >> "$LOG"
    return 1
  fi

  if healthy; then
    printf '%s' "$want" > "$STATE"
    good "$(stamp) deployed ${want:0:7} and it answered"
    printf '%s  ok %s\n' "$(stamp)" "${want:0:7}" >> "$LOG"
    return 0
  fi

  # It built and will not answer. Go back.
  printf "${RED}%s did not come back after deploying %s${RESET}\n" "$HEALTH" "${want:0:7}"
  if [ -z "$before" ]; then
    warn "No previous known-good commit recorded, so there is nothing to roll back to."
    note "docker compose logs --tail 50 jarvis"
    printf '%s  UNHEALTHY at %s, no baseline to revert to\n' "$(stamp)" "${want:0:7}" >> "$LOG"
    return 1
  fi

  say "Rolling back to ${before:0:7}…"
  # The work is not lost: the engine's jarvis/auto/* branch still has it, which
  # is why a hard reset is acceptable here rather than reckless.
  git reset --hard "$before" >/dev/null 2>&1 || warn "git reset failed — see below"
  if build && healthy; then
    good "Rolled back to ${before:0:7}; Jarvis is answering again"
    printf '%s  ROLLED BACK %s -> %s\n' "$(stamp)" "${want:0:7}" "${before:0:7}" >> "$LOG"
    note "The change that broke it is still on its own branch:  git branch --list 'jarvis/auto/*'"
  else
    printf "${RED}Rollback did not restore it either. This needs you.${RESET}\n"
    note "docker compose logs --tail 80 jarvis"
    printf '%s  ROLLBACK FAILED %s -> %s\n' "$(stamp)" "${want:0:7}" "${before:0:7}" >> "$LOG"
  fi
  return 1
}

watch() {
  local line="*/10 * * * * cd $(pwd) && bash scripts/autodeploy.sh >> $(pwd)/$LOG 2>&1 $MARK"
  if [ "${1:-}" = "off" ]; then
    (crontab -l 2>/dev/null | grep -v "$MARK" || true) | crontab -
    good "Auto-deploy watching stopped. Merges will sit until you deploy them."
    return 0
  fi
  mkdir -p "$(dirname "$LOG")"
  { crontab -l 2>/dev/null | grep -v "$MARK" || true; echo "$line"; } | crontab -
  good "Every 10 minutes: if Jarvis has merged something, it gets built, health-checked, and kept or rolled back."
  note "Log: $LOG        Stop: bash scripts/autodeploy.sh watch off"
}

case "${1:-}" in
  "") deploy ;;
  status) status ;;
  watch) watch "${2:-}" ;;
  *) fail "Usage: bash scripts/autodeploy.sh [status | watch | watch off]" ;;
esac
