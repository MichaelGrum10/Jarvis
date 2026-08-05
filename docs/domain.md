# Getting a domain (free, ~5 minutes)

## What a domain actually is

Your server has a public IP address — four numbers like `129.153.44.201`. That
address works. You could type it into a browser and reach your server.

A domain is a human-readable label that points at it:

```
jarvis-mg.duckdns.org   →   129.153.44.201
```

That's the whole idea. Somebody types the name, the internet's phone book (DNS)
looks up the number, and the request goes to your server.

## Why Jarvis needs one

Convenience isn't the real reason. **You cannot get an HTTPS certificate for a
bare IP address**, and modern browsers gate two features behind HTTPS:

- **Microphone access** — no HTTPS, no voice mode
- **Geolocation** — no HTTPS, no "find a barber near me"

Both are things you specifically wanted, and both are hard browser rules that no
setting on your end can override. There's also the ordinary reason: without HTTPS,
your Jarvis password and everything it reads out of your email would cross the
network in plain text.

So the domain isn't decoration. It's what makes the certificate possible, and the
certificate is what makes the features work.

---

## The free route: DuckDNS

DuckDNS hands out subdomains free, forever, no account fee, no card.

### 1. Get your server's public IP

On your Oracle instance:

```bash
curl -4 ifconfig.me
```

Write down what it prints. (It's also on the instance page in the Oracle console,
labelled "Public IP address".)

### 2. Claim a subdomain

1. Go to <https://www.duckdns.org>
2. Sign in with Google, GitHub, Reddit or Twitter — no new account needed
3. In the **domains** box, type a name: `jarvis-mg`, say
4. Click **add domain**

You now own `jarvis-mg.duckdns.org`.

### 3. Point it at your server

On that same page, your new domain has a **current ip** field. Paste the IP from
step 1 into it and click **update ip**.

Also copy the **token** shown at the top of the page — you'll want it in a moment.

### 4. Check it worked

Give it a minute, then from anywhere:

```bash
ping jarvis-mg.duckdns.org
```

If it replies from your server's IP, DNS is live and you're done.

### 5. Keep it pointed there

Oracle's free instances usually hold their IP, but "usually" isn't "always" — a
reboot or a rebuild can change it, and then your domain quietly points nowhere.
One cron line keeps it honest:

```bash
crontab -e
```

Add, substituting your domain and token:

```
*/15 * * * * curl -s "https://www.duckdns.org/update?domains=jarvis-mg&token=YOUR-TOKEN&ip=" >/dev/null 2>&1
```

Leaving `ip=` empty tells DuckDNS to use whatever address the request came from,
which is exactly what you want.

---

## Wire it into Jarvis

Edit `Caddyfile` and replace `jarvis.example.com` with your new domain:

```
jarvis-mg.duckdns.org {
	encode gzip
	reverse_proxy jarvis:8000 {
		flush_interval -1
	}
	...
}
```

Then:

```bash
docker compose up -d
docker compose logs -f caddy
```

Caddy contacts Let's Encrypt, proves it controls the domain, and installs a real
certificate — automatically, and it renews itself every 60 days. You should see
`certificate obtained successfully` within a minute.

Visit `https://jarvis-mg.duckdns.org`. Padlock in the address bar, no warnings,
microphone and location both available.

---

## Buying a real domain instead

If you'd rather have `jarvis.yourname.com`, it's about $10–15/year from Cloudflare
Registrar (sells at cost), Namecheap, or Porkbun.

The setup is the same idea: in your registrar's DNS panel, create an **A record**
with name `jarvis` and value = your server's IP. Then put `jarvis.yourname.com` in
the `Caddyfile`.

Nothing else about Jarvis changes. DuckDNS works exactly as well — the only
difference is the name.

---

## Troubleshooting

**Certificate won't issue.** Let's Encrypt has to reach your server on port **80**
from the outside. Check both layers:

```bash
# Oracle: security list must allow TCP 80 and 443 from 0.0.0.0/0
# Instance: Oracle's Ubuntu images ship a restrictive local firewall
sudo iptables -I INPUT -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

`docker compose logs caddy` names the exact failure.

**Domain doesn't resolve.** DNS propagation is usually seconds for DuckDNS but can
take a few minutes. `dig jarvis-mg.duckdns.org +short` should return your IP.

**Worked yesterday, broken today.** Your server's public IP likely changed. Re-run
the DuckDNS update, and set up the cron job above so it can't happen again.

**"Too many certificates" from Let's Encrypt.** Rate limit — 5 per domain per
week. Wait an hour; it's usually caused by repeatedly restarting Caddy with a
broken config. Fix the config first, then restart once.
