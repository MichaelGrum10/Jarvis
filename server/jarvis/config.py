"""Central configuration. Everything is env-driven so nothing secret lives in git."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path("/data") if Path("/data").is_dir() else REPO_ROOT / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env"), env_file_encoding="utf-8", extra="ignore"
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

    # --- llm (groq free tier) ---
    groq_api_key: str = ""
    # Additional keys, comma separated. Each is a separate quota bucket, tried in
    # order when one is busy. See docs/model-capacity.md before adding several
    # keys from one provider — that is often against their terms, and using a
    # second provider instead gives more capacity anyway.
    groq_api_keys: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_model: str = "llama-3.3-70b-versatile"

    # Tried in order. Every model here must be good at tool calling: this
    # assistant sends ~29 tool schemas on every turn, and a model that fumbles
    # them produces malformed calls the provider rejects outright. Small "fast"
    # models are deliberately absent — they have the tightest token-per-minute
    # caps, so they fail on exactly the large requests a fallback exists to catch.
    # Only models verified to exist on Groq's free tier. Anything unreachable
    # here costs a wasted attempt on every single turn before failover moves on,
    # so `python -m jarvis.benchmark` lists what your key actually has and flags
    # entries it cannot reach.
    groq_model_ladder: str = "llama-3.3-70b-versatile,openai/gpt-oss-120b"

    # --- other OpenAI-compatible providers (all optional, all free tiers) ---
    cerebras_api_key: str = ""
    cerebras_base_url: str = "https://api.cerebras.ai/v1"
    cerebras_model: str = "llama-3.3-70b"

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
    gemini_model: str = "gemini-2.0-flash"

    # GitHub Models — free for GitHub accounts, and the way to reach frontier
    # models (GPT-class) without paying. Rate limits are tight, so it earns its
    # place as a last resort for hard questions rather than the everyday default.
    github_models_api_key: str = ""
    github_models_base_url: str = "https://models.inference.ai.azure.com"
    github_models_model: str = "gpt-4o-mini"

    mistral_api_key: str = ""
    mistral_base_url: str = "https://api.mistral.ai/v1"
    mistral_model: str = "mistral-large-latest"
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

    # --- search / news ---
    searxng_url: str = ""  # e.g. http://searxng:8080 — free, self-hosted, no key
    brave_api_key: str = ""  # optional fallback, free tier
    user_agent: str = "Jarvis/0.1 (self-hosted personal assistant)"

    # --- places (OpenStreetMap: free, no key) ---
    nominatim_url: str = "https://nominatim.openstreetmap.org"
    overpass_url: str = "https://overpass-api.de/api/interpreter"

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
            "auth": [("AUTH_SECRET", self.auth_secret), ("ACCESS_PASSWORD", self.access_password)],
        }
        return [name for name, value in needs.get(feature, []) if not value]


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    return s
