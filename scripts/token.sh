#!/usr/bin/env bash
# Print an API token, or call an endpoint with one already attached.
#
#   bash scripts/token.sh                      # print a token
#   bash scripts/token.sh /api/autonomy/health # call an endpoint, formatted
#
# Reads your access password from .env and logs in, so there is nothing to
# remember and nothing to paste. Tokens are signed and last 90 days; asking for
# another does not invalidate the ones your phone and laptop are using.

set -euo pipefail

cd "$(dirname "$0")/.."

RED='\033[31m'; DIM='\033[2m'; RESET='\033[0m'
fail() { printf "${RED}%s${RESET}\n" "$1" >&2; exit 1; }

[ -f .env ] || fail "No .env here. Run: bash scripts/setup.sh"

# Values are written single-quoted, so strip those back off.
PASSWORD="$(sed -n 's/^ACCESS_PASSWORD=//p' .env | tail -1 | sed "s/^'//; s/'$//")"
[ -n "$PASSWORD" ] || fail "ACCESS_PASSWORD is not set in .env"

# The container publishes to localhost only; going in that way avoids depending
# on DNS, the certificate, or the machine having outside network access at all.
BASE="${JARVIS_URL:-http://127.0.0.1:8000}"

# Build the JSON in Python rather than the shell: a password containing a quote
# or backslash would otherwise produce a malformed body and a baffling 422.
BODY="$(PASSWORD="$PASSWORD" python3 -c '
import json, os
print(json.dumps({"password": os.environ["PASSWORD"], "label": "cli"}))
')"

RESPONSE="$(curl -sS -X POST "$BASE/api/auth/login" \
  -H 'Content-Type: application/json' -d "$BODY" 2>&1)" \
  || fail "Could not reach $BASE — is the container running? (docker compose ps)"

TOKEN="$(printf '%s' "$RESPONSE" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)
print(data.get("token", ""))
' 2>/dev/null || true)"

if [ -z "$TOKEN" ]; then
  printf "${RED}Login failed.${RESET} Server said:\n" >&2
  printf "${DIM}%s${RESET}\n" "$(printf '%s' "$RESPONSE" | head -c 300)" >&2
  exit 1
fi

# No path given: just print the token.
if [ $# -eq 0 ]; then
  printf '%s\n' "$TOKEN"
  exit 0
fi

# Path given: call it with the token attached and format the result.
PATH_ARG="$1"
[ "${PATH_ARG:0:1}" = "/" ] || PATH_ARG="/$PATH_ARG"

curl -sS "$BASE$PATH_ARG" -H "Authorization: Bearer $TOKEN" \
  | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin), indent=2))' \
  2>/dev/null || curl -sS "$BASE$PATH_ARG" -H "Authorization: Bearer $TOKEN"
