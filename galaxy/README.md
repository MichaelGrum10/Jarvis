# Knowledge galaxy

A 3D view of your markdown vault, plus a question box that answers from it.

    python3 build.py            # scan the vault -> viewer/graph-data.js
    python3 server.py           # serve viewer/ on 127.0.0.1:4700

From your Mac:

    ssh -N -L 4700:127.0.0.1:4700 jarvis

then open <http://localhost:4700>.

## The API key

Answers come from **Groq** (OpenAI-compatible chat completions), which is what
the assistant already uses — free tier, no second bill.

You probably do not need to do anything. If `config.json` still has its
placeholder, the server falls back to `GROQ_API_KEY` in `../.env` — the same
key Jarvis uses. One secret, one place to rotate it.

To override (a different key, or a different model), `server.py` creates
`config.json` here on first run, mode `600`:

```json
{ "api_key": "PUT-YOUR-KEY-HERE", "model": "openai/gpt-oss-120b" }
```

Either way the key is treated as live. `config.json` is **gitignored** and
lives outside `viewer/`, so it is not on any path the static handler can
reach — and the handler additionally refuses any request that resolves outside
`viewer/`, including encoded traversal. The key is stripped from API error
text before anything is returned to the browser.

The galaxy itself works without a key. Only the question box needs one.

## Rebuilding after the notes change

Syncthing rewrites the vault whenever your Mac does. Nothing watches the
filesystem — rebuild deliberately:

    curl -X POST http://127.0.0.1:4700/rebuild

Returns `{"notes": N, "links": N, "generation": "..."}`. Reload the page to
pick up the new graph.

Answers stay correct in between regardless: `/chat` rescans the vault on every
question. What goes stale is the *page* — node ids are array positions, so if
the vault changed after the page loaded, the ids in an answer refer to a graph
your browser does not have. The server detects that and the answer carries a
warning instead of flying the camera to unrelated stars.

## Rate limit

20 questions a minute, shared across everything, so a stray script cannot spend
the whole budget in a loop.

In practice Groq's own ceiling arrives first. Its free tier meters ~8,000
tokens per minute, each question spends roughly 1,200-1,500 of them (six notes
of context plus the answer), and **this shares that bucket with Jarvis
itself** — so heavy questioning here will make the assistant's own capacity
errors more likely. Groq's 429 is reported as a plain sentence rather than a
raw error body, because it is a budget, not a bug.
