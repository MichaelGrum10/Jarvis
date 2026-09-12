# OmniRoute as a provider

OmniRoute is a local AI gateway: one OpenAI-compatible endpoint in front of a
large catalogue of providers, with its own failover and routing. Jarvis already
has a pool that does this across a handful of providers, so the question is not
whether to replace it but whether to put OmniRoute *behind* it as one more
endpoint — with far more capacity behind that one endpoint than any single
provider offers.

Given how the afternoon of provider-hunting went — Cerebras not actually free,
GitHub Models mid-retirement, three model defaults expiring in a day — a
catalogue that someone else maintains is worth trying.

---

## What it changes, and what it doesn't

**Changes.** Instead of three endpoints, Jarvis reaches whatever OmniRoute is
configured with. When Groq's per-minute budget runs out, the fallback is a
catalogue rather than one other key.

**Doesn't change.** The per-minute limits of the underlying free tiers. A
gateway spreads load across more accounts; it does not create allowance that
does not exist. If a request needs 8,000 tokens and every free tier is spent,
routing it differently changes nothing.

**Unknown until measured.** Whether it preserves the provider-specific details
this assistant depends on. Two of them cost most of a day to find:

- Gemini signs its tool calls and refuses a follow-up whose signature is
  missing. Jarvis stores each provider's message verbatim and hands it back.
- Groq emits `content: null` on tool-call messages; Gemini rejects it.

A gateway that normalises messages between providers may fix both for free, or
may reintroduce them one layer further away where they are harder to see. The
benchmark answers this, and nothing else does.

---

## Running it here

OmniRoute is a service in this project's `docker-compose.yml`, on the
`omniroute` profile, so it starts, restarts and updates with everything else.
One command installs it and wires it in:

```bash
cd ~/Jarvis
bash scripts/omniroute.sh
```

That generates the three secrets it requires, asks for a dashboard password
(or generates one and shows it once), starts the container, waits until it
answers, sets the custom slot to `http://omniroute:20128/v1` with model
`auto`, and hands OmniRoute every provider key already in `.env` — Groq,
Gemini and the rest — through its management API, so nothing is re-pasted.

Jarvis needs no code change — the custom provider slot exists for exactly this.
All three of its values are required; a half-configured slot is ignored rather
than building an endpoint that fails every request, and `scripts/omniroute.sh
status` and doctor both say so.

**Its dashboard is on the server's loopback only.** Nothing about OmniRoute is
reachable from the internet — deliberately, because its API-key requirement is
off, and that is the only thing between the world and an anonymous LLM proxy.
Reach it over an SSH tunnel from the Mac:

```bash
ssh -N -L 20128:127.0.0.1:20128 ubuntu@<server ip>
```

then `http://localhost:20128` in a browser. You should rarely need it: the
keyless free providers are added by the install (and by `bash
scripts/omniroute.sh free` any time), keyed ones by `seed`. The dashboard's
own "Set up free providers" card lives in a first-run wizard that hides itself
once a password is set, which it is from the moment this script installs — the
card is one API call, and `free` is that call. Two keyless providers OmniRoute's
own catalogue flags for terms-of-service reasons (Kiro, OpenCode Free) are left
out unless you ask with `free all`. Anything OAuth-based is added from the
dashboard's Providers page; the callback goes to `localhost:20128`, which
through the tunnel is the server.

Two facts about it, from its own source rather than its README, because they
decide whether this deployment is sound:

- **Redis is optional.** Without one it says so once and rate-limits in memory.
  One process on one host does not need the shared store.
- **A wrong bearer key is ignored, not rejected, while `REQUIRE_API_KEY` is
  off.** The install then replaces the placeholder with a real key it creates
  through OmniRoute's API, named "Jarvis" in its dashboard, which is what makes
  `REQUIRE_API_KEY=true` possible later. The loopback binding is the security
  either way.
- **`auto` will happily route to a paid model it has a key for.** The first
  real benchmark hit exactly that: an OpenRouter key meant for `:free` models,
  routed to OpenRouter's paid `auto`, answered 402. OmniRoute's `hidePaidModels`
  setting removes anything not catalogued as free from every `auto/*` pool
  before routing; the install turns it on (`freeonly`). Its stricter
  `freeAccessPolicy: strict` mode is deliberately not used — by its own doc it
  can empty the pool, since it admits only providers with a hand-curated
  "no card can be attached" guarantee and a usage adapter.

### Measure it before trusting it

```bash
docker compose exec jarvis python -m jarvis.doctor      # is it reachable from inside?
docker compose exec jarvis python -m jarvis.benchmark   # can it do the actual job?
```

In the benchmark, look at the `custom:` rows. Tool calling at full size is the
only thing that matters — a model that answers beautifully and fumbles tool
calls is useless here, and that is most of what this assistant does. `auto`
routes by OmniRoute's own scoring; `auto/coding` or a named `provider/model`
can be set with `bash scripts/omniroute.sh model …` if `auto` picks badly.

### Other commands

    bash scripts/omniroute.sh status     running? reachable? wired?
    bash scripts/omniroute.sh seed       re-hand it the keys in .env
    bash scripts/omniroute.sh free       add the keyless free providers (free all: every one)
    bash scripts/omniroute.sh freeonly   keep auto off paid models (the 402 fix; freeonly off reverts)
    bash scripts/omniroute.sh apikey     give Jarvis a real OmniRoute key instead of the placeholder
    bash scripts/omniroute.sh logs       its container log
    bash scripts/omniroute.sh off        stop and unwire; its data volume is kept
    bash scripts/omniroute.sh URL MODEL  an OmniRoute running somewhere else

---

## Whether to make it primary

Not at first. Keep Groq direct and leave OmniRoute as the backup, so a gateway
problem degrades into using Groq rather than into nothing working. Once the
benchmark has shown it handling full-size turns across several days, promote it:

```bash
bash scripts/setkey.sh PRIMARY_PROVIDER custom
```

Two claims to check yourself rather than take on faith. Its token-compression
figures are quoted as a wide range; the benchmark reports the actual
`prompt_tokens` for a full-size turn, so compare that number before and after.
And a local gateway is another process holding every one of your provider keys —
worth knowing, if not worth avoiding, on a machine that already holds your mail
and calendar credentials.
