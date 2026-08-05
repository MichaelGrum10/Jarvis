# Setup, in order

Roughly 30 minutes end to end. Steps 1–4 get you a working assistant; 5 and 6 are
optional extras.

## 1. Oracle Cloud instance

The Always Free tier covers this comfortably. Create a VM:

- **Shape**: `VM.Standard.A1.Flex` (ARM), 2 OCPU / 12 GB — free tier allows up to 4/24
- **Image**: Canonical Ubuntu 22.04
- **Networking**: assign a public IPv4

Add ingress rules to the subnet's security list for TCP **80** and **443** from
`0.0.0.0/0`.

Oracle's Ubuntu images also ship a restrictive local iptables, which catches
almost everyone:

```bash
sudo iptables -I INPUT -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

## 2. Install and clone

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-v2 git
sudo usermod -aG docker $USER && newgrp docker

git clone https://github.com/MichaelGrum10/Jarvis.git
cd Jarvis
bash scripts/setup.sh
```

The script handles everything in step 3 below — generating secrets, collecting
credentials, writing `.env` with tight permissions and setting your domain in the
`Caddyfile`. The rest of this section explains what it's asking for and where to
get each value.

## 3. Credentials

### Secrets

```bash
openssl rand -base64 32   # AUTH_SECRET
openssl rand -base64 32   # BRIDGE_TOKEN
```

Set `ACCESS_PASSWORD` too — it's the only thing between the internet and your
email, so make it long.

### Groq (free, required)

<https://console.groq.com/keys> — sign in, create a key, no card required. Paste
as `GROQ_API_KEY`.

### Apple app-specific password (free)

One password covers both mail and calendar.

1. <https://account.apple.com> → **Sign-In and Security** → **App-Specific Passwords**
2. **+**, label it "Jarvis", copy the `xxxx-xxxx-xxxx-xxxx` value
3. `ICLOUD_EMAIL` = your Apple ID email, `ICLOUD_APP_PASSWORD` = that value

This is not your Apple ID password. It only grants mail and calendar access, and
revoking it from the same page cuts Jarvis off without touching your account.

> If you use a custom iCloud domain, `ICLOUD_EMAIL` must be the address you
> actually sign in with, not the alias.

## 4. Domain, TLS, launch

You need HTTPS — browsers refuse geolocation, the microphone and service workers
over plain HTTP, so neither voice mode nor "find a barber near me" works without
it. HTTPS in turn needs a domain, because certificates can't be issued for a bare
IP address.

**No domain yet? Start at [domain.md](domain.md)** — free DuckDNS subdomain, about
two minutes.

Once it resolves to your server:

```bash
nano Caddyfile      # replace jarvis.example.com
nano searxng/settings.yml   # set a real secret_key
docker compose up -d
docker compose logs -f jarvis
```

Certificates issue automatically within a minute or so. Visit your domain, enter
`ACCESS_PASSWORD`, and check the drawer → **Status** to see which integrations
came up.

### Install on iPhone

Safari → **Share** → **Add to Home Screen**. It runs as a standalone app with its
own icon and keeps you signed in. Tap **◎** once to grant location — that's what
makes "near me" follow you as you travel.

Do the same on any other device; each gets its own token, all revocable at once by
rotating `AUTH_SECRET`.

Voice works out of the box on that same HTTPS — see [voice.md](voice.md). Desktops
open in voice mode, phones in text mode, and the top-bar button switches either way.

## 5. Messages bridge (optional, needs a Mac)

See [messages-bridge.md](messages-bridge.md).

## 6. Self-improvement (optional)

See [autonomy.md](autonomy.md). Set `AUTONOMY_ENABLED=true` and restart.

---

## Verifying

```bash
curl https://your-domain.com/api/health
```

```json
{"status":"ok","tools":29,
 "features":{"llm":true,"mail":true,"calendar":true,"messages":true,"auth":true}}
```

Any `false` means that feature's credentials are missing — its tools stay hidden
from the model rather than failing mid-conversation. `system_status` in chat tells
you exactly which variable to fill in.

## Troubleshooting

**Certificate won't issue.** Port 80 must be reachable from outside — check *both*
the Oracle security list and local iptables. `docker compose logs caddy` names the
specific failure.

**`Groq 401`.** Key wrong or not saved. `docker compose exec jarvis env | grep GROQ`.

**`iCloud rejected the login`.** You used your Apple ID password instead of an
app-specific one, or the account has 2FA in a state that needs re-verifying at
account.apple.com.

**Calendar connects but shows nothing.** iCloud CalDAV hides calendars that aren't
shared to the account you authenticated as. `calendar_list` with no arguments
reports which ones it can see.

**Location button does nothing.** Only works over HTTPS. On iOS also check
Settings → Privacy → Location Services → Safari.

**Microphone does nothing.** Same cause — browsers require HTTPS for the mic. On
iOS also check Settings → Safari → Microphone.

**Rate limited by Groq.** The free tier is generous but finite. The client already
retries with backoff and falls back to `llama-3.1-8b-instant`, which has a separate
quota. Heavy days may still hit a wall — it resets hourly.

**Out of disk.** `docker system prune -a` reclaims old build layers.

## Backups

Everything lives in one SQLite file:

```bash
docker compose exec jarvis sqlite3 /data/jarvis.db ".backup '/data/backup.db'"
docker compose cp jarvis:/data/backup.db ./jarvis-backup-$(date +%F).db
```

Conversations, memories, devices and mirrored messages are all in there. Your mail
and calendar are not — those stay in iCloud and are only ever read live.
