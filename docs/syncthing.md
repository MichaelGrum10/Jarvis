# Two-way sync between iCloud Drive and the server, with Syncthing

Gets notes written on the Mac onto the server where Jarvis can read them, and
notes Jarvis captures back onto the Mac. Syncthing is peer-to-peer, so nothing
transits a third party and there is no account to create.

Read the three corrections first. Each one is a command that looks right and
fails, or worse, succeeds and quietly destroys something.

---

## Before you start: three corrections

**1. The path is `/home/ubuntu/Jarvis/notes`, capital J.** Linux is
case-sensitive; `jarvis/notes` does not exist and Syncthing will offer to
create it, leaving you with an empty folder syncing happily to nothing while
the real vault sits untouched next to it.

**2. `notes/README.md` is tracked in git.** Sharing the folder as *Send &
Receive* means the Mac side is authoritative about deletions too — and since
your Mac folder has no `README.md`, Syncthing will delete the server's copy.
That dirties the working tree, and the next `git pull` refuses to run. It
surfaces as "Could not reach GitHub", which is a lie the tooling tells you.
The ignore list below covers it.

**3. Jarvis writes captures as root.** The container has no `USER` directive,
so `notes/captures/*.md` are owned by `root` while Syncthing runs as `ubuntu`.
Syncthing can read and send them, but cannot apply a later edit made on the
Mac — it will log a permission error per file. Fix below.

And one judgement call worth stating plainly: **Syncthing and iCloud Drive are
two sync engines managing the same directory.** iCloud may evict a file to a
placeholder at any time; Syncthing sees that as a change and propagates it.
This works, and people run it, but it is the single most likely source of
conflict files. Turning off *Optimize Mac Storage* (step 4) removes most of
the risk. If you would rather not take any of it, sync a folder *outside*
iCloud Drive and let Obsidian's own sync handle the phone.

---

## 0. Check the vault actually exists

Worth thirty seconds, because "my iCloud notes folder" is easy to assume and
easy to be wrong about:

```bash
find "$HOME/Library/Mobile Documents/com~apple~CloudDocs" -name '*.md' | wc -l
```

Zero means there is nothing to sync yet and Syncthing will happily replicate an
empty directory. Create the folder and put some markdown in it first — a vault
is just `.md` files in nested folders, and Obsidian is optional.

Note also that **Apple Notes.app is not iCloud Drive**. Notes.app keeps its
data in a private CloudKit database rather than as files, so Syncthing cannot
see it at all; that route needs an export step first.

## 1. Set your two variables

Every command below uses these. Edit the second line — nothing else needs
changing, and there are no placeholders to trip over.

On the **Mac**:

```bash
ICLOUD_NOTES="$HOME/Library/Mobile Documents/com~apple~CloudDocs/Notes"
SERVER="ubuntu@203.0.113.10"        # ← your instance's public IP

ls "$ICLOUD_NOTES" | head            # sanity check: does it list your notes?
```

If that `ls` errors, fix the path before going further. The folder name is
whatever it is in Finder — `Notes`, `Obsidian`, `vault`.

---

## 2. Install on the Mac, running at login

```bash
brew install syncthing
brew services start syncthing
```

`brew services` registers a LaunchAgent, which is what makes it survive logout
and reboot. Confirm:

```bash
brew services list | grep syncthing      # want: started
```

The Mac's web UI is now at <http://127.0.0.1:8384>.

---

## 3. Install on the server, surviving reboot

Ubuntu's own repo carries an old build; use Syncthing's. It serves `arm64`,
which is what your Ampere instance needs.

```bash
sudo mkdir -p /etc/apt/keyrings
sudo curl -fsSL -o /etc/apt/keyrings/syncthing-archive-keyring.gpg \
  https://syncthing.net/release-key.gpg

echo "deb [signed-by=/etc/apt/keyrings/syncthing-archive-keyring.gpg] https://apt.syncthing.net/ syncthing stable" \
  | sudo tee /etc/apt/sources.list.d/syncthing.list

sudo apt-get update
sudo apt-get install -y syncthing
```

Now the service. **The `enable-linger` line is the one that matters** — without
it a `--user` service is bound to your login session, so it dies when you close
SSH and never starts at boot. This is the most common way this setup silently
fails a week later.

```bash
systemctl --user enable --now syncthing.service
sudo loginctl enable-linger ubuntu

systemctl --user status syncthing.service --no-pager | head -5
```

Verify it is bound to loopback only, as it should be out of the box:

```bash
ss -tlnp | grep 8384        # want 127.0.0.1:8384, NOT 0.0.0.0:8384
```

---

## 4. Force iCloud to download real files — before pairing

This is the step that quietly ruins the whole thing if skipped.

With *Optimize Mac Storage* on, macOS evicts file contents and leaves a stub
named `.YourNote.md.icloud` — a few hundred bytes of plist, not your note. To
Syncthing those stubs *are* the files. You end up with a server full of
`.icloud` junk, no note content, and a galaxy with no stars in it. Worse, with
Send & Receive both ways, a later re-materialisation looks like a mass edit.

Count the damage first:

```bash
find "$ICLOUD_NOTES" -name '*.icloud' | wc -l      # 0 means you are fine
```

If that is not zero, force the download and wait for it to finish:

```bash
brctl download "$ICLOUD_NOTES"

# Re-check until it reaches 0. Large vaults take a few minutes.
find "$ICLOUD_NOTES" -name '*.icloud' | wc -l
```

**Then stop it happening again.** Force-downloading is a one-time fix for a
recurring behaviour — macOS will evict again next time the disk gets tight, and
you will not be told. Turn it off permanently:

 System Settings → your name → iCloud → iCloud Drive → **Optimise Mac Storage: off**

The ignore rules in step 7 also exclude `*.icloud`, so stubs never sync even if
one appears. Belt and braces, because this failure is invisible until you go
looking for a note that is not there.

---

## 5. Reach the server's UI over SSH — never expose it

From the **Mac**, in a terminal you leave open:

```bash
ssh -N -L 8385:127.0.0.1:8384 "$SERVER"
```

Then browse to <http://127.0.0.1:8385>.

**Local port 8385, not 8384.** Your Mac's own Syncthing already owns 8384 —
forward onto the same number and you will be looking at the Mac's UI while
believing it is the server's, and the pairing steps below will make no sense.
Keep them straight: **8384 = Mac, 8385 = server.**

`-N` means "no remote command", so the tunnel just sits there. Close the
terminal to close it.

---

## 6. Pair the two devices

Each device has an ID — a long hyphenated string, not a secret, but the thing
that identifies it.

1. **Server UI** (`:8385`) → *Actions* → *Show ID*. Copy it.
2. **Mac UI** (`:8384`) → *Add Remote Device* → paste the ID → name it
   `jarvis-server` → **Save**.
3. Back on the **server UI**, a bar appears: *"New Device … Add"*. Click
   **Add**, name it `mac`, **Save**.

Both sides must accept. If no bar appears on the server, wait 30 seconds and
reload — discovery is not instant. Both should read **Connected** when done.

---

## 7. Share the folder, Send & Receive on both sides

On the **Mac UI**:

1. *Add Folder*
2. **Folder Path**: your iCloud notes path from step 1
3. **Folder Label**: `notes`
4. **Folder ID**: `jarvis-notes` — must match on both sides, so set it
   explicitly rather than accepting the random one
5. *Sharing* tab → tick **jarvis-server**
6. *Advanced* tab → **Folder Type: Send & Receive**
7. *Ignore Patterns* tab → paste exactly this:

```
.DS_Store
(?d).DS_Store
.obsidian/
*.icloud
.*.icloud
README.md
.stversions
```

`(?d)` lets Syncthing delete a stray `.DS_Store` when it is the only thing
keeping a directory alive — without it, removing a folder on one side leaves an
empty one on the other forever. `README.md` is excluded because it is tracked
in git on the server; see correction 2.

8. **Save**

On the **server UI** (`:8385`) a bar appears offering the shared folder:

1. Click **Add**
2. **Folder Path**: `/home/ubuntu/Jarvis/notes` — capital J
3. *Advanced* → **Folder Type: Send & Receive** (the server writes captures,
   so it cannot be Receive Only)
4. *Ignore Patterns* → paste the same block
5. **Save**

Both sides should show **Up to Date** within a minute or two.

---

## 8. Fix the root-owned captures

Jarvis writes as root; Syncthing runs as `ubuntu`. Existing captures need
handing over, and new ones need a group Syncthing can write through:

```bash
sudo chown -R ubuntu:ubuntu /home/ubuntu/Jarvis/notes
```

Run it again after Jarvis writes new captures, or watch for permission errors
in `journalctl --user -u syncthing -n 50`. A cleaner long-term fix is a `USER`
directive in the Dockerfile, which is a code change rather than a sync change.

---

## 9. Confirm the counts match

On the **server**:

```bash
find /home/ubuntu/Jarvis/notes -name '*.md' -not -path '*/.*' | wc -l
```

On the **Mac**:

```bash
find "$ICLOUD_NOTES" -name '*.md' -not -path '*/.*' | wc -l
```

`-not -path '*/.*'` skips `.stfolder`, `.stversions` and `.obsidian`, which
exist on one side and not the other by design. The two numbers should be
identical. If the server is lower, check step 4 — placeholders are the usual
reason.

Then confirm Jarvis itself sees them:

```bash
cd /home/ubuntu/Jarvis && docker compose exec jarvis python -m jarvis.doctor
```

The **Notes vault** section prints the note count it indexed. That number
matching the two above means the whole chain works — iCloud to disk, disk to
container, container to the galaxy.

---

## When something looks wrong

| Symptom | Cause |
|---|---|
| Server folder full of `.icloud` files | Step 4 skipped; placeholders synced instead of notes |
| Sync stops after you close SSH | `loginctl enable-linger ubuntu` not run |
| You are editing the Mac's settings by mistake | Forwarded onto 8384 instead of 8385 |
| `notes/README.md` vanished, `git pull` refuses | `README.md` missing from ignore patterns |
| "permission denied" in the Syncthing log | Root-owned captures; run the `chown` in step 8 |
| Folder shows "Out of Sync" forever | Ignore patterns differ between the two sides |
| Conflict files appearing | Both engines touched a file; the Optimise Mac Storage setting is the usual root cause |
