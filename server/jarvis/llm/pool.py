"""A pool of interchangeable LLM endpoints, with failover.

## Why this exists

One free API key is one rate limit. A single turn of this assistant carries a
system prompt plus ~29 tool schemas, which is 8-11k tokens before the user has
said anything — so a per-minute budget gets eaten by two or three questions in a
row, and then everything stops.

The fix that actually works is more than one endpoint. An endpoint here is a
(base URL, key, model) triple, so the pool covers three different situations
with one mechanism:

  * several models on one provider — spread load, and fall back when one is busy
  * several keys on one provider — separate quota buckets
  * several *providers* — Groq, Cerebras, OpenRouter and friends all speak the
    OpenAI wire format, so they slot in unchanged

The third is by far the best answer and the one to reach for first. Different
providers mean genuinely independent capacity, real redundancy when one has an
outage, and no ambiguity about whether you're within your agreement. Stacking
many accounts at a single provider to dodge its limits is usually a breach of
that provider's terms — check yours before going that route, because losing the
account costs more than the extra headroom is worth.

## Failover, not retry

A rate-limited endpoint is not retried into submission. It's put on a short
cooldown and the next endpoint is tried immediately, which is both faster and
kinder to the provider. Only when every endpoint is cooling down does the caller
wait.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# How long an endpoint sits out after a rate limit, when the provider doesn't
# tell us. Long enough for a per-minute bucket to refill, short enough that a
# brief spike doesn't take an endpoint out of rotation for the whole session.
DEFAULT_COOLDOWN = 20.0
MAX_COOLDOWN = 120.0


@dataclass
class Endpoint:
    """One usable way to reach a model."""

    model: str
    api_key: str
    base_url: str
    label: str = ""

    # Runtime state
    cooldown_until: float = 0.0
    consecutive_failures: int = 0
    successes: int = 0
    failures: int = 0
    retired: str = ""  # why this endpoint is out for good, "" while in use
    resting_because: str = ""  # why it is cooling right now

    def __post_init__(self) -> None:
        if not self.label:
            host = self.base_url.split("//")[-1].split("/")[0].split(".")[0]
            self.label = f"{host}:{self.model}"

    @property
    def available(self) -> bool:
        return not self.retired and time.monotonic() >= self.cooldown_until

    def retire(self, reason: str) -> None:
        """Take this endpoint out permanently, for a fault time cannot fix.

        A cooldown assumes recovery. Some faults never recover: a model that
        cannot format a tool call will fail identically on every future request,
        and cooling it just means retrying a guaranteed failure at slower and
        slower intervals — while it still counts as pool capacity that isn't
        there. Retiring it makes the pool's size honest and stops the waste.
        """
        if not self.retired:
            self.retired = reason
            log.warning("Retiring %s: %s", self.label, reason)

    @property
    def seconds_until_available(self) -> float:
        return max(0.0, self.cooldown_until - time.monotonic())

    def rest(self, seconds: float | None = None, *, escalate: bool = True, why: str = "") -> None:
        """Take this endpoint out of rotation briefly.

        `escalate` is for faults that get worse the more you retry them — a
        provider in trouble, a hard daily cap. A per-minute rate limit is not
        one: that bucket refills on a fixed schedule, so doubling the wait each
        time turns a 20-second pause into 40, then 80, for a limit that had
        already cleared. That is how a healthy pool reported "capacity returns
        in about 39s" while both providers were ready to answer.
        """
        self.consecutive_failures += 1
        self.failures += 1
        # "1 still cooling down from earlier failures" says nothing about which
        # failure, so there is no way to tell a busy provider from a broken one
        # without reading the server log.
        self.resting_because = why or self.resting_because
        if seconds is None:
            steps = self.consecutive_failures - 1 if escalate else 0
            seconds = min(DEFAULT_COOLDOWN * (2**steps), MAX_COOLDOWN)
        self.cooldown_until = time.monotonic() + max(0.0, seconds)

    def succeeded(self) -> None:
        self.resting_because = ""
        self.consecutive_failures = 0
        self.successes += 1
        self.cooldown_until = 0.0

    def masked_key(self) -> str:
        if not self.api_key:
            return "(none)"
        return f"{self.api_key[:6]}…{self.api_key[-2:]}"


@dataclass
class Pool:
    endpoints: list[Endpoint] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.endpoints)

    def add(self, endpoint: Endpoint) -> None:
        self.endpoints.append(endpoint)

    def ready(self) -> list[Endpoint]:
        """Endpoints not currently cooling down, best-rested first.

        Ordering is by position, not by success rate: the list is the user's
        stated preference — their best model first — and quietly promoting a
        weaker endpoint because it happens to be idle would trade answer quality
        for latency without anyone asking.
        """
        return [e for e in self.endpoints if e.available]

    def soonest(self) -> Endpoint | None:
        """The endpoint that will come back first. Retired ones never do.

        Excluding them matters: a retired endpoint's cooldown is zero, so it
        would win this comparison and produce "capacity returns shortly" for a
        pool where nothing is coming back at all.
        """
        live = [e for e in self.endpoints if not e.retired]
        if not live:
            return None
        return min(live, key=lambda e: e.cooldown_until)

    def wait_hint(self) -> float:
        endpoint = self.soonest()
        return endpoint.seconds_until_available if endpoint else 0.0

    def status(self) -> list[dict]:
        return [
            {
                "label": e.label,
                "model": e.model,
                "key": e.masked_key(),
                "available": e.available,
                "cooling_for": round(e.seconds_until_available, 1),
                "cooling_because": e.resting_because,
                "retired": e.retired,
                "successes": e.successes,
                "failures": e.failures,
            }
            for e in self.endpoints
        ]


# Matched on the base URL rather than the label: a label is only reliably a
# provider name when build_pool set it, and the fallback is the hostname's first
# component — "api" for nearly all of these.
_PREFIX_BY_HOST = (
    ("api.groq.com", "GROQ"),
    ("api.cerebras.ai", "CEREBRAS"),
    ("openrouter.ai", "OPENROUTER"),
    ("api.together.xyz", "TOGETHER"),
    ("generativelanguage.googleapis.com", "GEMINI"),
    ("models.inference.ai.azure.com", "GITHUB_MODELS"),
    ("models.github.ai", "GITHUB_MODELS"),
    ("api.mistral.ai", "MISTRAL"),
    ("integrate.api.nvidia.com", "NVIDIA"),
    ("router.huggingface.co", "HUGGINGFACE"),
)


def provider_prefix(endpoint) -> str:
    """The .env settings prefix for this endpoint's provider."""
    for host, prefix in _PREFIX_BY_HOST:
        if host in endpoint.base_url:
            return prefix
    return endpoint.label.split(":", 1)[0].upper()


def model_setting(endpoint) -> str:
    """The setting that chooses this endpoint's model.

    Lives here rather than in the benchmark because the client needs it too:
    telling someone to edit GROQ_MODEL_LADDER when an *OpenRouter* model is
    missing names a setting that has nothing to do with the failure.
    """
    prefix = provider_prefix(endpoint)
    # Groq is the only provider configured with a ladder rather than one model.
    return "GROQ_MODEL_LADDER" if prefix == "GROQ" else f"{prefix}_MODEL"


def split_keys(raw: str) -> list[str]:
    """Parse a comma/whitespace separated key list, preserving order."""
    if not raw:
        return []
    # split() on whitespace also strips a trailing newline or stray space from a
    # pasted key — which otherwise survives .env quoting, lands in the
    # Authorization header, and gets rejected as an invalid key.
    parts = [p.strip() for chunk in raw.split(",") for p in chunk.split()]
    seen: set[str] = set()
    out: list[str] = []
    for part in parts:
        if part and part not in seen:
            seen.add(part)
            out.append(part)
    return out


def build_pool(settings) -> Pool:
    """Assemble the pool from settings.

    Order matters and is deliberate: every model on the primary provider is tried
    before moving to a secondary one, because the primary is the provider the
    user configured on purpose. Within a provider, models are in the order given
    — best first.
    """
    pool = Pool()

    groq_keys = split_keys(settings.groq_api_keys) or split_keys(settings.groq_api_key)
    models = [m.strip() for m in settings.groq_model_ladder.split(",") if m.strip()]
    if not models:
        models = [settings.groq_model]

    # Model-major, then key: a different model on the same key is usually a
    # different quota bucket, so this exhausts cheap options before rotating keys.
    for model in models:
        for index, key in enumerate(groq_keys):
            suffix = f" #{index + 1}" if len(groq_keys) > 1 else ""
            pool.add(
                Endpoint(
                    model=model,
                    api_key=key,
                    base_url=settings.groq_base_url,
                    label=f"groq:{model}{suffix}",
                )
            )

    for provider in _extra_providers(settings):
        pool.add(provider)

    # Measured health first, then the stated preference — so the user's primary
    # still leads, and within every group the endpoints known to work come
    # before the ones known not to.
    from .health import rank

    pool.endpoints = rank(pool.endpoints)
    return _prioritise(pool, settings.primary_provider, settings.provider_order)


def _prioritise(pool: Pool, primary: str, order: str = "") -> Pool:
    """Put providers in the order asked for.

    Position in this list *is* the priority: the client walks it in order and
    stops at the first endpoint that answers, so everything after the first is
    a backup by construction. There is no separate "backup" flag to set, and no
    load balancing — a secondary is only ever reached when everything ahead of it
    is rate limited or failing.

    PROVIDER_ORDER names the whole sequence and wins when set; PRIMARY_PROVIDER
    only names who goes first. Providers named in neither keep their build
    order after the named ones. Ordering within each provider is preserved
    either way, so a provider's own model ladder still runs best-first.
    """
    wanted = [name.strip().lower() for name in (order or "").split(",") if name.strip()]
    if wanted:
        present = {e.label.split(":", 1)[0] for e in pool.endpoints}
        unknown = [name for name in wanted if name not in present]
        if unknown:
            log.warning("PROVIDER_ORDER names providers with no endpoints: %s", ", ".join(unknown))
        rank_of = {name: i for i, name in enumerate(wanted)}
        # sorted() is stable, so endpoints sharing a rank keep their build order.
        pool.endpoints = sorted(
            pool.endpoints, key=lambda e: rank_of.get(e.label.split(":", 1)[0], len(wanted))
        )
        return pool

    primary = (primary or "").strip().lower()
    if not primary:
        return pool

    preferred = [e for e in pool.endpoints if e.label.split(":", 1)[0] == primary]
    if not preferred:
        # Naming a provider you haven't configured shouldn't silently do nothing
        # different — but it also shouldn't be fatal, so log and carry on.
        log.warning(
            "PRIMARY_PROVIDER=%s has no configured endpoints; leaving order unchanged", primary
        )
        return pool

    rest = [e for e in pool.endpoints if e not in preferred]
    pool.endpoints = preferred + rest
    return pool


def _extra_providers(settings) -> list[Endpoint]:
    """Other OpenAI-compatible providers, if the user configured any.

    These are the real answer to running out of capacity: independent quota,
    independent uptime, and unambiguously within each provider's terms.
    """
    out: list[Endpoint] = []
    for key_raw, base_url, model_raw, name in (
        (settings.cerebras_api_key, settings.cerebras_base_url, settings.cerebras_model, "cerebras"),
        (settings.openrouter_api_key, settings.openrouter_base_url, settings.openrouter_model, "openrouter"),
        (settings.together_api_key, settings.together_base_url, settings.together_model, "together"),
        (settings.gemini_api_key, settings.gemini_base_url, settings.gemini_model, "gemini"),
        (settings.github_models_api_key, settings.github_models_base_url,
         settings.github_models_model, "github"),
        (settings.mistral_api_key, settings.mistral_base_url, settings.mistral_model, "mistral"),
        (settings.nvidia_api_key, settings.nvidia_base_url, settings.nvidia_model, "nvidia"),
        (settings.huggingface_api_key, settings.huggingface_base_url,
         settings.huggingface_model, "huggingface"),
        (settings.custom_api_key, settings.custom_base_url, settings.custom_model, "custom"),
    ):
        # The custom slot is the only one that can be half-filled, since it has
        # no defaults to fall back on. A key with no URL would otherwise build an
        # endpoint that fails on every request with a confusing error.
        if not base_url or not model_raw:
            continue
        # Every provider takes a comma-separated list, not just Groq. One
        # NVIDIA key reaches a whole catalogue, and listing four models behind
        # it is four independent chances to answer for the cost of one signup.
        models = [m.strip() for m in str(model_raw).split(",") if m.strip()]
        for model in models:
            for index, key in enumerate(split_keys(key_raw)):
                suffix = f" #{index + 1}" if index else ""
                out.append(
                    Endpoint(
                        model=model, api_key=key, base_url=base_url,
                        label=f"{name}:{model}{suffix}",
                    )
                )
    return out
