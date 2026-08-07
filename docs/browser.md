# Reading articles in full

Jarvis normally reads pages over plain HTTP, which is fast and works for most of
the web. Two kinds of page defeat it:

- **JavaScript-rendered** — the HTML arrives nearly empty and the article is
  drawn in afterwards, so there is nothing to parse.
- **Subscriber-only** — the server sends a teaser to anyone without a session.

Turning on the browser reader fixes both. It runs Chromium on your server, opens
the page properly, and — if you have imported a session — reads it signed in.

---

## What it does and does not do

It opens a URL, waits for the page to render, and returns the text. That is all.
It does not click, fill in forms, or navigate anywhere the model chose. The
narrowness is the point: it is why this is safe to aim at accounts you care
about.

**Two things worth knowing before you turn it on.**

Automating access to a subscription is a matter between you and the publisher's
terms of use. This reads articles you already pay for, from a session you created
by signing in yourself — but it is your account and your call. WSJ's terms
prohibit automated access; nothing here hides that.

And a fetched page is text written by someone else. A page can contain
instructions aimed at whatever reads it, which is a real attack surface pointed
at an assistant holding your mail and calendar credentials. Page text is fenced
and labelled as untrusted before the model sees it, and the model is told to
treat it as information rather than instruction. That mitigates the risk; it does
not eliminate it. Be sceptical if Jarvis does something odd right after reading a
page.

---

## 1. Turn it on

```bash
cd ~/Jarvis && bash scripts/browser.sh on
```

Chromium is only installed into the image when this is enabled, so this rebuilds
— a few minutes the first time. It adds about 400MB to the image and a few
hundred megabytes of RAM while a page is open. Chromium is launched on demand and
shuts itself down after three minutes idle, so an idle server holds nothing.

At this point JavaScript-rendered pages already work. Subscriber-only ones need
step 2.

---

## 2. Import your session

You sign in normally, in your own browser, and hand Jarvis the resulting cookies.
**Jarvis never sees your password.**

That is not squeamishness — it is what works. News sites run bot detection that
scripted logins trip routinely, and a string of failed automated sign-ins is
exactly the pattern that gets an account flagged. A session you created normally
looks like what it is.

Takes about five minutes the first time, and again only when the session lapses.

**From a phone**, skip the terminal entirely: export the cookies in Safari, then
open Jarvis → ☰ → **🍪 Browser session** and paste them in. Steps 1-3 below still
apply (the iOS extension is *Cookie Editor for Safari*, App Store, iOS 16+, paid);
steps 4-5 become one paste.

**From a laptop**, either route works — the app paste box or the terminal.

### Step 1 — Install the extension

Go to the Chrome Web Store (or Firefox Add-ons) and search **Cookie-Editor**.
It's free. Install it.

After installing, **pin it to the toolbar** — click the jigsaw-piece icon at the
top right of Chrome, find Cookie-Editor, click the pin. You need to be able to
click it while you're on the WSJ tab, and an unpinned extension hides in that
menu.

### Step 2 — Sign in and prove it worked

1. Go to <https://www.wsj.com> and sign in as normal.
2. **Open an actual subscriber article and check you can read the whole thing.**

Don't skip that second part. If you're not genuinely signed in, the export will
still produce a perfectly valid file full of cookies that don't authenticate
anything — and the failure shows up much later, as "only got 240 characters",
which looks like a bug rather than a bad export.

### Step 3 — Export

With the WSJ article still open in front of you:

1. Click the **Cookie-Editor icon** in your toolbar. A panel opens listing every
   cookie for the site you're on — you'll see a scrollable list of names like
   `djcs_auto`, `ab_uuid`, and others.
2. At the bottom of that panel there's a row of small icons. Click **Export**.
3. Choose **Export as JSON** (some versions just say "JSON"). This copies the
   whole thing to your clipboard.

The extension only exports cookies for the domain you're currently on. That's
why it matters that you're on `www.wsj.com` and not, say, Google.

### Step 4 — Get it onto the server

The clipboard route avoids file transfers entirely. In your SSH session:

```bash
cd ~/Jarvis
cat > cookies.json
```

The cursor drops to a blank line and just sits there — that's correct, it's
waiting for input. **Paste** (Cmd-V or Ctrl-Shift-V). A wall of JSON appears.
Then press **Enter**, then **Ctrl-D** to finish.

Check it arrived whole:

```bash
head -c 100 cookies.json; echo; wc -c cookies.json
```

You want it to start with `[{"` or `{"cookies"` and be a few thousand bytes at
least. A few hundred bytes means the paste was truncated — redo it.

Prefer a file transfer? From your own machine, not the server:

```bash
scp cookies.json ubuntu@YOUR-SERVER:~/Jarvis/
```

### Step 5 — Import it

```bash
cd ~/Jarvis && bash scripts/browser.sh session cookies.json && rm cookies.json
```

You'll get back something like:

```
✓ {"imported":true,"present":true,"domains":["wsj.com"],"cookies":34,"expires":1801234567}
```

**Read the `domains` field.** If it doesn't include `wsj.com`, you exported from
the wrong tab and nothing will work — redo steps 2-4.

The `rm` matters. That file is a working key to your account; there's no reason
to leave a copy lying in your home directory. The import stores a normalised
version at `/data/browser-state.json` inside the container, mode 600.

### Step 6 — Confirm

```bash
bash scripts/browser.sh status
```

Cookie *values* are never returned by any endpoint — you get site names, a count
and an expiry date, nothing usable. That output is safe to paste into a chat.

Then try it for real in the app: ask for *"WSJ markets headlines"*, then
*"open the second one"*.

### If the article still comes back short

Some of WSJ's auth lives on a sign-in subdomain rather than the main site. If a
correct-looking import still yields a teaser, repeat steps 3-5 while on
<https://accounts.wsj.com> and import that file too. Imports merge, so the
second one adds to the first rather than replacing it, and re-importing the same
site just refreshes it.

---

## 3. Use it

Just ask:

> *"WSJ markets headlines"* → *"open the second one"*

or give it a link directly:

> *"What does this say? https://www.wsj.com/..."*

Jarvis is told which sites it holds a session for, so it will open those rather
than telling you they are paywalled.

---

## When it stops working

Sessions expire — typically a few weeks to a month. The symptom is Jarvis
reporting that it only got a few hundred characters despite using the stored
session.

```bash
bash scripts/browser.sh status     # check days_until_expiry
```

Re-export and re-import exactly as above. Signing out of the site in your own
browser also invalidates the stored session, since it is the same session.

**"Could not start Chromium"** — the image was built without it. Run
`bash scripts/browser.sh on` again, which rebuilds.

**A page still comes back thin, with a fresh session** — some sites bind the
session to a device fingerprint, so a cookie exported from your laptop is not
accepted from a datacentre IP. There is no clean way around that, and trying
harder is exactly what gets accounts flagged. Read that one in your own browser.

---

## Turning it off

```bash
bash scripts/browser.sh off
bash scripts/update.sh    # rebuild to reclaim the disk space
```

To remove a stored session without turning the feature off:

```bash
curl -X DELETE http://127.0.0.1:8000/api/browser/session \
  -H "Authorization: Bearer $(bash scripts/token.sh)"
```
