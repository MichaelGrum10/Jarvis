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

## Wiring it in

Jarvis needs no code change — the custom provider slot exists for exactly this.

1. Install and start OmniRoute per its own documentation. It serves an
   OpenAI-compatible API on port **20128** by default. (Its install command is
   not reproduced here, because a stale one is worse than none — three of the
   endpoints hardcoded in this project went out of date during a single day.)

2. If it runs on the same host, reach it from the container by IP rather than
   `localhost` — inside the container that is the container:

   ```bash
   cd ~/Jarvis
   bash scripts/setkey.sh CUSTOM_BASE_URL http://172.17.0.1:20128/v1
   bash scripts/setkey.sh CUSTOM_API_KEY whatever-omniroute-expects
   bash scripts/setkey.sh CUSTOM_MODEL a-model-from-its-catalogue
   docker compose up -d
   ```

   All three are required; a half-configured slot is ignored rather than
   building an endpoint that fails every request.

3. Measure it before trusting it:

   ```bash
   docker compose exec jarvis python -m jarvis.benchmark
   ```

   Look at the `custom:` row. Tool calling at full size is the only thing that
   matters — a model that answers beautifully and fumbles tool calls is useless
   here, and that is most of what this assistant does.

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
