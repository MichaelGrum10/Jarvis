"""Central configuration. Everything is env-driven so nothing secret lives in git."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path("/data") if Path("/data").is_dir() else REPO_ROOT / "data"

# Overridable so the test suite can point somewhere empty. Without this, tests
# silently inherit whatever is in the developer's real .env and fail in ways that
# look like code bugs — which is exactly what happened while building this.
ENV_FILE = Path(os.environ.get("JARVIS_ENV_FILE", REPO_ROOT / ".env"))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore"
    )

    # --- core ---
    app_name: str = "Jarvis"
    owner_name: str = Field("", description="How Jarvis addresses you")
    timezone: str = "America/New_York"
    data_dir: Path = DATA_DIR
    log_level: str = "INFO"

    # --- auth ---
    # Single shared secret. Every device you authorise gets a long-lived token
    # signed with this. Rotate it to revoke every device at once.
    auth_secret: str = Field("", description="Server signing secret (generate 32+ random chars)")
    access_password: str = Field("", description="Password you type once per device")
    token_ttl_days: int = 90

    # Which provider answers first. Everything else becomes a backup — the pool
    # is walked in order and stops at the first endpoint that responds, so a
    # secondary is only reached when everything ahead of it is busy or failing.
    # One of: groq, gemini, cerebras, openrouter, together, github, mistral.
    primary_provider: str = "groq"

    # --- llm (groq free tier) ---
    groq_api_key: str = ""
    # Additional keys, comma separated. Each is a separate quota bucket, tried in
    # order when one is busy. See docs/model-capacity.md before adding several
    # keys from one provider — that is often against their terms, and using a
    # second provider instead gives more capacity anyway.
    groq_api_keys: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_model: str = "openai/gpt-oss-120b"

    # Tried in order. Every model here must be good at tool calling: this
    # assistant sends ~29 tool schemas on every turn, and a model that fumbles
    # them produces malformed calls the provider rejects outright. Small "fast"
    # models are deliberately absent — they have the tightest token-per-minute
    # caps, so they fail on exactly the large requests a fallback exists to catch.
    # Only models verified to exist on Groq's free tier. Anything unreachable
    # here costs a wasted attempt on every single turn before failover moves on,
    # so `python -m jarvis.benchmark` lists what your key actually has and flags
    # entries it cannot reach.
    # gpt-oss-120b leads on measured evidence, not reputation. On a real free-tier
    # account llama-3.3-70b-versatile returns 400 "Failed to call a function" on
    # even a single-tool request, while gpt-oss-120b handles a full 30-schema turn
    # cleanly at the same latency.
    #
    # One entry, deliberately. llama-3.3-70b-versatile was the obvious second
    # choice and is gone because it was measured failing every tool call: a
    # fallback that cannot make tool calls is worse than no fallback at all. It
    # burns two round-trips per turn, then its own escalating cooldown takes the
    # slot out of rotation, so a busy minute on the primary finds nothing behind
    # it. Nothing else has been substituted, because putting an unverified model
    # here would repeat the same mistake with a different name.
    #
    # Real capacity comes from a second *provider*, not a second model on the
    # same key — a rate limit is per account, so the fallback shares the bucket
    # it is meant to relieve. Cerebras and OpenRouter both have free tiers:
    # see docs/model-capacity.md. To add a model here, measure it first with
    # `python -m jarvis.benchmark`, which reports tool-call correctness.
    groq_model_ladder: str = "openai/gpt-oss-120b"

    # --- other OpenAI-compatible providers (all optional, all free tiers) ---
    cerebras_api_key: str = ""
    cerebras_base_url: str = "https://api.cerebras.ai/v1"
    # Cerebras lists models to a free key that it will not actually serve one:
    # the catalogue call succeeds, and the first completion returns 402 "Payment
    # required". So a working key here is not evidence of a usable endpoint —
    # only `python -m jarvis.benchmark`, which makes a real call, settles it.
    cerebras_model: str = "gpt-oss-120b"

    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "meta-llama/llama-3.3-70b-instruct:free"

    together_api_key: str = ""
    together_base_url: str = "https://api.together.xyz/v1"
    together_model: str = "meta-llama/Llama-3.3-70B-Instruct-Turbo-Free"

    # Google AI Studio. Likely the single biggest upgrade available free: the
    # Flash models reason better than Llama 3.3 70B and carry a ~1M token context,
    # which makes the tool-schema budget that constrains everything else here
    # simply stop mattering. Google exposes an OpenAI-compatible endpoint, so it
    # slots into the same pool with no special handling.
    gemini_api_key: str = ""
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    # Measured working on a real free key, including a correct tool call. Two
    # predecessors died here in quick succession: 2.0-flash lost its free
    # allowance (429 "exceeded your current quota"), and 2.5-flash was closed to
    # new users (404 "no longer available to new users"). Neither reads as "wrong
    # model name" at a glance, which is why the benchmark now searches the live
    # catalogue rather than trusting this line.
    #
    # The `models/` prefix is Google's own form and is kept because that is the
    # exact string that was verified; the bare name is also accepted, but was
    # not tested.
    gemini_model: str = "models/gemini-3.6-flash"

    # GitHub Models — free for GitHub accounts, and the way to reach frontier
    # models (GPT-class) without paying. Rate limits are tight, so it earns its
    # place as a last resort for hard questions rather than the everyday default.
    github_models_api_key: str = ""
    # Do not expect this to work. Measured on a real account, models.github.ai
    # answers 410 "GitHub Models is temporarily unavailable as part of a
    # scheduled retirement brownout" — the service is being withdrawn, so no
    # endpoint or model name here will help. Kept configured rather than removed
    # because a brownout is not a shutdown and the settings cost nothing, but it
    # is not a capacity plan. The older models.inference.ai.azure.com host is
    # fully gone: it answers 404 with an empty body, which reads as a bad model
    # name rather than a bad address.
    github_models_base_url: str = "https://models.github.ai/inference"
    github_models_model: str = "openai/gpt-4o-mini"

    mistral_api_key: str = ""
    mistral_base_url: str = "https://api.mistral.ai/v1"
    mistral_model: str = "mistral-large-latest"

    # NVIDIA's hosted catalogue. Free credits on signup, OpenAI-compatible, and
    # carries frontier open models. build.nvidia.com issues the key.
    nvidia_api_key: str = ""
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    nvidia_model: str = "meta/llama-3.3-70b-instruct"

    # Hugging Face's inference router, which fronts several providers behind one
    # OpenAI-compatible endpoint. A free account gets a monthly allowance.
    huggingface_api_key: str = ""
    huggingface_base_url: str = "https://router.huggingface.co/v1"
    huggingface_model: str = "meta-llama/Llama-3.3-70B-Instruct"

    # Anything else OpenAI-compatible, without waiting for it to be added here.
    # Free tiers appear, change and close faster than any hardcoded list keeps
    # up — four of the defaults in this file went stale during one afternoon —
    # so there is a slot that needs no code change. Set all three and it joins
    # the pool like any other provider.
    custom_api_key: str = ""
    custom_base_url: str = ""
    custom_model: str = ""
    # How long a request may wait for a cooling endpoint rather than failing.
    # An error saying "try again in 15s" is worse than waiting 15s, so these are
    # generous — but they are settings rather than constants so the test suite
    # can set them to zero and not spend a minute asleep.
    # No single gap may outlast the browser's patience: past roughly twenty
    # seconds of silence the connection is dropped and the user sees "Load
    # failed", which is worse than the error the wait was meant to avoid.
    llm_wait_seconds: float = 18.0
    llm_retry_wait_seconds: float = 15.0
    # Higher once tools have run: failing then discards completed work.
    llm_retry_wait_mid_turn_seconds: float = 18.0
    # And a ceiling for the whole turn. A turn is up to eight model calls, so
    # per-call limits alone permit minutes of waiting — which is not slow, it
    # is broken.
    llm_turn_wait_budget_seconds: float = 30.0
    llm_max_tokens: int = 4096
    llm_temperature: float = 0.3
    # Whisper on Groq: free with the same key, and better than the browser engines.
    whisper_model: str = "whisper-large-v3-turbo"

    # --- apple: icloud mail over imap ---
    # Use an app-specific password from appleid.apple.com, NOT your Apple ID password.
    icloud_email: str = ""
    icloud_app_password: str = ""
    imap_host: str = "imap.mail.me.com"
    imap_port: int = 993
    smtp_host: str = "smtp.mail.me.com"
    smtp_port: int = 587

    # --- apple: icloud calendar over caldav ---
    caldav_url: str = "https://caldav.icloud.com/"
    caldav_username: str = ""  # defaults to icloud_email
    caldav_password: str = ""  # defaults to icloud_app_password
    default_calendar: str = ""

    # --- messages bridge (runs on a Mac you own) ---
    bridge_token: str = Field("", description="Shared secret between server and Mac bridge")
    bridge_stale_minutes: int = 15

    # --- mac companion agent (mail, calendar, screen, shortcuts) ---
    # Deliberately not bridge_token. The two grant different capability
    # surfaces, and sharing one secret would mean a leak of the messages token
    # also hands over screen capture.
    agent_secret: str = Field("", description="Shared secret for the Mac companion agent socket")

    # --- HUD ---
    # What the markets panel watches. Comma separated; indices are added
    # automatically and don't belong here.
    watchlist: str = "AAPL,NVDA,MSFT,TSLA,GOOGL"

    # --- search / news ---
    searxng_url: str = ""  # e.g. http://searxng:8080 — free, self-hosted, no key
    brave_api_key: str = ""  # optional fallback, free tier
    user_agent: str = "Jarvis/0.1 (self-hosted personal assistant)"

    # --- real browser (Playwright) ---
    # Off by default: it adds ~400MB to the image and a few hundred MB of RAM
    # while a page is open, and most of the web reads fine over plain HTTP.
    # Turn on for JavaScript-rendered pages and subscriber-only articles.
    browser_enabled: bool = False
    # Chromium's own UA is used when this is blank. The default Jarvis UA above
    # is honest but unrecognised, and sites serve a degraded page to unknown
    # agents — which defeats the point of rendering it properly.
    browser_user_agent: str = ""
    browser_max_chars: int = 8000
    # Use a Chromium already on the machine instead of the one Playwright
    # downloads. Worth having because the two are version-locked: a Playwright
    # upgrade looks for a build number the installed browser doesn't have, and
    # fails with a message about downloading rather than about the mismatch.
    browser_executable_path: str = ""

    # --- places (OpenStreetMap: free, no key) ---
    nominatim_url: str = "https://nominatim.openstreetmap.org"
    overpass_url: str = "https://overpass-api.de/api/interpreter"

    # --- notes: a folder of markdown (an Obsidian vault, or anything shaped
    # like one). Read from disk, so syncing it is git's or iCloud's problem,
    # not ours. Empty means the notes tools stay hidden.
    notes_dir: str = ""

    # --- voice identity ---
    # A filter, not a security boundary: it stops other people in the room being
    # answered, and tells you when someone tried. A recording of you will pass it.
    #
    # 0.78 sits near the measured equal-error rate for this embedding: on
    # synthetic speakers it rejects ~5% of genuine attempts and admits ~7% of
    # impostors. The distributions genuinely overlap, so no threshold is clean —
    # raise it toward 0.85 to favour keeping others out at the cost of being
    # asked to repeat yourself, lower it toward 0.72 for the reverse.
    require_voice_match: bool = False
    voice_match_threshold: float = 0.78
    voice_alert_email: bool = True
    wake_word: str = "jarvis"
    require_wake_word: bool = False

    # --- continuous self-improvement ---
    # off     : nothing runs
    # propose : fix on a branch, run tests, notify you — you merge  (default)
    # apply   : merge automatically when tests pass
    #
    # 'propose' is the default because the risk isn't bad code — tests catch most
    # of that — it's the assistant breaking itself overnight and taking your
    # email and calendar access with it. A branch awaiting review costs minutes.
    improve_mode: str = "off"
    improve_interval_hours: int = 12
    improve_lookback_days: int = 7
    # Don't start work the instant the process boots; it may have just restarted
    # because of the previous cycle, and settling first avoids a restart loop.
    improve_startup_delay_seconds: int = 300

    # --- autonomy ---
    autonomy_enabled: bool = False
    autonomy_repo_path: Path = REPO_ROOT
    autonomy_max_iterations: int = 6
    autonomy_test_command: str = "pytest -q"
    autonomy_branch_prefix: str = "jarvis/auto"

    @property
    def caldav_user(self) -> str:
        return self.caldav_username or self.icloud_email

    @property
    def caldav_pass(self) -> str:
        return self.caldav_password or self.icloud_app_password

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jarvis.db"

    def missing_for(self, feature: str) -> list[str]:
        """Which env vars a feature still needs. Drives the /status page."""
        needs = {
            "llm": [("GROQ_API_KEY", self.groq_api_key)],
            "mail": [
                ("ICLOUD_EMAIL", self.icloud_email),
                ("ICLOUD_APP_PASSWORD", self.icloud_app_password),
            ],
            "calendar": [("CALDAV_USERNAME", self.caldav_user), ("CALDAV_PASSWORD", self.caldav_pass)],
            "messages": [("BRIDGE_TOKEN", self.bridge_token)],
            "agent": [("AGENT_SECRET", self.agent_secret)],
            # A boolean, not a credential — but it gates its tools the same way,
            # so the model is never offered a browser the image may not contain.
            "browser": [("BROWSER_ENABLED", self.browser_enabled)],
            "notes": [("NOTES_DIR", self.notes_dir)],
            "auth": [("AUTH_SECRET", self.auth_secret), ("ACCESS_PASSWORD", self.access_password)],
        }
        return [name for name, value in needs.get(feature, []) if not value]


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    return s
