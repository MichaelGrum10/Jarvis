#!/usr/bin/env bash
# Pull the newest code and actually run it.
#
#   bash scripts/update.sh
#
# The reason this exists rather than a line in the README: the server code is
# baked into the image at build time, not mounted from the checkout. So
# `git pull && docker compose up -d` updates the files on disk and leaves the
# container running exactly what it ran before — the pull succeeds, compose
# reports the service started, and nothing changed. The failure is invisible
# until you notice a fix you just installed hasn't taken effect.
#
# (`docker compose up -d` alone is right for .env changes — compose recreates
# the container when env_file contents change. It's only code that needs a
# rebuild.)

set -euo pipefail

cd "$(dirname "$0")/.."

GREEN='\033[32m'; RED='\033[31m'; YELLOW='\033[33m'; DIM='\033[2m'; BOLD='\033[1m'; RESET='\033[0m'
say()  { printf "${BOLD}%s${RESET}\n" "$1"; }
note() { printf "${DIM}%s${RESET}\n" "$1"; }
fail() { printf "${RED}%s${RESET}\n" "$1" >&2; exit 1; }

BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)" || fail "Not a git checkout."
BEFORE="$(git rev-parse HEAD)"

say "Fetching $BRANCH…"
# Retry: a dropped connection mid-fetch is common on small cloud instances and
# is not a reason to make someone re-run the whole thing by hand.
for delay in 2 4 8 0; do
  if git pull --ff-only origin "$BRANCH"; then
    break
  fi
  [ "$delay" -eq 0 ] && fail "Could not reach GitHub after four attempts."
  note "Retrying in ${delay}s…"
  sleep "$delay"
done

AFTER="$(git rev-parse HEAD)"

if [ "$BEFORE" = "$AFTER" ]; then
  note "Already up to date at ${AFTER:0:7}."
else
  say "Updated ${BEFORE:0:7} → ${AFTER:0:7}"
  git --no-pager log --oneline "$BEFORE..$AFTER" | sed 's/^/  /'
fi

# Rebuild regardless of whether the pull moved. If someone is running this,
# something isn't behaving as expected, and "no new commits" is not proof the
# running container matches the checkout — a previous update may have pulled
# without rebuilding, which is the exact problem this script exists to fix.
say "Rebuilding…"
docker compose up -d --build

say "Verifying…"
# The image can build cleanly and still fail at startup on a bad setting, so
# check the app answers rather than trusting that compose returned zero.
for _ in $(seq 1 30); do
  if curl -fsS --max-time 3 http://127.0.0.1:8000/api/health >/dev/null 2>&1; then
    printf "${GREEN}✓ Jarvis is running the code at ${AFTER:0:7}${RESET}\n"
    note "Check everything over with:  docker compose exec jarvis python -m jarvis.doctor"
    exit 0
  fi
  sleep 2
done

printf "${YELLOW}! Rebuilt, but the app isn't answering on :8000 yet.${RESET}\n"
note "See what it says:  docker compose logs --tail 40 jarvis"
exit 1
