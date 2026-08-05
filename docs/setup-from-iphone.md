# Setting up entirely from an iPhone

The whole install works from a phone. Everything except the server commands
happens in Safari, and the server part is one paste into an SSH app.

Budget about 20 minutes.

---

## What you'll need

- **Termius** — free on the App Store. This is your terminal.
- Safari, for Oracle Cloud, DuckDNS, Groq and Apple ID.
- Your Oracle instance's SSH key (see below — this is the one part that can trip you up).

Blink Shell and a-Shell also work if you prefer them. Termius is the least
painful on a small screen and its free tier covers everything here.

---

## First: can you actually reach your server?

SSH needs a private key file. Oracle gave you one when you created the instance,
and where it landed decides which path you take.

### If the key is already on your iPhone

It'll be in **Files**, usually under Downloads or iCloud Drive, named something
like `ssh-key-2026-08-05.key`. Skip to *Connect with Termius*.

### If the key is on your Mac (which is off)

Three options, easiest first:

1. **Turn the Mac on for two minutes and AirDrop the key to your iPhone.** Then
   it's in Files and you're done. This is by far the simplest.
2. **Email it to yourself** from the Mac, then save the attachment to Files.
3. **iCloud Drive** — drop it in a folder, open the Files app on the phone.

A private key in email isn't ideal practice. Since it only guards this one
throwaway server, and you can replace the keypair any time, it's an acceptable
trade if AirDrop isn't available.

### If you haven't created the Oracle instance yet

Better position — you can generate the key on the phone and never move it:

1. In **Termius → Keychain → + → Generate key**. Name it `jarvis`, type ED25519.
2. Tap the key → **Copy public key**.
3. In Safari, create the Oracle instance. Under **Add SSH keys**, choose **Paste
   public keys** and paste it.

Shape for the instance itself: **VM.Standard.A1.Flex**, 2 OCPU / 12 GB RAM,
**Canonical Ubuntu 22.04**, and make sure it gets a public IPv4. All within the
Always Free tier.

---

## Connect with Termius

1. Open Termius → **Keychain** → **+** → **Import key** → pick your `.key` file
   from Files. (Skip if you generated one above.)
2. Go to **Hosts** → **+** → **New Host**:
   - **Hostname**: your instance's public IP (Oracle console → your instance →
     Public IP address)
   - **Username**: `ubuntu`
   - **Key**: the one you just imported
3. Tap the host to connect. Accept the fingerprint prompt.

You should land at a prompt like `ubuntu@instance-2026:~$`.

**"Connection refused" or a hang?** The instance is probably still booting, or
port 22 isn't allowed in the Oracle security list. Give it two minutes, then
check the console.

---

## Run the installer

Typing long commands on glass is miserable, so this is one paste.

Copy this, paste into Termius, hit return:

```
sudo apt-get update -qq && sudo apt-get install -y -qq git && { [ -d ~/Jarvis/.git ] || git clone -b claude/jarvis-ai-assistant-9tlgu3 https://github.com/MichaelGrum10/Jarvis.git ~/Jarvis; } && bash ~/Jarvis/scripts/bootstrap.sh
```

**Git will ask you to sign in**, because the repo is private:

- **Username**: `MichaelGrum10`
- **Password**: a personal access token — *not* your GitHub password, which
  GitHub stopped accepting years ago

[private-repo-access.md](private-repo-access.md) walks through creating one on a
phone, and covers making the repo public instead if you'd prefer.

It then installs Docker, opens the firewall, and pauses to let you set up the
domain.

**Already started once?** Don't re-run the clone — it will refuse because the
directory exists. Resume with:

```
cd ~/Jarvis && git pull && bash scripts/bootstrap.sh
```

Your `.env` is left alone, so press Enter at each prompt to keep what you already
entered.


---

## While it's running: three browser tabs

Switch to Safari for these, then come back to Termius. iOS keeps the SSH session
alive for a while in the background, but don't linger — if it drops, reconnect
and re-run the same command. It's safe to re-run.

### 1. Oracle firewall

The script opens the server's own firewall, but Oracle blocks separately in the
console and you must do both.

Oracle Cloud console → your instance → click the **subnet** link → **Security
List** → **Add Ingress Rules**. Add two:

| Source | Protocol | Destination port |
|---|---|---|
| `0.0.0.0/0` | TCP | 80 |
| `0.0.0.0/0` | TCP | 443 |

Mobile Safari handles the Oracle console fine, though **aA → Request Desktop
Website** makes the menus easier to hit.

### 2. DuckDNS domain

The script prints your public IP. With it:

1. **duckdns.org** → sign in with Google or GitHub
2. Type a name — `michael-jarvis` — → **add domain**
3. Paste your IP into the **current ip** box → **update ip**

### 3. Your two credentials

- **console.groq.com/keys** → create a key → copy
- **account.apple.com** → Sign-In and Security → App-Specific Passwords → **+**
  → copy

iOS clipboard only holds one thing at a time, so fetch each one right before the
prompt asks for it rather than trying to collect both up front.

---

## Answer the prompts

Back in Termius, the script asks six things:

| Prompt | Enter |
|---|---|
| Access password | Invent one, 10+ characters. **Write it down** — you'll type it on every device |
| Groq API key | the `gsk_...` you copied |
| Apple ID email | `you@icloud.com` |
| App-specific password | the `xxxx-xxxx-xxxx-xxxx` |
| What Jarvis calls you | your name |
| Domain | `michael-jarvis.duckdns.org` |

**Password fields show nothing as you type.** That's deliberate, not a frozen
keyboard — the most common moment to think Termius has stopped responding. Type
or paste, press Enter, and the script confirms with `✓ got N characters` so you
can tell it landed.

Then it starts the containers. First build takes a few minutes.

---

## Check it worked

```
cd ~/Jarvis && docker compose exec jarvis python -m jarvis.doctor
```

This logs into your iCloud mail for real, calls Groq, and lists your calendars,
naming the specific fix for anything broken. Secrets are masked, so you can
share the output safely when you want help.

Long output on a phone: Termius scrolls, and you can select-all and copy from its
menu.

---

## Install the app

Safari → `https://michael-jarvis.duckdns.org` → **Share** → **Add to Home
Screen**.

It becomes a real app with its own icon and stays signed in. Sign in with your
access password, then tap **◎** once to allow location.

Try *"What's on my calendar today?"* and *"I need a haircut"*.

Phones open in **text** mode by default; tap **🎙** in the top bar to switch to
voice.

---

## Phone-specific tips

**Save the connection.** Termius remembers hosts, so reconnecting later is one
tap.

**Snippets.** Termius → Snippets lets you save `cd ~/Jarvis && docker compose
exec jarvis python -m jarvis.doctor` and run it from a menu instead of typing it.

**The special characters.** Termius has a key row above the keyboard for `-`,
`/`, `|` and tab-completion. Use tab completion aggressively — type `cd ~/Ja`
then tab.

**If the session drops mid-install**, reconnect and run the same bootstrap
command again. Every step checks before acting, so nothing is done twice.

**Landscape** makes long output far more readable.

---

## When your Mac comes back

Only needed for iMessage. From the Mac:

```bash
git clone https://github.com/MichaelGrum10/Jarvis.git
cd Jarvis && git checkout claude/jarvis-ai-assistant-9tlgu3
export JARVIS_URL="https://michael-jarvis.duckdns.org"
export JARVIS_BRIDGE_TOKEN="<printed at the end of setup.sh>"
python3 bridge/jarvis_bridge.py
```

Grant Terminal Full Disk Access first — [messages-bridge.md](messages-bridge.md)
has the detail.

Lost the bridge token? On the server: `grep BRIDGE_TOKEN ~/Jarvis/.env`
