# Getting more capacity (and a better model)

## The problem, precisely

Every turn sends a system prompt plus ~29 tool schemas before you've said
anything — roughly 8–11k tokens. Free tiers meter *tokens per minute*, so two or
three questions in a row can exhaust a minute's budget and everything stops.

The original design made this worse rather than better. When the main model was
busy it "fell back" to `llama-3.1-8b-instant`, which has a **6,000 TPM** limit —
smaller than a single request. So the fallback couldn't ever have worked, and it
produced two failures instead of one:

```
Request too large for llama-3.1-8b-instant … Limit 6000, Requested 11081
tool call validation failed: attempted to call tool 'calendar_list {"start": "today"}'
```

That second error is the same small model fumbling tool calls — putting the
arguments inside the function name, which the provider rejects as an unknown
tool.

Both are fixed. Small models are gone from the ladder, oversized requests are
never retried on a *smaller* model, and malformed tool calls are repaired
client-side and nudged once before moving on.

---

## How the pool works now

An endpoint is a **(provider, key, model)** triple. They're tried in order, and a
rate-limited one is put on a short cooldown and skipped rather than retried —
faster for you, and easier on the provider.

With just the default key you already get three endpoints:

```
groq:llama-3.3-70b-versatile
groq:openai/gpt-oss-120b
groq:moonshotai/kimi-k2-instruct
```

Different models are usually metered separately, so this alone buys real
headroom.

See yours, and what your key can actually reach:

```bash
docker compose exec jarvis python -m jarvis.doctor
```

That queries the provider for the live model list rather than trusting anything
written here — model names and limits change without notice.

---

## Adding capacity

### Best: a second provider

Independent quota, independent uptime, and unambiguously within everyone's
terms. All of these are free and speak the same API, so they drop straight in:

| Provider | Sign up | Notes |
|---|---|---|
| **Cerebras** | <https://cloud.cerebras.ai> | Very fast, generous free tier, good tool calling |
| **OpenRouter** | <https://openrouter.ai/keys> | Aggregates many `:free` models |
| **Together** | <https://api.together.xyz> | Free Llama endpoints |

```bash
# .env
CEREBRAS_API_KEY=csk_...
OPENROUTER_API_KEY=sk-or-...
```

Restart, and they join the end of the pool — used only when Groq is busy, so
your preferred model still answers whenever it can.

### Multiple keys at one provider

Supported, and legitimate when you genuinely have more than one — a work key and
a personal key, say:

```bash
GROQ_API_KEYS=gsk_first,gsk_second
```

**But be careful here.** Creating several accounts at one provider specifically
to get around its rate limits is usually a breach of that provider's terms, and
losing the account costs more than the extra headroom is worth. Check the terms
you agreed to. A second provider gives you *more* capacity than a second key
anyway, with none of the ambiguity — which is why it's the recommendation above.

---

## Is there something better than Groq?

Probably, for your purposes — but measure it rather than take anyone's word,
including mine. Free tiers, model names and rate limits change constantly, so
any ranking written into a document is stale within months.

```bash
docker compose exec jarvis python -m jarvis.benchmark
```

That tests every provider you've configured on requests shaped like the ones
this assistant actually sends, and reports three things:

| | why it matters |
|---|---|
| **latency** | how long a turn takes |
| **tool calling** | can it pick a tool and format the call correctly |
| **at full size** | does it survive a ~29-schema turn, where token caps bite |

Tool calling is weighted above speed on purpose. This assistant is tool calls
almost end to end, so a fast model that fumbles them is unusable — that is
exactly the failure that produced `attempted to call tool 'calendar_list
{"start": "today"}'`.

### The candidates worth trying

All free, all OpenAI-compatible, all drop into the pool with one line:

| Provider | Get a key | Why it might beat Groq |
|---|---|---|
| **Google AI Studio** | <https://aistudio.google.com/apikey> | The Flash models reason better than Llama 3.3 70B, and the ~1M token context makes the schema budget that constrains everything here simply stop mattering. Probably the biggest single upgrade available free. |
| **Cerebras** | <https://cloud.cerebras.ai> | Comparable models, often faster than Groq |
| **GitHub Models** | <https://github.com/settings/tokens> | Frontier GPT-class models free for GitHub accounts. Tight limits, so best kept as a last resort for hard questions |
| **OpenRouter** | <https://openrouter.ai/keys> | Aggregates many `:free` models — useful for trying things cheaply |
| **Mistral** | <https://console.mistral.ai> | Solid tool calling, separate quota |

```bash
# .env — add any of these, they join the pool automatically
GEMINI_API_KEY=AIza...
CEREBRAS_API_KEY=csk_...
GITHUB_MODELS_API_KEY=ghp_...
```

Then benchmark, and put whatever wins first in the ladder.

**Groq's real advantage is speed** — it runs on custom silicon and is usually the
fastest by a wide margin. Its weakness is the per-minute token cap. Which matters
more depends on how you use this, which is the whole reason for measuring rather
than asserting.

## Choosing a better model

Change the ladder:

```bash
GROQ_MODEL_LADDER=openai/gpt-oss-120b,llama-3.3-70b-versatile,moonshotai/kimi-k2-instruct
```

Two things actually matter for this assistant:

**Tool calling has to be reliable.** 29 schemas per turn is demanding, and a
model that fumbles them produces the `not in request.tools` error. Large
instruction-tuned models handle it; small "instant" variants often don't.

**Token-per-minute limit matters more than raw quality.** A brilliant model you
can only call twice a minute is worse, in practice, than a good one you can call
ten times.

Verify before committing — `jarvis.doctor` marks the models in your ladder with
`*` and flags any that your key can't reach, which is the failure mode that
otherwise looks like a mysterious outage.

---

## If it still runs out

**Shorten the history.** Each turn replays recent conversation. It's capped at 12
messages; drop it lower in `server/jarvis/agent/loop.py` if you run long threads.

**Start new conversations more often.** A fresh thread is the cheapest possible
request.

**Wait a minute.** Per-minute buckets refill. The error now tells you roughly how
long, instead of showing you a wall of provider JSON.

---

## What the errors mean now

| Message | Meaning |
|---|---|
| "All N endpoints are busy or failing" | Genuinely out of capacity — add a provider |
| "You're running on a single API key…" | You never configured a second endpoint |
| "That request is too large" | One turn exceeded a model's per-minute cap |
| "No LLM endpoints configured" | `GROQ_API_KEY` is missing entirely |

None of them will show you raw JSON, an organisation ID, or a billing upsell —
those are stripped before anything reaches the chat window.
