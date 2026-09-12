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

[ $# -ge 2 ] || fail "Usage: bash scripts/setkey.sh NAME value
       e.g. bash scripts/setkey.sh CEREBRAS_API_KEY csk-abc123
       Type the value plainly — no < > around it, those are shell redirects."
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
WARNED=0
warn_shape() { WARNED=1; printf "${YELLOW}! %s${RESET}\n" "$1"; }

# A name that isn't a real setting is the worst failure mode here: pydantic is
# configured with extra="ignore", so .env gains a line, the write reports
# success, and the setting does nothing — with no error anywhere to explain it.
# The field names are read out of config.py by regex rather than by importing
# jarvis, so this works on the host with nothing installed and whether or not
# the container is running.
UNKNOWN="$(NAME="$NAME" python3 - <<'PY'
import os, pathlib, re, sys

source = pathlib.Path("server/jarvis/config.py")
if not source.is_file():
    sys.exit(0)  # Not a full checkout — nothing to validate against.

fields = {m.upper() for m in re.findall(r"^    ([a-z][a-z0-9_]*)\s*:", source.read_text(), re.M)}
# Read by docker-compose's own interpolation rather than by pydantic, so they
# are absent from config.py and still perfectly real.
fields |= {
    "NOTES_HOST_DIR",
    # The OmniRoute sidecar (scripts/omniroute.sh): its profile switch and the
    # secrets docker-compose.yml hands it under its own names.
    "COMPOSE_PROFILES",
    "OMNIROUTE_JWT_SECRET", "OMNIROUTE_API_KEY_SECRET",
    "OMNIROUTE_PASSWORD", "OMNIROUTE_WS_BRIDGE_SECRET",
}
name = os.environ["NAME"]
if not fields or name in fields:
    sys.exit(0)

# Suggest by shared word, which is what a transposition looks like:
# VOICE_MATCH_REQUIRED and REQUIRE_VOICE_MATCH share three.
words = set(name.split("_"))
ranked = sorted(fields, key=lambda f: -len(words & set(f.split("_"))))
near = [f for f in ranked if words & set(f.split("_"))][:4]

print(f"{name} isn't a setting Jarvis reads — it will be written to .env and ignored.")
if near:
    print("Did you mean: " + ", ".join(near))
PY
)"

if [ -n "$UNKNOWN" ]; then
  while IFS= read -r line; do warn_shape "$line"; done <<<"$UNKNOWN"
fi

case "$NAME" in
  GEMINI_API_KEY)
    # Google hands out several credential types that all look like "the key",
    # and only one of them works here. Naming the wrong one is far more useful
    # than repeating what the right one looks like.
    case "$VALUE" in
      AIza*)
        [ ${#VALUE} -ge 35 ] || warn_shape "Google keys are ~39 characters — this looks truncated." ;;
      ya29.*)
        warn_shape "That's an OAuth access token, not an API key — it expires in about an hour. Get a key at aistudio.google.com/apikey" ;;
      gsk_*)
        warn_shape "That's a Groq key. Set it with: bash scripts/setkey.sh GROQ_API_KEY $VALUE" ;;
      projects/*|*/locations/*)
        warn_shape "That's a Google Cloud resource name, not an API key. Keys come from aistudio.google.com/apikey — not the Cloud Console." ;;
      '{'*)
        warn_shape "That's a service-account JSON file. Vertex AI uses those; this needs an AI Studio key from aistudio.google.com/apikey" ;;
      *)
        warn_shape "Google AI Studio keys usually start with 'AIza' — saving it anyway, since formats change." ;;
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

# Say so out loud. A yellow line above a green "✓ Updated" reads as a failure
# unless it's spelled out that the value went in regardless.
if [ "$WARNED" -eq 1 ]; then
  printf "${DIM}  (that's advice, not a rejection — saving it anyway)${RESET}\n"
fi

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
