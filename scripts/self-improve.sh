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
  printf "${GREEN}✓ Self-improvement off${RESET}\n"
  note "Apply it with: docker compose up -d"
  exit 0
fi

if [ "$MODE" = "apply" ]; then
  printf "${YELLOW}'apply' merges its own changes and restarts, unattended.${RESET}\n"
  printf "${YELLOW}The tests catch bad code; they don't catch a bad idea that passes.${RESET}\n"
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
if grep -qE '^\s*-\s*\./:/app/repo:rw' docker-compose.yml; then
  note "Already mounted."
else
  # Uncomment in place rather than appending: the line already exists commented
  # out, and adding a second one under a different key produces a compose file
  # that looks right and merges wrong.
  python3 - <<'PY' || fail "Could not edit docker-compose.yml — uncomment './:/app/repo:rw' by hand."
import pathlib, re
path = pathlib.Path("docker-compose.yml")
text = path.read_text()
patched, count = re.subn(r"^(\s*)#\s*(-\s*\./:/app/repo:rw\s*)$", r"\1\2", text, flags=re.M)
if count != 1:
    raise SystemExit(1)
path.write_text(patched)
PY
  note "Mounted ./ at /app/repo"
fi

step "Restarting"
docker compose up -d

printf "\n${GREEN}✓ Self-improvement is on in '%s' mode${RESET}\n" "$MODE"
note "Confirm with:  docker compose exec jarvis python -m jarvis.doctor"
note "See what it would work on:  in the app, ☰ → Self-improve"
if [ "$MODE" = "propose" ]; then
  note "Fixes land on branches named jarvis/auto-*. Nothing merges without you."
fi
