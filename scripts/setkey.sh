#!/usr/bin/env bash
# Set or replace a value in .env without opening an editor.
#
#   bash scripts/setkey.sh GEMINI_API_KEY AIzaSy...
#   bash scripts/setkey.sh PRIMARY_PROVIDER gemini
#
# Idempotent: running it again replaces the value rather than appending a second
# copy. Editing .env by hand on a phone is miserable, and a stray duplicate line
# produces a config that looks right and behaves unpredictably.

set -euo pipefail

cd "$(dirname "$0")/.."

GREEN='\033[32m'; RED='\033[31m'; YELLOW='\033[33m'; DIM='\033[2m'; RESET='\033[0m'
fail() { printf "${RED}%s${RESET}\n" "$1" >&2; exit 1; }

[ $# -ge 2 ] || fail "Usage: bash scripts/setkey.sh NAME value"
[ -f .env ] || fail "No .env here. Run: bash scripts/setup.sh"

NAME="$1"
shift
VALUE="$*"
# Strip leading/trailing whitespace. A trailing space or newline from a phone
# paste survives .env quoting and reaches the Authorization header verbatim,
# where providers reject it as an invalid key — with no hint that whitespace is
# the cause.
VALUE="$(printf '%s' "$VALUE" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"

printf '%s' "$NAME" | grep -qE '^[A-Z][A-Z0-9_]*$' \
  || fail "Setting names are UPPER_SNAKE_CASE, e.g. GEMINI_API_KEY"

case "$VALUE" in
  *"'"*) fail "Value can't contain a single quote — it would break .env quoting." ;;
esac

# Catch a mistyped or truncated key here rather than at the next benchmark run,
# where it surfaces as an opaque "Please pass a valid API key" from the provider.
# These are warnings, not errors: key formats change, and being wrong about one
# shouldn't stop someone configuring their own server.
warn_shape() { printf "${YELLOW}! %s${RESET}\n" "$1"; }

case "$NAME" in
  GEMINI_API_KEY)
    case "$VALUE" in
      AIza*) [ ${#VALUE} -ge 35 ] || warn_shape "Google keys are ~39 characters — this looks truncated." ;;
      *)     warn_shape "Google AI Studio keys start with 'AIza'. Check you copied the whole key from aistudio.google.com/apikey" ;;
    esac
    ;;
  GROQ_API_KEY)
    case "$VALUE" in gsk_*) ;; *) warn_shape "Groq keys start with 'gsk_'." ;; esac
    ;;
  OPENROUTER_API_KEY)
    case "$VALUE" in sk-or-*) ;; *) warn_shape "OpenRouter keys start with 'sk-or-'." ;; esac
    ;;
  CEREBRAS_API_KEY)
    case "$VALUE" in csk-*|csk_*) ;; *) warn_shape "Cerebras keys start with 'csk-'." ;; esac
    ;;
esac

# Values are single-quoted on write. Unquoted, anything containing '#' is
# truncated at that character and trailing spaces are dropped, silently.
if grep -q "^${NAME}=" .env; then
  # A literal replacement via python, not sed: an API key containing / or & would
  # otherwise be mangled by sed's own substitution syntax.
  NAME="$NAME" VALUE="$VALUE" python3 - <<'PY'
import os, pathlib
name, value = os.environ["NAME"], os.environ["VALUE"]
path = pathlib.Path(".env")
lines = path.read_text().splitlines()
out = [f"{name}='{value}'" if line.startswith(f"{name}=") else line for line in lines]
path.write_text("\n".join(out) + "\n")
PY
  ACTION="Updated"
else
  printf "%s='%s'\n" "$NAME" "$VALUE" >> .env
  ACTION="Added"
fi

chmod 600 .env

MASKED="${VALUE:0:6}…${VALUE: -2}"
[ ${#VALUE} -le 10 ] && MASKED="$VALUE"
printf "${GREEN}✓ %s %s = %s${RESET}\n" "$ACTION" "$NAME" "$MASKED"
printf "${DIM}Apply it with:  docker compose up -d${RESET}\n"

case "$NAME" in
  *_API_KEY)
    printf "${YELLOW}Then check it works:${RESET}\n"
    printf "${DIM}  docker compose exec jarvis python -m jarvis.benchmark${RESET}\n"
    ;;
esac
