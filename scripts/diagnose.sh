#!/usr/bin/env bash
# Why isn't my domain loading?
#
#   bash scripts/diagnose.sh
#
# Checks the chain from the outside in: DNS → cloud firewall → host firewall →
# Caddy → certificate → app. Reports the first thing that's actually wrong,
# rather than making you infer it from logs.

set -uo pipefail   # deliberately not -e: every check must run even when one fails

cd "$(dirname "$0")/.."

GREEN='\033[32m'; RED='\033[31m'; YELLOW='\033[33m'; DIM='\033[2m'; BOLD='\033[1m'; RESET='\033[0m'
ok()   { printf "  ${GREEN}✓${RESET} %s ${DIM}%s${RESET}\n" "$1" "${2:-}"; }
bad()  { printf "  ${RED}✗${RESET} %s ${DIM}%s${RESET}\n" "$1" "${2:-}"; PROBLEMS=$((PROBLEMS+1)); }
warn() { printf "  ${YELLOW}!${RESET} %s ${DIM}%s${RESET}\n" "$1" "${2:-}"; }
head_() { printf "\n${BOLD}%s${RESET}\n" "$1"; }
fix()  { printf "      ${YELLOW}→${RESET} %s\n" "$1"; }

PROBLEMS=0

DOMAIN="$(sed -n 's/^\([a-zA-Z0-9][a-zA-Z0-9.-]*\) {$/\1/p' Caddyfile 2>/dev/null | head -1)"
PUBLIC_IP="$(curl -4 -fsS --max-time 10 ifconfig.me 2>/dev/null || echo '')"

printf "${BOLD}Jarvis network diagnosis${RESET}\n"
printf "${DIM}domain: %s   server ip: %s${RESET}\n" "${DOMAIN:-not set}" "${PUBLIC_IP:-unknown}"

# ---------------------------------------------------------------- 1. DNS
head_ "1. DNS — does your domain point here?"
if [ -z "$DOMAIN" ] || [ "$DOMAIN" = "jarvis.example.com" ]; then
  bad "No domain configured in the Caddyfile"
  fix "bash scripts/setup.sh  — and enter your full domain"
else
  RESOLVED="$(getent hosts "$DOMAIN" 2>/dev/null | awk '{print $1}' | head -1)"
  if [ -z "$RESOLVED" ]; then
    bad "$DOMAIN does not resolve"
    fix "Check the domain was added at duckdns.org and 'update ip' was pressed"
  elif [ -n "$PUBLIC_IP" ] && [ "$RESOLVED" != "$PUBLIC_IP" ]; then
    bad "$DOMAIN resolves to $RESOLVED, but this server is $PUBLIC_IP"
    fix "At duckdns.org, set 'current ip' to $PUBLIC_IP and press 'update ip'"
  else
    ok "$DOMAIN → $RESOLVED"
  fi
fi

# ---------------------------------------------------------------- 2. host fw
head_ "2. Host firewall — are 80 and 443 open locally?"
for port in 80 443; do
  if sudo -n iptables -C INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null; then
    ok "port $port allowed"
  elif sudo -n iptables -S INPUT 2>/dev/null | grep -qE '^-P INPUT ACCEPT'; then
    ok "port $port" "(INPUT policy is ACCEPT)"
  else
    warn "could not confirm port $port" "(needs sudo, or a rule is missing)"
    fix "sudo iptables -I INPUT -p tcp --dport $port -j ACCEPT && sudo netfilter-persistent save"
  fi
done

# ---------------------------------------------------------------- 3. containers
head_ "3. Containers"
if ! command -v docker >/dev/null; then
  bad "Docker is not installed"
  fix "bash scripts/bootstrap.sh"
else
  RUNNING="$(docker compose ps --services --filter status=running 2>/dev/null | tr '\n' ' ')"
  if [ -z "${RUNNING// /}" ]; then
    bad "Nothing is running"
    fix "docker compose up -d     (the first build takes several minutes)"
  else
    ok "running:" "$RUNNING"
    for svc in jarvis caddy; do
      case " $RUNNING " in
        *" $svc "*) ;;
        *) bad "$svc is not running"; fix "docker compose logs $svc | tail -30" ;;
      esac
    done
  fi

  # A build still in progress looks exactly like a broken deploy from a browser.
  if docker compose ps 2>/dev/null | grep -qiE 'starting|created|restarting'; then
    warn "a container is still starting — give it a minute and re-run this"
  fi
fi

# ---------------------------------------------------------------- 4. app
head_ "4. The app itself"
HEALTH="$(curl -sS --max-time 8 http://127.0.0.1:8000/api/health 2>/dev/null || echo '')"
if [ -n "$HEALTH" ]; then
  ok "responding on localhost:8000"
  printf "      ${DIM}%s${RESET}\n" "$(printf '%s' "$HEALTH" | head -c 300)"
else
  bad "not responding on localhost:8000"
  fix "docker compose logs jarvis | tail -40"
fi

# ---------------------------------------------------------------- 5. tls
head_ "5. HTTPS certificate"
if command -v docker >/dev/null; then
  CADDY_LOG="$(docker compose logs --tail=200 caddy 2>/dev/null || echo '')"
  if [ -z "$CADDY_LOG" ]; then
    warn "no Caddy logs yet"
  elif printf '%s' "$CADDY_LOG" | grep -qi "certificate obtained successfully"; then
    ok "certificate issued"
  elif printf '%s' "$CADDY_LOG" | grep -qiE "timeout|connection refused|could not connect|no route"; then
    bad "Let's Encrypt cannot reach this server on port 80"
    fix "This is almost always the Oracle Cloud security list, which is separate"
    fix "from the server's own firewall. In the Oracle console:"
    fix "  your instance → subnet → Security List → Add Ingress Rules"
    fix "  source 0.0.0.0/0, TCP, destination port 80   (and again for 443)"
  elif printf '%s' "$CADDY_LOG" | grep -qi "too many certificates"; then
    bad "Let's Encrypt rate limit hit (5 per domain per week)"
    fix "Wait an hour. Fix any config problem before restarting Caddy again."
  elif printf '%s' "$CADDY_LOG" | grep -qiE "obtain|solving challenge|acme"; then
    warn "still working on the certificate — normal for the first minute"
  else
    warn "no certificate confirmation yet"
    fix "docker compose logs caddy | tail -40"
  fi
fi

# ---------------------------------------------------------------- 6. outside
head_ "6. Reaching it from outside"
if [ -n "$DOMAIN" ] && [ "$DOMAIN" != "jarvis.example.com" ]; then
  # curl writes %{http_code} even when it fails, so a `|| echo 000` fallback
  # concatenates onto that and yields "000000", which then matches no branch.
  CODE="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 "https://$DOMAIN/api/health" 2>/dev/null)" || true
  [ -z "$CODE" ] && CODE="000"
  if [ "$CODE" = "200" ]; then
    ok "https://$DOMAIN/api/health returned 200 — it's working"
  elif [ "$CODE" = "000" ]; then
    bad "could not complete an HTTPS request to $DOMAIN"
    fix "If checks 1-5 passed, this is the Oracle security list blocking 443."
  else
    warn "https://$DOMAIN returned HTTP $CODE"
  fi
fi

# ---------------------------------------------------------------- summary
printf "\n"
if [ "$PROBLEMS" -eq 0 ]; then
  printf "${GREEN}No problems found.${RESET} If the browser still hangs, it may be caching —\n"
  printf "try a private tab, or wait a minute for the certificate to finish.\n"
else
  printf "${RED}%d problem(s) found.${RESET} Fix the first one listed and re-run this.\n" "$PROBLEMS"
  printf "\n${DIM}Most common cause by far: the Oracle Cloud security list. The bootstrap\n"
  printf "script opens the server's own firewall, but Oracle blocks separately in the\n"
  printf "web console and both must allow ports 80 and 443.${RESET}\n"
fi
printf "\n"
