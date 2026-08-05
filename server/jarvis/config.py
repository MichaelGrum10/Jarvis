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
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_model: str = "llama-3.3-70b-versatile"
    groq_fast_model: str = "llama-3.1-8b-instant"
    llm_max_tokens: int = 4096
    llm_temperature: float = 0.3

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
