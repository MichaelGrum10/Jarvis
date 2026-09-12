#!/usr/bin/env bash
# Turn continuous self-improvement on (or off) in one step.
#
#   bash scripts/self-improve.sh propose   # fix on a branch, you merge
#   bash scripts/self-improve.sh apply     # merge automatically when tests pass
#   bash scripts/self-improve.sh off
#
# Three separate things have to line up for this to work, which is why it gets a
# script rather than a line in the README:
#
#   IMPROVE_MODE       what to do with a fix once it has one
#   AUTONOMY_ENABLED   permission to edit its own code at all
#   the ./:/app/repo   a real git checkout to branch in — without it there is no
#   volume mount       way to isolate or undo a change, so the engine refuses
#
# 'apply' needs a fourth: scripts/autodeploy.sh, on a timer. The engine merges
# into this checkout, but the running server is built from an image — without a
# rebuild it has fixed a bug in a file it is not executing. This script turns
# that on too, so 'apply' means what it says.
#
# Setting only the first leaves the loop running and failing every cycle, in a
# log nobody reads. Nothing about that is visible from the app.

set -euo pipefail

cd "$(dirname "$0")/.."

GREEN='\033[32m'; RED='\033[31m'; YELLOW='\033[33m'; DIM='\033[2m'; BOLD='\033[1m'; RESET='\033[0m'
fail() { printf "${RED}%s${RESET}\n" "$1" >&2; exit 1; }
step() { printf "${BOLD}%s${RESET}\n" "$1"; }
note() { printf "${DIM}  %s${RESET}\n" "$1"; }

MODE="${1:-}"
case "$MODE" in
  propose|apply|off) ;;
  *) fail "Usage: bash scripts/self-improve.sh propose|apply|off" ;;
esac

[ -f .env ] || fail "No .env here. Run: bash scripts/setup.sh"
[ -f docker-compose.yml ] || fail "Run this from the Jarvis directory."

if [ "$MODE" = "off" ]; then
  bash scripts/setkey.sh IMPROVE_MODE off >/dev/null
  bash scripts/setkey.sh AUTONOMY_ENABLED false >/dev/null
  if [ -f docker-compose.override.yml ] && grep -q '/app/repo' docker-compose.override.yml; then
    rm -f docker-compose.override.yml
    note "Removed the repo mount"
  fi
  bash scripts/autodeploy.sh watch off >/dev/null 2>&1 || true
  note "Stopped auto-deploy"
  printf "${GREEN}✓ Self-improvement off${RESET}\n"
  note "Apply it with: docker compose up -d"
  exit 0
fi

if [ "$MODE" = "apply" ]; then
  printf "${YELLOW}'apply' merges its own changes, rebuilds and restarts, unattended.${RESET}\n"
  printf "${YELLOW}The tests catch bad code; they don't catch a bad idea that passes.${RESET}\n"
  printf "${YELLOW}A deploy that stops answering is rolled back automatically — that is the net.${RESET}\n"
  printf "Type 'yes' to continue: "
  read -r answer
  [ "$answer" = "yes" ] || fail "Left unchanged. 'propose' is the safer starting point."
fi

step "1/3  Setting the mode"
bash scripts/setkey.sh IMPROVE_MODE "$MODE" >/dev/null
note "IMPROVE_MODE=$MODE"

step "2/3  Allowing it to edit its own code"
bash scripts/setkey.sh AUTONOMY_ENABLED true >/dev/null
note "AUTONOMY_ENABLED=true"

step "3/3  Giving it this checkout to work in"

# Written to docker-compose.override.yml, which Compose merges automatically and
# git ignores. Editing docker-compose.yml directly is what the first version did,
# and it was wrong: that file is tracked, so the edit collided with every
# subsequent update and made `git pull` refuse — reported, unhelpfully, as a
# network failure.
if [ -f docker-compose.yml ] && grep -qE '^\s*-\s*\./:/app/repo:rw' docker-compose.yml; then
  note "Reverting the old in-place edit to docker-compose.yml"
  git checkout -- docker-compose.yml 2>/dev/null \
    || note "Couldn't revert it automatically — run: git checkout -- docker-compose.yml"
fi

if [ -f docker-compose.override.yml ] && grep -q '/app/repo' docker-compose.override.yml; then
  note "Already mounted."
else
  [ -f docker-compose.override.yml ] && cp docker-compose.override.yml docker-compose.override.yml.bak
  cat > docker-compose.override.yml <<'YAML'
# Written by scripts/self-improve.sh. Compose merges this over
# docker-compose.yml automatically; git ignores it, so it survives updates.
# Remove this file (or run `bash scripts/self-improve.sh off`) to take the
# checkout away from the self-improvement engine.
services:
  jarvis:
    volumes:
      - ./:/app/repo:rw
YAML
  note "Mounted ./ at /app/repo via docker-compose.override.yml"
fi

if [ "$MODE" = "apply" ]; then
  step "4/4  Shipping what it merges"
  # Without this, 'apply' merges into the checkout and the container keeps
  # running the image it was built from. Health-checked, with rollback.
  bash scripts/autodeploy.sh watch
  bash scripts/autodeploy.sh >/dev/null 2>&1 || true
fi

step "Restarting"
docker compose up -d

printf "\n${GREEN}✓ Self-improvement is on in '%s' mode${RESET}\n" "$MODE"
note "Confirm with:  docker compose exec jarvis python -m jarvis.doctor"
note "See what it would work on:  in the app, ☰ → Self-improve"
if [ "$MODE" = "propose" ]; then
  note "Fixes land on branches named jarvis/auto-*. Nothing merges without you."
else
  note "Merged changes are built and health-checked within 10 minutes, and rolled"
  note "back automatically if the server stops answering."
  note "Watch it:  bash scripts/autodeploy.sh status"
fi
note "Ask for a feature any time: say it to Jarvis, or ☰ → Self-improve."
