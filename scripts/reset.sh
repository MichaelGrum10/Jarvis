#!/usr/bin/env bash
# Wipe local state and start over.
#
#   bash scripts/reset.sh
#
# Removes your .env, the containers and their data volume, and reverts the two
# files setup.sh edits in place. Leaves the checkout itself alone, so this is a
# credentials-and-data reset, not a re-download.
#
# What this does NOT do: revoke your Groq key or Apple app-specific password.
# Those live at console.groq.com and account.apple.com — if the point of the
# reset is that a credential leaked, revoke it there as well, or you have only
# changed which copy your server uses.

set -euo pipefail

cd "$(dirname "$0")/.."

bold()  { printf '\n\033[1m%s\033[0m\n' "$1"; }
green() { printf '\033[32m%s\033[0m\n' "$1"; }
warn()  { printf '\033[33m%s\033[0m\n' "$1"; }

bold "Reset Jarvis"
echo "This will remove:"
echo "  • .env — every credential you entered"
echo "  • the containers, and the data volume holding conversations, memories"
echo "    and any mirrored messages"
echo "  • your domain from the Caddyfile, and the generated SearXNG secret"
echo
echo "Your mail and calendar are untouched — those live in iCloud and are only"
echo "ever read live."
echo
read -rp "Type 'reset' to confirm: " answer
[ "$answer" = "reset" ] || { warn "Cancelled — nothing changed."; exit 0; }

bold "1/3  Stopping containers and removing data"
if command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then
  # -v drops the named volume, which is where the SQLite database lives. Without
  # it a "fresh" start would silently keep every old conversation and device.
  if docker compose down -v --remove-orphans 2>/dev/null; then
    green "✓ Containers and data volume removed"
  else
    warn "! Nothing was running — continuing"
  fi
else
  warn "! Docker not available — skipping"
fi

bold "2/3  Removing credentials"
if [ -f .env ]; then
  # Overwrite before unlinking: the file held plaintext secrets, and on a shared
  # or snapshotted disk an unlinked-but-intact block is still readable.
  shred -u .env 2>/dev/null || rm -f .env
  green "✓ .env removed"
else
  warn "! No .env found"
fi

bold "3/3  Reverting edited files"
for file in Caddyfile searxng/settings.yml; do
  if [ -f "$file" ] && git diff --quiet -- "$file" 2>/dev/null; then
    green "✓ $file already clean"
  elif [ -f "$file" ]; then
    git checkout -- "$file" 2>/dev/null && green "✓ $file reverted" \
      || warn "! Could not revert $file"
  fi
done

bold "Done — clean slate"
echo
warn "If you are resetting because a credential leaked, revoke it at the source"
warn "before continuing. Changing your server's copy does not disable the old one:"
echo "    Groq   → console.groq.com/keys"
echo "    Apple  → account.apple.com → Sign-In and Security → App-Specific Passwords"
echo
echo "Then start over with:"
echo
echo "    bash scripts/setup.sh && docker compose up -d"
echo
