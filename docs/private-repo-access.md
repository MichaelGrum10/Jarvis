# Cloning a private repo onto your server

`MichaelGrum10/Jarvis` is private, so your server needs credentials to fetch it.
There is no unauthenticated download — `raw.githubusercontent.com` returns a bare
404 for private repos no matter how the URL is written, which is what the
`curl … 404` error means.

Two ways forward. Pick one.

---

## Option A — Personal access token (keeps the repo private)

Best if you'd rather not publish your assistant's source. Roughly ten taps on a
phone.

### Create the token

1. In Safari: **github.com** → your avatar → **Settings**
2. Scroll right to the bottom → **Developer settings**
3. **Personal access tokens** → **Fine-grained tokens** → **Generate new token**
4. Fill in:
   - **Token name**: `jarvis-server`
   - **Expiration**: 90 days (or custom — you can regenerate freely)
   - **Repository access**: *Only select repositories* → pick **Jarvis**
   - **Permissions** → **Repository permissions** → **Contents** → **Read-only**
5. **Generate token**, then copy it. It starts `github_pat_…` and **GitHub shows
   it exactly once.**

Read-only on one repository is all the server ever needs. A token this narrow
can't push, can't touch your other repos, and can't change account settings.

### Use it

Run the installer:

```bash
sudo apt-get update -qq && sudo apt-get install -y -qq git && git clone -b claude/jarvis-ai-assistant-9tlgu3 https://github.com/MichaelGrum10/Jarvis.git ~/Jarvis && bash ~/Jarvis/scripts/bootstrap.sh
```

Git prompts:

```
Username for 'https://github.com': MichaelGrum10
Password for 'https://MichaelGrum10@github.com': <paste the token>
```

The password field shows nothing as you paste. That's normal — paste and press
return.

### Save it, so you're not pasting it on every update

```bash
git config --global credential.helper store
```

This writes the token in plaintext to `~/.git-credentials`. On a single-purpose
server you control, that's a reasonable trade for not re-entering it. If you'd
rather not, skip this and paste when asked.

---

## Option B — Make the repo public (simplest)

One toggle, and everything works with no tokens ever.

**github.com → Jarvis → Settings → General →** scroll to **Danger Zone** →
**Change visibility** → **Make public**.

### Is that safe?

For this repo, yes — and it's worth understanding why rather than taking it on
faith:

- **`.env` is gitignored** and has never been committed. Your Groq key, Apple
  password and access password live only on your server.
- The committed `.env.example` contains empty placeholders.
- `scripts/setup.sh` writes secrets at runtime; it doesn't contain any.
- Every secret the diagnostics print is masked.

You can verify this yourself before flipping the switch:

```bash
cd ~/Jarvis && git log --all --full-history -- .env
```

Empty output means `.env` has never been in the history.

### What you'd actually be revealing

The source code, and the fact that you run it. Not your email, not your
credentials, not your data. Your calendar, messages and inbox are never in the
repo — Jarvis reads them live from iCloud and stores them in a SQLite file on
your server that git never sees.

If that's fine by you, Option B is less to maintain: no token to expire, nothing
to rotate.

---

## Which to choose

**Option A** if you'd prefer the repo stay yours alone. The only ongoing cost is
regenerating the token when it expires.

**Option B** if you don't mind the code being visible and want zero maintenance.

Either works identically from Jarvis's point of view.

---

## Troubleshooting

**`curl: (22) … 404`** — the unauthenticated path. Use `git clone` as above.

**`fatal: Authentication failed`** — you entered your GitHub *password*. GitHub
stopped accepting those in 2021; it must be a token.

**`remote: Repository not found`** — the token lacks access to this repo. Check
you selected **Jarvis** under *Only select repositories*, and gave **Contents:
Read-only**.

**Token expired** — generate a new one the same way, then:

```bash
rm -f ~/.git-credentials
cd ~/Jarvis && git pull
```

**Pulling updates later:**

```bash
cd ~/Jarvis && git pull && docker compose up -d --build
```
