#!/usr/bin/env bash
# Ask the running server to render one line in the cloned voice, and report
# exactly what Fish said back — with no phone, browser, or speaker involved.
#
#   bash scripts/say.sh
#   bash scripts/say.sh "Good evening, sir."
#
# This is the test that separates "the server cannot get audio from Fish" from
# "the phone cannot play what it got". A 200 with an mp3 on disk means the
# server side works and the problem is on the phone; anything else prints the
# reason Fish gave. Costs a few characters of credit on a cache miss, nothing
# on a hit.

set -euo pipefail

cd "$(dirname "$0")/.."

GREEN='\033[32m'; RED='\033[31m'; DIM='\033[2m'; RESET='\033[0m'
fail() { printf "${RED}%s${RESET}\n" "$1" >&2; exit 1; }

BASE="${JARVIS_URL:-http://127.0.0.1:8000}"
TEXT="${1:-Good evening, sir. All systems are functioning within normal parameters.}"
OUT="${TMPDIR:-/tmp}/jarvis-say.mp3"

TOKEN="$(bash scripts/token.sh)" || fail "Could not log in — see above."

BODY="$(TEXT="$TEXT" python3 -c 'import json, os; print(json.dumps({"text": os.environ["TEXT"]}))')"
PREPARED="$(curl -sS -X POST "$BASE/api/voice/speak" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d "$BODY")"

URL="$(printf '%s' "$PREPARED" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)
print(data.get("url", ""))
' 2>/dev/null || true)"

if [ -z "$URL" ]; then
  printf "${RED}The server would not prepare the line.${RESET} It said:\n"
  printf "${DIM}%s${RESET}\n" "$(printf '%s' "$PREPARED" | head -c 400)"
  printf "\nIf that says not configured:  bash scripts/setkey.sh FISH_API_KEY … / FISH_VOICE_ID …  then  docker compose up -d\n"
  exit 1
fi

# Headers to stderr-ish temp file, body to disk; the status decides what to print.
HEADERS="$(mktemp)"
STATUS="$(curl -sS -o "$OUT" -D "$HEADERS" -w '%{http_code}' "$BASE$URL" || echo 000)"
# `|| true`: a failed response carries no source header, and under pipefail
# an empty grep would otherwise end the script before it explains anything.
SOURCE="$(grep -i '^x-speech-source:' "$HEADERS" | tr -d '\r' | awk '{print $2}' || true)"
rm -f "$HEADERS"

if [ "$STATUS" = "200" ]; then
  SIZE="$(wc -c < "$OUT" | tr -d ' ')"
  printf "${GREEN}✓ Fish rendered the line${RESET} — %s bytes of mp3 from %s, saved to %s\n" "$SIZE" "${SOURCE:-?}" "$OUT"
  printf "${DIM}The server side works. If the phone still uses its own voice, the reason is on the phone:\n"
  printf "close the app fully and reopen it, then read the status line under the ring.${RESET}\n"
  exit 0
fi

printf "${RED}✗ HTTP %s${RESET} — Fish did not give us audio. The server said:\n" "$STATUS"
printf "${DIM}%s${RESET}\n" "$(head -c 400 "$OUT")"
rm -f "$OUT"
printf "\nThe same reason is in the container log:  docker compose logs --tail 30 jarvis | grep 'Cloned voice'\n"
exit 1
