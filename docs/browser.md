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

### Export the cookies

On the computer where you are signed in, install a cookie-export extension —
"Cookie-Editor" and "Get cookies.txt LOCALLY" are the common ones, both available
for Chrome and Firefox. Then:

1. Go to the site and make sure you are signed in (open a subscriber article and
   check you can read it).
2. Open the extension → **Export** → **JSON**.
3. Save it as `cookies.json`.

The JSON is a plain array of cookies. Any of the usual shapes work — a bare
array, or an object with a `cookies` key — and the field-name differences between
extensions are handled.

### Get it onto the server

From your own machine:

```bash
scp cookies.json ubuntu@YOUR-SERVER:~/Jarvis/
```

On a phone, paste the contents into a file instead:

```bash
cd ~/Jarvis && cat > cookies.json
# paste, then press Ctrl-D
```

### Import it

```bash
cd ~/Jarvis && bash scripts/browser.sh session cookies.json
rm cookies.json
```

Delete the file afterwards. The import stores a normalised copy at
`/data/browser-state.json` inside the container, mode 600.

Check what landed:

```bash
bash scripts/browser.sh status
```

Cookie *values* are never returned by any endpoint — you get the site names, a
count, and an expiry date. That output is safe to paste into a chat.

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
