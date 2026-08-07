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

    def __post_init__(self) -> None:
        if not self.label:
            host = self.base_url.split("//")[-1].split("/")[0].split(".")[0]
            self.label = f"{host}:{self.model}"

    @property
    def available(self) -> bool:
        return time.monotonic() >= self.cooldown_until

    @property
    def seconds_until_available(self) -> float:
        return max(0.0, self.cooldown_until - time.monotonic())

    def rest(self, seconds: float | None = None) -> None:
        """Take this endpoint out of rotation briefly."""
        self.consecutive_failures += 1
        self.failures += 1
        if seconds is None:
            # Back off further each time an endpoint keeps refusing, so a hard
            # daily cap doesn't get hammered once a minute for the rest of the day.
            seconds = min(DEFAULT_COOLDOWN * (2 ** (self.consecutive_failures - 1)), MAX_COOLDOWN)
        self.cooldown_until = time.monotonic() + max(0.0, seconds)

    def succeeded(self) -> None:
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
        if not self.endpoints:
            return None
        return min(self.endpoints, key=lambda e: e.cooldown_until)

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
                "successes": e.successes,
                "failures": e.failures,
            }
            for e in self.endpoints
        ]


def split_keys(raw: str) -> list[str]:
    """Parse a comma/whitespace separated key list, preserving order."""
    if not raw:
        return []
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

    groq_keys = split_keys(settings.groq_api_keys) or (
        [settings.groq_api_key] if settings.groq_api_key else []
    )
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

    return _prioritise(pool, settings.primary_provider)


def _prioritise(pool: Pool, primary: str) -> Pool:
    """Move one provider's endpoints to the front.

    Position in this list *is* the priority: the client walks it in order and
    stops at the first endpoint that answers, so everything after the primary is
    a backup by construction. There is no separate "backup" flag to set, and no
    load balancing — a secondary is only ever reached when everything ahead of it
    is rate limited or failing.

    Ordering within each provider is preserved, so a primary's own model ladder
    still runs best-first.
    """
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
    for key_raw, base_url, model, name in (
        (settings.cerebras_api_key, settings.cerebras_base_url, settings.cerebras_model, "cerebras"),
        (settings.openrouter_api_key, settings.openrouter_base_url, settings.openrouter_model, "openrouter"),
        (settings.together_api_key, settings.together_base_url, settings.together_model, "together"),
        (settings.gemini_api_key, settings.gemini_base_url, settings.gemini_model, "gemini"),
        (settings.github_models_api_key, settings.github_models_base_url,
         settings.github_models_model, "github"),
        (settings.mistral_api_key, settings.mistral_base_url, settings.mistral_model, "mistral"),
    ):
        for index, key in enumerate(split_keys(key_raw)):
            suffix = f" #{index + 1}" if index else ""
            out.append(
                Endpoint(model=model, api_key=key, base_url=base_url, label=f"{name}:{model}{suffix}")
            )
    return out
