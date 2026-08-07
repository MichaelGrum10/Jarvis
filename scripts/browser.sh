#!/usr/bin/env bash
# Turn the real-browser reader on or off, and import a signed-in session.
#
#   bash scripts/browser.sh on                    # install Chromium, enable
#   bash scripts/browser.sh session cookies.json  # import a session export
#   bash scripts/browser.sh status
#   bash scripts/browser.sh off
#
# Chromium is installed into the image only when BROWSER_ENABLED=true, so
# turning this on is a rebuild rather than a restart. That is deliberate: it
# adds about 400MB, and most setups never open a page with it.

set -euo pipefail

cd "$(dirname "$0")/.."

GREEN='\033[32m'; RED='\033[31m'; YELLOW='\033[33m'; DIM='\033[2m'; BOLD='\033[1m'; RESET='\033[0m'
fail() { printf "${RED}%s${RESET}\n" "$1" >&2; exit 1; }
step() { printf "${BOLD}%s${RESET}\n" "$1"; }
note() { printf "${DIM}  %s${RESET}\n" "$1"; }

[ -f docker-compose.yml ] || fail "Run this from the Jarvis directory."

case "${1:-}" in
  on)
    step "Enabling the browser reader"
    bash scripts/setkey.sh BROWSER_ENABLED true >/dev/null
    note "BROWSER_ENABLED=true"

    step "Rebuilding with Chromium (this takes a few minutes)"
    docker compose up -d --build

    printf "\n${GREEN}✓ Browser reader on${RESET}\n"
    note "Next: sign in to the site on your own phone or laptop, export the"
    note "cookies, and import them with:"
    note "  bash scripts/browser.sh session cookies.json"
    note "Full walkthrough: docs/browser.md"
    ;;

  off)
    bash scripts/setkey.sh BROWSER_ENABLED false >/dev/null
    printf "${GREEN}✓ Browser reader off${RESET}\n"
    note "Rebuild to reclaim the disk space: bash scripts/update.sh"
    ;;

  session)
    FILE="${2:-}"
    [ -n "$FILE" ] || fail "Usage: bash scripts/browser.sh session cookies.json"
    [ -f "$FILE" ] || fail "No such file: $FILE"

    TOKEN="$(bash scripts/token.sh 2>/dev/null | tr -d '[:space:]')" \
      || fail "Could not get a device token. Run: bash scripts/token.sh"
    [ -n "$TOKEN" ] || fail "No device token found. Sign in on a device first."

    step "Importing session"
    # Uploaded to the app rather than copied onto the volume: the app normalises
    # the several formats extensions export, and rejects a file that isn't one.
    RESPONSE="$(curl -fsS -X POST http://127.0.0.1:8000/api/browser/session \
      -H "Authorization: Bearer $TOKEN" \
      -F "file=@${FILE};type=application/json")" \
      || fail "Import failed. Is Jarvis running? Check: docker compose logs --tail 20 jarvis"

    printf "${GREEN}✓ %s${RESET}\n" "$RESPONSE"
    note "Cookie values are never echoed back — the summary above is safe to share."
    ;;

  status)
    TOKEN="$(bash scripts/token.sh 2>/dev/null | tr -d '[:space:]')" || TOKEN=""
    [ -n "$TOKEN" ] || fail "No device token found. Sign in on a device first."
    curl -fsS http://127.0.0.1:8000/api/browser/session \
      -H "Authorization: Bearer $TOKEN" || fail "Could not reach Jarvis on :8000"
    printf "\n"
    ;;

  *)
    fail "Usage: bash scripts/browser.sh on|off|status|session cookies.json"
    ;;
esac
